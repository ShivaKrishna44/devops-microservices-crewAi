"""
Unit tests for the hardened approval gate. No AWS/kubectl/terraform.

Covers: version binding, evidence-hash binding (drift detection), single vs
two-person approval, distinct-approver enforcement, TTL expiry, and rejection.
"""
from datetime import datetime, timezone, timedelta

import pytest

import approval_gate as ag


EVIDENCE = "PRE-CHECK: all PASS. terraform plan: 1 change (eks_version 1.33->1.34). GO."
OTHER_EVIDENCE = "PRE-CHECK: all PASS. terraform plan: DIFFERENT plan. GO."


# ─── single-approver happy path ─────────────────────────────────────────

def test_single_approver_approves_and_unlocks():
    ag.record_request("1.34", "1.33", EVIDENCE, cluster_name="expense-dev", required_approvers=1)
    status = ag.record_approval("1.34", EVIDENCE, actor="alice", reason="lgtm")
    assert status == "APPROVED"
    assert ag.is_approved("1.34", EVIDENCE) is True


def test_not_approved_before_any_decision():
    ag.record_request("1.34", "1.33", EVIDENCE, cluster_name="expense-dev")
    assert ag.is_approved("1.34", EVIDENCE) is False


# ─── version binding ────────────────────────────────────────────────────

def test_approval_is_version_bound():
    ag.record_request("1.34", "1.33", EVIDENCE, cluster_name="expense-dev")
    ag.record_approval("1.34", EVIDENCE, actor="alice", reason="ok")
    # Approving 1.34 must NOT approve a different target.
    assert ag.is_approved("1.35", EVIDENCE) is False
    ok, reason = ag.approval_check("1.35", EVIDENCE)
    assert ok is False and "1.34" in reason


def test_cannot_approve_target_without_request():
    ag.record_request("1.34", "1.33", EVIDENCE, cluster_name="expense-dev")
    with pytest.raises(ValueError):
        ag.record_approval("1.35", EVIDENCE, actor="alice", reason="ok")


# ─── evidence-hash binding (drift detection) ────────────────────────────

def test_evidence_drift_refuses_approval():
    ag.record_request("1.34", "1.33", EVIDENCE, cluster_name="expense-dev")
    # Approver's regenerated evidence differs -> hash mismatch -> refuse.
    with pytest.raises(ValueError):
        ag.record_approval("1.34", OTHER_EVIDENCE, actor="alice", reason="ok")


def test_apply_check_fails_if_evidence_changes_after_approval():
    ag.record_request("1.34", "1.33", EVIDENCE, cluster_name="expense-dev")
    ag.record_approval("1.34", EVIDENCE, actor="alice", reason="ok")
    # Same target + APPROVED, but a later check with different evidence must fail.
    ok, reason = ag.approval_check("1.34", OTHER_EVIDENCE)
    assert ok is False and "evidence" in reason.lower()


# ─── two-person approval ────────────────────────────────────────────────

def test_two_person_needs_two_distinct_approvers():
    ag.record_request("1.34", "1.33", EVIDENCE, cluster_name="expense-prod", required_approvers=2)
    s1 = ag.record_approval("1.34", EVIDENCE, actor="alice", reason="ok")
    assert s1 == "PARTIALLY_APPROVED"
    assert ag.is_approved("1.34", EVIDENCE) is False
    s2 = ag.record_approval("1.34", EVIDENCE, actor="bob", reason="ok")
    assert s2 == "APPROVED"
    assert ag.is_approved("1.34", EVIDENCE) is True


def test_same_person_cannot_approve_twice():
    ag.record_request("1.34", "1.33", EVIDENCE, cluster_name="expense-prod", required_approvers=2)
    ag.record_approval("1.34", EVIDENCE, actor="alice", reason="ok")
    with pytest.raises(ValueError):
        ag.record_approval("1.34", EVIDENCE, actor="alice", reason="again")
    # Still not enough distinct approvers.
    assert ag.is_approved("1.34", EVIDENCE) is False


# ─── rejection ──────────────────────────────────────────────────────────

def test_rejection_blocks_apply():
    ag.record_request("1.34", "1.33", EVIDENCE, cluster_name="expense-dev")
    ag.record_rejection("1.34", actor="alice", reason="found deprecated API")
    assert ag.is_approved("1.34", EVIDENCE) is False


def test_cannot_approve_after_rejection():
    ag.record_request("1.34", "1.33", EVIDENCE, cluster_name="expense-dev")
    ag.record_rejection("1.34", actor="alice", reason="no")
    with pytest.raises(ValueError):
        ag.record_approval("1.34", EVIDENCE, actor="bob", reason="ok")


# ─── TTL expiry ─────────────────────────────────────────────────────────

def test_approval_expires_after_ttl(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "APPROVAL_TTL_MINUTES", 60)

    ag.record_request("1.34", "1.33", EVIDENCE, cluster_name="expense-dev")
    ag.record_approval("1.34", EVIDENCE, actor="alice", reason="ok")
    assert ag.is_approved("1.34", EVIDENCE) is True

    # Backdate the decision to 2 hours ago -> should now be expired.
    data = ag._load()
    data["current"]["decided_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat()
    ag._save(data)

    ok, reason = ag.approval_check("1.34", EVIDENCE)
    assert ok is False and "expired" in reason.lower()


def test_ttl_zero_never_expires(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "APPROVAL_TTL_MINUTES", 0)

    ag.record_request("1.34", "1.33", EVIDENCE, cluster_name="expense-dev")
    ag.record_approval("1.34", EVIDENCE, actor="alice", reason="ok")
    data = ag._load()
    data["current"]["decided_at"] = (
        datetime.now(timezone.utc) - timedelta(days=30)
    ).isoformat()
    ag._save(data)
    assert ag.is_approved("1.34", EVIDENCE) is True


# ─── reset ──────────────────────────────────────────────────────────────

def test_reset_clears_current_keeps_history():
    ag.record_request("1.34", "1.33", EVIDENCE, cluster_name="expense-dev")
    ag.record_approval("1.34", EVIDENCE, actor="alice", reason="ok")
    ag.reset()
    assert ag.get_current() is None
    assert ag.is_approved("1.34", EVIDENCE) is False
    # History retained for audit.
    data = ag._load()
    assert any(h["event"] == "RESET" for h in data["history"])


# ─── S8: AWS account / region binding ────────────────────────────────────

def test_approval_bound_to_account(monkeypatch):
    ag.record_request("1.34", "1.33", EVIDENCE, cluster_name="expense-dev",
                      aws_account_id="111111111111", region="us-east-1")
    ag.record_approval("1.34", EVIDENCE, actor="alice", reason="ok")
    # same account -> ok
    ok, _ = ag.approval_check("1.34", EVIDENCE, aws_account_id="111111111111", region="us-east-1")
    assert ok is True
    # different account -> refused
    ok, reason = ag.approval_check("1.34", EVIDENCE, aws_account_id="999999999999", region="us-east-1")
    assert ok is False and "account" in reason.lower()


def test_approval_bound_to_region(monkeypatch):
    ag.record_request("1.34", "1.33", EVIDENCE, cluster_name="expense-dev",
                      aws_account_id="111111111111", region="us-east-1")
    ag.record_approval("1.34", EVIDENCE, actor="alice", reason="ok")
    ok, reason = ag.approval_check("1.34", EVIDENCE, aws_account_id="111111111111", region="eu-west-1")
    assert ok is False and "region" in reason.lower()


def test_account_binding_backward_compatible(monkeypatch):
    # older record without account id -> account check is skipped, still approves
    ag.record_request("1.34", "1.33", EVIDENCE, cluster_name="expense-dev")  # no account/region
    ag.record_approval("1.34", EVIDENCE, actor="alice", reason="ok")
    ok, _ = ag.approval_check("1.34", EVIDENCE, aws_account_id="111111111111", region="us-east-1")
    assert ok is True
