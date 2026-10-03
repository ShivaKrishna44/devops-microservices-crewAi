"""
Unit tests for the additional cluster operations (launch / scale / addon /
teardown) and the generalized (operation, target) approval identity.

No AWS/kubectl/terraform — exercises the pure safety logic only, matching the
style of test_approval_gate.py / test_guardrails.py / test_preapply_gate.py.

Covers:
  * approval identity is bound to (operation, target), not just a version
  * upgrade behavior is unchanged (records still match with the old API)
  * cross-operation approvals never leak (a 'scale' approval can't authorize a
    'teardown', etc.)
  * per-operation guardrail sets block the right things
  * teardown requires two distinct approvers AND the cluster name typed twice,
    and refuses production-looking clusters
  * the gated executor tools refuse to run without a valid matching approval
"""
import pytest

import approval_gate as ag
import guardrails as gr
import gates


# ─── generalized approval identity ──────────────────────────────────────

def _ev(operation, target):
    return f"OPERATION={operation}\nTARGET={target}\nCLUSTER=expense-dev"


def test_scale_approval_identity_bound():
    ev = _ev("scale", "5")
    ag.record_request(target_version="", current_version="", evidence=ev,
                      cluster_name="expense-dev", operation="scale", target="5")
    status = ag.record_approval(target_version="", evidence=ev, actor="alice",
                                reason="ok", operation="scale", target="5")
    assert status == "APPROVED"
    ok, _ = ag.approval_check("", ev, operation="scale", target="5")
    assert ok is True


def test_scale_approval_does_not_authorize_different_count():
    ev = _ev("scale", "5")
    ag.record_request(target_version="", current_version="", evidence=ev,
                      cluster_name="expense-dev", operation="scale", target="5")
    ag.record_approval(target_version="", evidence=ev, actor="alice", reason="ok",
                       operation="scale", target="5")
    # A scale-to-5 approval must not authorize scale-to-9.
    ok, reason = ag.approval_check("", _ev("scale", "9"), operation="scale", target="9")
    assert ok is False


def test_scale_approval_does_not_authorize_teardown():
    """Cross-operation isolation: a scale approval can't authorize a teardown."""
    ev = _ev("scale", "5")
    ag.record_request(target_version="", current_version="", evidence=ev,
                      cluster_name="expense-dev", operation="scale", target="5")
    ag.record_approval(target_version="", evidence=ev, actor="alice", reason="ok",
                       operation="scale", target="5")
    ok, reason = ag.approval_check("", _ev("teardown", "expense-dev"),
                                   operation="teardown", target="expense-dev")
    assert ok is False and "teardown" in reason.lower()


def test_upgrade_behavior_unchanged_with_defaults():
    """Old-style upgrade calls (no operation kwarg) still work and match."""
    ev = "PRE-CHECK all PASS. plan 1.33->1.34. GO."
    ag.record_request("1.34", "1.33", ev, cluster_name="expense-dev")
    status = ag.record_approval("1.34", ev, actor="alice", reason="ok")
    assert status == "APPROVED"
    # Default operation is 'upgrade', target defaults to the version.
    assert ag.is_approved("1.34", ev) is True
    ok, _ = ag.approval_check("1.34", ev)
    assert ok is True


def test_launch_approval_identity():
    ev = _ev("launch", "1.34")
    ag.record_request(target_version="1.34", current_version="none", evidence=ev,
                      cluster_name="expense-dev", operation="launch", target="1.34")
    ag.record_approval(target_version="1.34", evidence=ev, actor="alice", reason="ok",
                       operation="launch", target="1.34")
    ok, _ = ag.approval_check("1.34", ev, operation="launch", target="1.34")
    assert ok is True
    # An 'upgrade' check for the same version must NOT be satisfied by a launch approval.
    ok2, _ = ag.approval_check("1.34", ev, operation="upgrade", target="1.34")
    assert ok2 is False


# ─── teardown two-person via gates.required_approvers ────────────────────

def test_teardown_always_requires_two_person_even_on_dev():
    assert gates.required_approvers("expense-dev", "teardown") == 2
    # non-teardown on dev is single-person
    assert gates.required_approvers("expense-dev", "scale") == 1


def test_teardown_needs_two_distinct_approvers():
    ev = _ev("teardown", "expense-dev")
    required = gates.required_approvers("expense-dev", "teardown")
    ag.record_request(target_version="", current_version="", evidence=ev,
                      cluster_name="expense-dev", required_approvers=required,
                      operation="teardown", target="expense-dev")
    s1 = ag.record_approval(target_version="", evidence=ev, actor="alice", reason="ok",
                            operation="teardown", target="expense-dev")
    assert s1 == "PARTIALLY_APPROVED"
    s2 = ag.record_approval(target_version="", evidence=ev, actor="bob", reason="ok",
                            operation="teardown", target="expense-dev")
    assert s2 == "APPROVED"


def test_preapply_gate_teardown_blocks_single_approver():
    ev = _ev("teardown", "expense-dev")
    ag.record_request(target_version="", current_version="", evidence=ev,
                      cluster_name="expense-dev", required_approvers=1,  # deliberately too low
                      operation="teardown", target="expense-dev")
    ag.record_approval(target_version="", evidence=ev, actor="alice", reason="ok",
                       operation="teardown", target="expense-dev")
    # Even though the record said required=1, the gate insists on 2 for teardown.
    ok, reason = gates.preapply_gate(target_version="", operation="teardown",
                                     target="expense-dev")
    assert ok is False and "2 distinct" in reason


def test_preapply_gate_teardown_allows_two_approvers():
    ev = _ev("teardown", "expense-dev")
    ag.record_request(target_version="", current_version="", evidence=ev,
                      cluster_name="expense-dev", required_approvers=2,
                      operation="teardown", target="expense-dev")
    ag.record_approval(target_version="", evidence=ev, actor="alice", reason="ok",
                       operation="teardown", target="expense-dev")
    ag.record_approval(target_version="", evidence=ev, actor="bob", reason="ok",
                       operation="teardown", target="expense-dev")
    ok, _ = gates.preapply_gate(target_version="", operation="teardown", target="expense-dev")
    assert ok is True


# ─── per-operation guardrails ────────────────────────────────────────────

def test_launch_guardrail_blocks_when_cluster_exists():
    rep = gr.run_launch_guardrails(
        target_version="1.34", expected_cluster="expense-dev", confirmed_cluster="expense-dev",
        region="us-east-1", cluster_status="ACTIVE", evidence="GO",
    )
    assert rep.blocked is True  # cluster already exists


def test_launch_guardrail_passes_when_absent():
    rep = gr.run_launch_guardrails(
        target_version="1.34", expected_cluster="expense-dev", confirmed_cluster="expense-dev",
        region="us-east-1", cluster_status="ABSENT", evidence="GO",
    )
    assert rep.blocked is False


def test_launch_guardrail_blocks_wrong_typed_cluster():
    rep = gr.run_launch_guardrails(
        target_version="1.34", expected_cluster="expense-dev", confirmed_cluster="oops",
        region="us-east-1", cluster_status="ABSENT", evidence="GO",
    )
    assert rep.blocked is True


def test_scale_guardrail_blocks_scale_to_zero():
    rep = gr.run_scale_guardrails(
        desired_nodes=0, expected_cluster="expense-dev", confirmed_cluster="expense-dev",
        region="us-east-1", cluster_status="ACTIVE", evidence="GO",
    )
    assert rep.blocked is True


def test_scale_guardrail_blocks_absurd_count():
    rep = gr.run_scale_guardrails(
        desired_nodes=9999, expected_cluster="expense-dev", confirmed_cluster="expense-dev",
        region="us-east-1", cluster_status="ACTIVE", evidence="GO",
    )
    assert rep.blocked is True


def test_scale_guardrail_passes_sane_count():
    rep = gr.run_scale_guardrails(
        desired_nodes=4, expected_cluster="expense-dev", confirmed_cluster="expense-dev",
        region="us-east-1", cluster_status="ACTIVE", evidence="GO",
    )
    assert rep.blocked is False


def test_scale_guardrail_blocks_when_cluster_not_active():
    rep = gr.run_scale_guardrails(
        desired_nodes=4, expected_cluster="expense-dev", confirmed_cluster="expense-dev",
        region="us-east-1", cluster_status="UPDATING", evidence="GO",
    )
    assert rep.blocked is True


def test_addon_guardrail_blocks_blank_name():
    rep = gr.run_addon_guardrails(
        addon_name="", expected_cluster="expense-dev", confirmed_cluster="expense-dev",
        region="us-east-1", cluster_status="ACTIVE", evidence="GO",
    )
    assert rep.blocked is True


def test_addon_guardrail_passes_named():
    rep = gr.run_addon_guardrails(
        addon_name="vpc-cni", expected_cluster="expense-dev", confirmed_cluster="expense-dev",
        region="us-east-1", cluster_status="ACTIVE", evidence="GO",
    )
    assert rep.blocked is False


def test_teardown_guardrail_blocks_single_confirmation():
    # second confirmation empty -> double-confirm fails
    rep = gr.run_teardown_guardrails(
        expected_cluster="expense-dev", confirmed_cluster="expense-dev", confirmed_cluster_2="",
        region="us-east-1", cluster_status="ACTIVE", evidence="GO",
    )
    assert rep.blocked is True


def test_teardown_guardrail_passes_double_confirmed_nonprod():
    rep = gr.run_teardown_guardrails(
        expected_cluster="expense-dev", confirmed_cluster="expense-dev",
        confirmed_cluster_2="expense-dev",
        region="us-east-1", cluster_status="ACTIVE", evidence="GO",
    )
    assert rep.blocked is False


def test_teardown_guardrail_blocks_production_cluster():
    rep = gr.run_teardown_guardrails(
        expected_cluster="expense-prod", confirmed_cluster="expense-prod",
        confirmed_cluster_2="expense-prod",
        region="us-east-1", cluster_status="ACTIVE", evidence="GO",
    )
    assert rep.blocked is True  # prod teardown refused at guardrail layer


# ─── gated executor tools refuse without a valid approval ────────────────

def _call(tool, **kw):
    fn = getattr(tool, "func", None) or tool
    return fn(**kw)


def test_launch_tool_blocks_without_approval():
    from tools.operation_tools import terraform_launch_cluster
    out = _call(terraform_launch_cluster, target_version="1.34")
    assert out.startswith("BLOCKED")


def test_scale_tool_blocks_without_approval():
    from tools.operation_tools import terraform_scale_nodegroup
    out = _call(terraform_scale_nodegroup, desired_nodes=4)
    assert out.startswith("BLOCKED")


def test_teardown_tool_blocks_with_only_one_approver():
    from tools.operation_tools import terraform_teardown_cluster
    ev = _ev("teardown", "expense-dev")
    ag.record_request(target_version="", current_version="", evidence=ev,
                      cluster_name="expense-dev", required_approvers=1,
                      operation="teardown", target="expense-dev")
    ag.record_approval(target_version="", evidence=ev, actor="alice", reason="ok",
                       operation="teardown", target="expense-dev")
    # Tool independently requires two-person for teardown -> still BLOCKED.
    out = _call(terraform_teardown_cluster, cluster_name="expense-dev")
    assert out.startswith("BLOCKED")


def test_scale_tool_blocks_when_approval_is_for_different_operation():
    """A launch approval must not let the scale tool run."""
    from tools.operation_tools import terraform_scale_nodegroup
    ev = _ev("launch", "1.34")
    ag.record_request(target_version="1.34", current_version="none", evidence=ev,
                      cluster_name="expense-dev", operation="launch", target="1.34")
    ag.record_approval(target_version="1.34", evidence=ev, actor="alice", reason="ok",
                       operation="launch", target="1.34")
    out = _call(terraform_scale_nodegroup, desired_nodes=4)
    assert out.startswith("BLOCKED")
