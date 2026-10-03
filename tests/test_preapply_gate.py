"""
Tests for the apply-time two-person enforcement (C3).

Targets app/gates.py (pure logic, no CrewAI) so these run without installing
CrewAI. A production cluster must have >= 2 distinct approvers recorded at apply
time, even if the stored required_approvers was somehow lower.
"""
import approval_gate as ag
import gates


EVID = "plan evidence"


def test_preapply_allows_nonprod_single_approver(monkeypatch):
    monkeypatch.setattr(gates.settings, "PROD_CLUSTER_MARKERS", ["prod"])
    monkeypatch.setattr(gates.settings, "REQUIRE_TWO_PERSON_FOR_PROD", True)
    ag.record_request("1.34", "1.33", EVID, cluster_name="expense-dev", required_approvers=1)
    ag.record_approval("1.34", EVID, actor="alice", reason="ok")
    ok, _ = gates.preapply_gate("1.34")
    assert ok is True


def test_preapply_blocks_prod_with_one_approver(monkeypatch):
    monkeypatch.setattr(gates.settings, "PROD_CLUSTER_MARKERS", ["prod"])
    monkeypatch.setattr(gates.settings, "REQUIRE_TWO_PERSON_FOR_PROD", True)
    # A prod cluster whose record only got 1 approver but is marked APPROVED
    # (e.g. required_approvers wrongly stored as 1) must still be refused.
    ag.record_request("1.34", "1.33", EVID, cluster_name="expense-prod", required_approvers=1)
    ag.record_approval("1.34", EVID, actor="alice", reason="ok")
    ok, reason = gates.preapply_gate("1.34")
    assert ok is False
    assert "distinct approver" in reason.lower()


def test_preapply_allows_prod_with_two_approvers(monkeypatch):
    monkeypatch.setattr(gates.settings, "PROD_CLUSTER_MARKERS", ["prod"])
    monkeypatch.setattr(gates.settings, "REQUIRE_TWO_PERSON_FOR_PROD", True)
    ag.record_request("1.34", "1.33", EVID, cluster_name="expense-prod", required_approvers=2)
    ag.record_approval("1.34", EVID, actor="alice", reason="ok")
    ag.record_approval("1.34", EVID, actor="bob", reason="ok")
    ok, _ = gates.preapply_gate("1.34")
    assert ok is True


def test_preapply_blocks_when_no_request():
    ok, reason = gates.preapply_gate("1.34")
    assert ok is False
    assert "no approved request" in reason.lower()


def test_preapply_blocks_when_not_approved(monkeypatch):
    ag.record_request("1.34", "1.33", EVID, cluster_name="expense-dev", required_approvers=1)
    # requested but never approved -> PENDING
    ok, reason = gates.preapply_gate("1.34")
    assert ok is False
    assert "pending" in reason.lower() or "need approved" in reason.lower()
