"""
Unit tests for the control-plane -> nodes sequence gate.

The gate must block and loop until BOTH the AWS API says ACTIVE on the target
version AND the Kubernetes API server responds within the latency threshold —
stable for two consecutive checks. We monkeypatch the two probe helpers so the
tests run instantly with no AWS/kubectl.
"""
import upgrade_tools as u


def test_gate_passes_when_both_ok(monkeypatch):
    monkeypatch.setattr(u, "_describe_cluster_version_status",
                        lambda tv: (True, "AWS: ACTIVE on 1.34"))
    monkeypatch.setattr(u, "_apiserver_responsive",
                        lambda: (True, 1.2, "kubectl responded in 1.2s"))
    ok, msg = u._control_plane_ready("1.34", retries=5, interval=0)
    assert ok is True
    assert "safe to proceed" in msg.lower()


def test_gate_blocks_when_aws_not_active(monkeypatch):
    monkeypatch.setattr(u, "_describe_cluster_version_status",
                        lambda tv: (False, "AWS: version=1.33 status=UPDATING"))
    # API would be fine, but AWS gate never opens.
    monkeypatch.setattr(u, "_apiserver_responsive",
                        lambda: (True, 1.0, "ok"))
    ok, msg = u._control_plane_ready("1.34", retries=3, interval=0)
    assert ok is False
    assert "not proceeding to node groups" in msg.lower()


def test_gate_blocks_when_apiserver_slow(monkeypatch):
    # AWS says ACTIVE, but kubectl is too slow -> gate must NOT open.
    monkeypatch.setattr(u, "_describe_cluster_version_status",
                        lambda tv: (True, "AWS: ACTIVE on 1.34"))
    monkeypatch.setattr(u, "_apiserver_responsive",
                        lambda: (False, 25.0, "kubectl responded but slow (25.0s > 10s)"))
    ok, msg = u._control_plane_ready("1.34", retries=3, interval=0)
    assert ok is False


def test_gate_blocks_when_apiserver_times_out(monkeypatch):
    monkeypatch.setattr(u, "_describe_cluster_version_status",
                        lambda tv: (True, "AWS: ACTIVE on 1.34"))
    monkeypatch.setattr(u, "_apiserver_responsive",
                        lambda: (False, 15.0, "kubectl timed out (> 10s) — API server not responsive"))
    ok, msg = u._control_plane_ready("1.34", retries=2, interval=0)
    assert ok is False


def test_gate_requires_stability_not_one_off(monkeypatch):
    # First check good, but then it flaps to bad; must NOT pass on a single good hit.
    aws_seq = iter([
        (True, "AWS: ACTIVE on 1.34"),   # attempt 1: good
        (False, "AWS: status=DEGRADED"), # attempt 2: bad -> resets stability
    ])
    monkeypatch.setattr(u, "_describe_cluster_version_status",
                        lambda tv: next(aws_seq))
    monkeypatch.setattr(u, "_apiserver_responsive",
                        lambda: (True, 1.0, "ok"))
    # Only 2 retries: 1 good then 1 bad -> never reaches stable_needed=2 consecutive.
    ok, msg = u._control_plane_ready("1.34", retries=2, interval=0)
    assert ok is False


def test_gate_passes_after_two_consecutive_good(monkeypatch):
    # bad, then good, good -> should pass once two consecutive good checks land.
    aws_seq = iter([
        (False, "AWS: UPDATING"),
        (True, "AWS: ACTIVE on 1.34"),
        (True, "AWS: ACTIVE on 1.34"),
    ])
    monkeypatch.setattr(u, "_describe_cluster_version_status",
                        lambda tv: next(aws_seq))
    monkeypatch.setattr(u, "_apiserver_responsive",
                        lambda: (True, 1.0, "ok"))
    ok, msg = u._control_plane_ready("1.34", retries=5, interval=0)
    assert ok is True
