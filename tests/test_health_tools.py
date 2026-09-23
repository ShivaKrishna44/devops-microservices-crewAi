"""
Unit tests for the zero-downtime health tools. No kubectl / no cluster.

We monkeypatch `health_tools._collect_health` to feed canned cluster snapshots,
so we exercise the pure baseline/diff/regression logic directly.
"""
import json

import health_tools as ht


def _call(tool, **kwargs):
    """Call a CrewAI @tool's underlying function regardless of wrapper version."""
    fn = getattr(tool, "func", None) or getattr(tool, "_run", None) or tool
    return fn(**kwargs)


def _snap(nodes=None, unhealthy_pods=None, degraded=None, replica_map=None):
    nodes = nodes or {"ip-1": "Ready", "ip-2": "Ready"}
    return {
        "nodes": nodes,
        "node_names": sorted(nodes.keys()),
        "unhealthy_pods": unhealthy_pods or [],
        "degraded_workloads": degraded or [],
        "replica_map": replica_map or {},
        "collected_at": 0,
    }


# ─── snapshot / baseline persistence ────────────────────────────────────

def test_snapshot_writes_baseline(monkeypatch, tmp_path):
    monkeypatch.setattr(ht, "_collect_health", lambda: _snap())
    result = _call(ht.snapshot_cluster_health, label="pre-upgrade")
    assert "Baseline captured" in result
    # file exists and is valid JSON
    with open(ht._baseline_path(), "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["label"] == "pre-upgrade"
    assert "nodes" in data


# ─── regression compare ─────────────────────────────────────────────────

def test_compare_no_regression(monkeypatch):
    # baseline healthy, current healthy -> PASS
    monkeypatch.setattr(ht, "_collect_health", lambda: _snap())
    _call(ht.snapshot_cluster_health, label="pre-upgrade")
    result = _call(ht.compare_to_baseline)
    assert "PASS" in result


def test_compare_detects_new_regression(monkeypatch):
    # baseline: everything healthy
    monkeypatch.setattr(ht, "_collect_health", lambda: _snap())
    _call(ht.snapshot_cluster_health, label="pre-upgrade")
    # now: a pod that was fine is broken
    monkeypatch.setattr(
        ht, "_collect_health",
        lambda: _snap(unhealthy_pods=["order-service/order-abc:CrashLoopBackOff"]),
    )
    result = _call(ht.compare_to_baseline)
    assert "REGRESSION" in result or "ALERT" in result
    assert "order-service/order-abc" in result


def test_compare_ignores_preexisting_problem(monkeypatch):
    # baseline: pod already broken BEFORE the upgrade
    monkeypatch.setattr(
        ht, "_collect_health",
        lambda: _snap(unhealthy_pods=["legacy/broken-xyz:Error"]),
    )
    _call(ht.snapshot_cluster_health, label="pre-upgrade")
    # now: same pod still broken, nothing new
    monkeypatch.setattr(
        ht, "_collect_health",
        lambda: _snap(unhealthy_pods=["legacy/broken-xyz:Error"]),
    )
    result = _call(ht.compare_to_baseline)
    assert "PASS" in result  # pre-existing breakage is NOT a regression


def test_compare_detects_degraded_workload(monkeypatch):
    monkeypatch.setattr(ht, "_collect_health", lambda: _snap())
    _call(ht.snapshot_cluster_health, label="pre-upgrade")
    monkeypatch.setattr(
        ht, "_collect_health",
        lambda: _snap(degraded=["payment-service/payment:1/3"]),
    )
    result = _call(ht.compare_to_baseline)
    assert "ALERT" in result or "REGRESSION" in result
    assert "payment-service/payment" in result


def test_compare_without_baseline_warns(monkeypatch, tmp_path):
    # point baseline path at a non-existent file
    monkeypatch.setattr(ht, "_baseline_path", lambda: str(tmp_path / "nope.json"))
    result = _call(ht.compare_to_baseline)
    assert "WARN" in result


# ─── wait_for_healthy loop ──────────────────────────────────────────────

def test_wait_for_healthy_passes_immediately(monkeypatch):
    monkeypatch.setattr(ht, "_collect_health", lambda: _snap())
    result = _call(ht.wait_for_healthy, timeout_seconds=5, interval_seconds=1)
    assert "PASS" in result


def test_wait_for_healthy_times_out_on_bad_node(monkeypatch):
    monkeypatch.setattr(
        ht, "_collect_health",
        lambda: _snap(nodes={"ip-1": "NotReady", "ip-2": "Ready"}),
    )
    # tiny timeout so the loop exits fast
    result = _call(ht.wait_for_healthy, timeout_seconds=1, interval_seconds=1)
    assert "ALERT" in result
    assert "NotReady" in result or "ip-1" in result


def test_wait_for_healthy_times_out_on_bad_pod(monkeypatch):
    monkeypatch.setattr(
        ht, "_collect_health",
        lambda: _snap(unhealthy_pods=["ns/pod:Pending"]),
    )
    result = _call(ht.wait_for_healthy, timeout_seconds=1, interval_seconds=1)
    assert "ALERT" in result


def test_wait_for_healthy_fails_if_old_nodes_still_present(monkeypatch):
    # baseline had old-1, old-2; after upgrade old-1 is STILL there (rollover stalled)
    baseline = _snap(nodes={"old-1": "Ready", "old-2": "Ready"})
    monkeypatch.setattr(ht, "_collect_health", lambda: baseline)
    _call(ht.snapshot_cluster_health, label="pre-upgrade")
    # current: new node is up and Ready, but old-1 never terminated
    monkeypatch.setattr(
        ht, "_collect_health",
        lambda: _snap(nodes={"new-1": "Ready", "new-2": "Ready", "old-1": "Ready"}),
    )
    result = _call(ht.wait_for_healthy, timeout_seconds=1, interval_seconds=1)
    assert "ALERT" in result
    assert "old-1" in result  # names the lingering old node


def test_wait_for_healthy_passes_when_old_nodes_gone(monkeypatch):
    baseline = _snap(nodes={"old-1": "Ready", "old-2": "Ready"})
    monkeypatch.setattr(ht, "_collect_health", lambda: baseline)
    _call(ht.snapshot_cluster_health, label="pre-upgrade")
    # current: entirely new nodes, old ones gone
    monkeypatch.setattr(
        ht, "_collect_health",
        lambda: _snap(nodes={"new-1": "Ready", "new-2": "Ready"}),
    )
    result = _call(ht.wait_for_healthy, timeout_seconds=5, interval_seconds=1)
    assert "PASS" in result


def test_wait_for_healthy_fails_on_replica_mismatch(monkeypatch):
    # Nodes are correctly replaced (old gone) so the ONLY failing criterion is
    # the replica mismatch — isolates the replica check.
    baseline = _snap(nodes={"old-1": "Ready", "old-2": "Ready"},
                     replica_map={"order-service/order": "3"})
    monkeypatch.setattr(ht, "_collect_health", lambda: baseline)
    _call(ht.snapshot_cluster_health, label="pre-upgrade")
    # after upgrade: nodes replaced, but only 1 replica instead of the baseline 3
    monkeypatch.setattr(
        ht, "_collect_health",
        lambda: _snap(nodes={"new-1": "Ready", "new-2": "Ready"},
                      replica_map={"order-service/order": "1"}),
    )
    result = _call(ht.wait_for_healthy, timeout_seconds=1, interval_seconds=1)
    assert "ALERT" in result
    assert "order-service/order" in result


def test_wait_for_healthy_passes_when_replicas_match(monkeypatch):
    # baseline on OLD nodes; after upgrade the nodes are REPLACED (new names)
    # and replica counts still match -> PASS.
    baseline = _snap(nodes={"old-1": "Ready", "old-2": "Ready"},
                     replica_map={"order-service/order": "3"})
    monkeypatch.setattr(ht, "_collect_health", lambda: baseline)
    _call(ht.snapshot_cluster_health, label="pre-upgrade")
    monkeypatch.setattr(
        ht, "_collect_health",
        lambda: _snap(nodes={"new-1": "Ready", "new-2": "Ready"},
                      replica_map={"order-service/order": "3"}),
    )
    result = _call(ht.wait_for_healthy, timeout_seconds=5, interval_seconds=1)
    assert "PASS" in result
