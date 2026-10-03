"""
Unit tests for availability pre-checks: node capacity headroom and EC2 surge
quota. No kubectl / no aws / no cluster — we monkeypatch the subprocess helpers
in eks_tools to return canned output, then assert the PASS/ALERT/WARN logic.
"""
import eks_tools as e


def _call(tool, **kwargs):
    fn = getattr(tool, "func", None) or getattr(tool, "_run", None) or tool
    return fn(**kwargs)


# ─── capacity headroom (>=2 Ready nodes) ─────────────────────────────────

def test_capacity_headroom_pass_two_nodes(monkeypatch):
    monkeypatch.setattr(e, "_run_kubectl", lambda cmd, timeout=30:
                        "ip-1 Ready <none> 5d v1.33\nip-2 Ready <none> 5d v1.33")
    r = _call(e.check_capacity_headroom)
    assert "PASS" in r


def test_capacity_headroom_alert_single_node(monkeypatch):
    monkeypatch.setattr(e, "_run_kubectl", lambda cmd, timeout=30:
                        "ip-1 Ready <none> 5d v1.33")
    r = _call(e.check_capacity_headroom)
    assert "ALERT" in r
    assert "downtime" in r.lower()


def test_capacity_headroom_alert_no_nodes(monkeypatch):
    monkeypatch.setattr(e, "_run_kubectl", lambda cmd, timeout=30: "")
    r = _call(e.check_capacity_headroom)
    assert "ALERT" in r


def test_capacity_headroom_warn_on_error(monkeypatch):
    monkeypatch.setattr(e, "_run_kubectl", lambda cmd, timeout=30: "ERROR: unreachable")
    r = _call(e.check_capacity_headroom)
    assert "WARN" in r


# ─── vCPU helper ─────────────────────────────────────────────────────────

def test_vcpus_known_and_unknown():
    assert e._vcpus_for("t3.medium") == 2
    assert e._vcpus_for("m5.2xlarge") == 8
    # unknown type falls back to the conservative default (over-estimate)
    assert e._vcpus_for("zz.unknown") == e._DEFAULT_VCPU


# ─── surge quota ─────────────────────────────────────────────────────────

def _aws_dispatch(responses):
    """Return a fake _run_aws that matches on a substring of the command."""
    def fake(cmd, timeout=60):
        for needle, out in responses.items():
            if needle in cmd:
                return out
        return "ERROR: unmatched command in test"
    return fake


def test_surge_quota_pass_when_headroom_enough(monkeypatch):
    # one node group, 1 surge node of t3.medium (2 vCPU). quota 100, in-use 10 -> headroom 90.
    responses = {
        "list-nodegroups": "ng-default",
        "describe-nodegroup": "t3.medium\t3\t1",            # instanceType, maxSize, maxUnavailable
        "get-service-quota": "100",
        "describe-instances": "1\t2\n1\t2\n1\t1",           # cores x threads -> 2+2+1 = 5 vCPU in use
    }
    monkeypatch.setattr(e, "_run_aws", _aws_dispatch(responses))
    r = _call(e.check_ec2_surge_quota)
    assert "PASS" in r


def test_surge_quota_alert_when_headroom_short(monkeypatch):
    # big surge (m5.4xlarge=16 vCPU x maxUnavailable 2 = 32) vs tiny headroom.
    responses = {
        "list-nodegroups": "ng-big",
        "describe-nodegroup": "m5.4xlarge\t10\t2",          # 16 vCPU * 2 = 32 surge vCPU
        "get-service-quota": "40",                           # quota 40
        "describe-instances": "8\t2\n8\t2",                  # 16+16 = 32 vCPU in use -> headroom 8
    }
    monkeypatch.setattr(e, "_run_aws", _aws_dispatch(responses))
    r = _call(e.check_ec2_surge_quota)
    assert "ALERT" in r
    assert "freeze" in r.lower()


def test_surge_quota_warn_when_quota_unreadable(monkeypatch):
    responses = {
        "list-nodegroups": "ng-default",
        "describe-nodegroup": "t3.medium\t3\t1",
        "get-service-quota": "ERROR: access denied",
        "describe-instances": "1\t2",
    }
    monkeypatch.setattr(e, "_run_aws", _aws_dispatch(responses))
    r = _call(e.check_ec2_surge_quota)
    assert "WARN" in r


def test_surge_quota_warn_when_no_nodegroups(monkeypatch):
    responses = {"list-nodegroups": ""}
    monkeypatch.setattr(e, "_run_aws", _aws_dispatch(responses))
    r = _call(e.check_ec2_surge_quota)
    assert "WARN" in r


# ─── PDB strength (preventive) ───────────────────────────────────────────

import json as _json


def _pdb_json(items):
    return _json.dumps({"items": items})


def _pdb(ns, name, allowed, healthy):
    return {
        "metadata": {"namespace": ns, "name": name},
        "status": {"disruptionsAllowed": allowed, "currentHealthy": healthy},
    }


def test_pdb_strength_pass_when_strict(monkeypatch):
    # 20% threshold; PDB allows 1 of 5 disrupted = 20% -> at the cap, OK.
    monkeypatch.setattr(e.settings, "AVAILABILITY_DROP_THRESHOLD", 0.20)
    monkeypatch.setattr(e.settings, "CRITICAL_NAMESPACES", ["order-service"])
    monkeypatch.setattr(e, "_run_kubectl",
                        lambda cmd, timeout=30: _pdb_json([_pdb("order-service", "order-pdb", 1, 5)]))
    r = _call(e.check_pdb_strength)
    assert "PASS" in r


def test_pdb_strength_alert_when_too_loose(monkeypatch):
    # allows 3 of 6 disrupted = 50% > 20% -> too loose.
    monkeypatch.setattr(e.settings, "AVAILABILITY_DROP_THRESHOLD", 0.20)
    monkeypatch.setattr(e.settings, "CRITICAL_NAMESPACES", ["order-service"])
    monkeypatch.setattr(e, "_run_kubectl",
                        lambda cmd, timeout=30: _pdb_json([_pdb("order-service", "loose-pdb", 3, 6)]))
    r = _call(e.check_pdb_strength)
    assert "ALERT" in r
    assert "too LOOSE" in r or "too loose" in r.lower()


def test_pdb_strength_alert_when_too_strict_zero_disruptions(monkeypatch):
    # disruptionsAllowed=0 (e.g. maxUnavailable:0 or minAvailable==replicas)
    # -> drain can evict nothing -> STALL/force-evict.
    monkeypatch.setattr(e.settings, "AVAILABILITY_DROP_THRESHOLD", 0.20)
    monkeypatch.setattr(e.settings, "CRITICAL_NAMESPACES", ["order-service"])
    monkeypatch.setattr(e, "_run_kubectl",
                        lambda cmd, timeout=30: _pdb_json([_pdb("order-service", "strict-pdb", 0, 3)]))
    r = _call(e.check_pdb_strength)
    assert "ALERT" in r
    assert "too STRICT" in r or "stall" in r.lower()


def test_pdb_strength_alert_when_no_pdbs(monkeypatch):
    monkeypatch.setattr(e, "_run_kubectl", lambda cmd, timeout=30: _pdb_json([]))
    r = _call(e.check_pdb_strength)
    assert "ALERT" in r


def test_pdb_strength_flags_misconfigured_zero_healthy(monkeypatch):
    monkeypatch.setattr(e.settings, "CRITICAL_NAMESPACES", ["order-service"])
    monkeypatch.setattr(e, "_run_kubectl",
                        lambda cmd, timeout=30: _pdb_json([_pdb("order-service", "bad-pdb", 0, 0)]))
    r = _call(e.check_pdb_strength)
    assert "ALERT" in r
    assert "misconfigured" in r.lower()


def test_pdb_strength_ignores_kube_system(monkeypatch):
    monkeypatch.setattr(e.settings, "AVAILABILITY_DROP_THRESHOLD", 0.20)
    monkeypatch.setattr(e.settings, "CRITICAL_NAMESPACES", [])  # all app ns
    # kube-system PDB is loose but must be ignored; an app PDB is strict -> PASS
    monkeypatch.setattr(
        e, "_run_kubectl",
        lambda cmd, timeout=30: _pdb_json([
            _pdb("kube-system", "coredns", 5, 5),      # ignored
            _pdb("order-service", "order-pdb", 1, 5),  # 20% ok
        ]),
    )
    r = _call(e.check_pdb_strength)
    assert "PASS" in r


def test_pdb_strength_warn_on_kubectl_error(monkeypatch):
    monkeypatch.setattr(e, "_run_kubectl", lambda cmd, timeout=30: "ERROR: unreachable")
    r = _call(e.check_pdb_strength)
    assert "WARN" in r


# ─── cluster upgradeable pre-flight ──────────────────────────────────────

def test_cluster_upgradeable_pass_when_active(monkeypatch):
    monkeypatch.setattr(e, "_run_aws", lambda cmd, timeout=60: "ACTIVE")
    r = _call(e.check_cluster_upgradeable)
    assert "PASS" in r


def test_cluster_upgradeable_alert_when_updating(monkeypatch):
    monkeypatch.setattr(e, "_run_aws", lambda cmd, timeout=60: "UPDATING")
    r = _call(e.check_cluster_upgradeable)
    assert "ALERT" in r
    assert "UNSAFE" in r


def test_cluster_upgradeable_alert_when_not_found(monkeypatch):
    monkeypatch.setattr(e, "_run_aws",
                        lambda cmd, timeout=60: "ERROR: ResourceNotFoundException")
    r = _call(e.check_cluster_upgradeable)
    assert "ALERT" in r
