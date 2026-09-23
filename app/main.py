"""
Human-driven entrypoint for the EKS upgrade agent (guardrails + approval gates).

Layers of protection, in order:
  1. READ-ONLY pre-checks + terraform plan (agents) — produce evidence.
  2. Deterministic GUARDRAILS (non-LLM) — hard-stop on invalid jumps, wrong
     cluster, disallowed region, or an UNSAFE pre-check verdict.
  3. HUMAN GATE — the operator must type the exact cluster name AND type APPROVE.
     Production clusters can require TWO distinct approvers.
  4. Approval is bound to a hash of the reviewed evidence and expires (TTL).
  5. The apply tool re-verifies the approval record before touching anything.

Do not remove the prompts — they are the safety model.

Usage:
    python main.py --target-version 1.34
    python main.py --target-version 1.34 --actor alice --approve   # 1st approver (non-interactive)
    python main.py --target-version 1.34 --actor bob   --approve   # 2nd approver (prod, two-person)
    python main.py --apply --target-version 1.34                    # run apply after approvals recorded
    python main.py --status
    python main.py --reset
"""
import argparse
import subprocess
import sys

from config import settings, logger
import approval_gate
import guardrails
from gates import is_prod as _is_prod, required_approvers as _required_approvers, preapply_gate as _preapply_gate

# NOTE: crew.py imports CrewAI (heavy). We import it LAZILY inside the functions
# that actually run agents, so lightweight commands like --status / --reset work
# without CrewAI installed.
def _import_crew():
    from crew import run_precheck, run_execute_and_validate
    return run_precheck, run_execute_and_validate


def _banner(text: str) -> None:
    print("\n" + "=" * 64)
    print(text)
    print("=" * 64)


def _read_current_version() -> str:
    """Read the live control-plane version directly from AWS (stable, no tool wrapper)."""
    try:
        r = subprocess.run(
            f"aws eks describe-cluster --name {settings.CLUSTER_NAME} "
            f"--region {settings.AWS_REGION} --query cluster.version --output text",
            shell=True, capture_output=True, text=True, timeout=60,
        )
        v = r.stdout.strip()
        if r.returncode == 0 and v.count(".") == 1:
            return v
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read current EKS version: %s", exc)
    return "unknown"


def _read_account_id() -> str:
    """Read the current AWS account ID (for binding the approval). '' on failure."""
    try:
        r = subprocess.run(
            "aws sts get-caller-identity --query Account --output text",
            shell=True, capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read AWS account id: %s", exc)
    return ""


def _read_cluster_status() -> str:
    """Read the live cluster status directly from AWS.

    Returns e.g. 'ACTIVE', 'UPDATING', 'ABSENT' if no such cluster exists, or
    'ERROR' if the cluster can't be described for another reason. 'ABSENT' is
    distinguished from 'ERROR' because launch needs to know the cluster does
    NOT exist yet.
    """
    try:
        r = subprocess.run(
            f"aws eks describe-cluster --name {settings.CLUSTER_NAME} "
            f"--region {settings.AWS_REGION} --query cluster.status --output text",
            shell=True, capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
        # ResourceNotFoundException => the cluster doesn't exist (safe to launch).
        err = (r.stderr or "").lower()
        if "resourcenotfound" in err or "no cluster found" in err or "not found" in err:
            return "ABSENT"
        return "ERROR"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read cluster status: %s", exc)
        return "ERROR"


def show_status() -> int:
    cur = approval_gate.get_current()
    _banner("APPROVAL GATE STATUS")
    if not cur:
        print("No pending or approved upgrade on record.")
        return 0
    print(f"Cluster        : {cur.get('cluster_name')}")
    print(f"Target version : {cur.get('target_version')}")
    print(f"From version   : {cur.get('current_version')}")
    print(f"Status         : {cur.get('status')}")
    print(f"Approvers      : {len(cur.get('approvers', []))}/{cur.get('required_approvers', 1)}")
    for a in cur.get("approvers", []):
        print(f"   - {a['actor']} @ {a['at']}: {a['reason']}")
    print(f"Requested at   : {cur.get('requested_at')}")
    print(f"Decided at     : {cur.get('decided_at')}")
    return 0


def request_and_check(target_version: str, actor: str) -> tuple:
    """Phase 1+2+guardrails: run pre-checks, apply guardrails, open the request.

    Returns (return_code, evidence_or_None).
    """
    _banner(f"EKS UPGRADE — PRE-CHECK & GUARDRAILS — TARGET {target_version}")

    current_version = _read_current_version()
    cluster = settings.CLUSTER_NAME

    # Deterministic pre-flight: cluster must exist and be ACTIVE (not mid-update).
    # Don't rely solely on the LLM to run this — check it directly and hard-stop.
    status = _read_cluster_status()
    if status != "ACTIVE":
        print(f"ABORT: cluster '{cluster}' status is '{status}', not ACTIVE. "
              f"An update may be in progress, or the name/account/region is wrong. "
              f"Nothing recorded, nothing applied.")
        return 1, None

    print("\n[1/4] Running read-only pre-checks and building the plan...\n")
    run_precheck, _ = _import_crew()
    evidence = run_precheck(target_version)
    print("\n----- PRE-CHECK & PLAN EVIDENCE -----")
    print(evidence)
    print("----- END EVIDENCE -----\n")

    # Deterministically capture a health baseline (don't rely on the LLM to do it),
    # so the post-upgrade regression check has something to compare against.
    from tools.health_tools import _collect_health, _baseline_path
    import json as _json
    try:
        snap = _collect_health()
        snap["label"] = "pre-upgrade"
        with open(_baseline_path(), "w", encoding="utf-8") as f:
            _json.dump(snap, f, indent=2)
        print(f"[baseline] captured: {len(snap['nodes'])} nodes, "
              f"{len(snap['unhealthy_pods'])} pre-existing unhealthy pods.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not capture health baseline: %s", exc)
        print("[baseline] WARN: could not capture baseline; regression check will be limited.")

    # ── Typed cluster-name confirmation (prevents wrong-cluster upgrades) ──
    print(f"[2/4] Guardrails. You are targeting cluster: {cluster} (region {settings.AWS_REGION})")
    confirmed = input(f"Type the cluster name to confirm you mean it: ").strip()

    report = guardrails.run_all_guardrails(
        current_version=current_version,
        target_version=target_version,
        expected_cluster=cluster,
        confirmed_cluster=confirmed,
        region=settings.AWS_REGION,
        evidence=evidence,
    )
    print("\n" + report.render() + "\n")

    if report.blocked:
        print("Guardrails BLOCKED this upgrade. Nothing recorded, nothing applied.")
        return 1, None

    required = _required_approvers(cluster)
    if required > 1:
        print(f"NOTE: '{cluster}' is production — {required} distinct approvers required.")

    approval_gate.record_request(target_version, current_version, evidence,
                                 cluster_name=cluster, required_approvers=required,
                                 aws_account_id=_read_account_id(), region=settings.AWS_REGION)
    print(f"\nRequest opened for {current_version} -> {target_version} on {cluster}.")
    print(f"Approve with: python main.py --target-version {target_version} --actor <name> --approve")
    print(f"Then apply with: python main.py --apply --target-version {target_version}")
    # Return the evidence so a same-session approver can approve without
    # re-running the pre-check.
    return 0, evidence


def record_approval_interactive(target_version: str, actor: str, non_interactive: bool,
                                evidence: str = None) -> int:
    """Record one human approval.

    Hash-only model: the raw plan is NOT stored. To approve, we need evidence
    whose hash matches the request. If `evidence` isn't supplied (e.g. a second
    approver in a separate process), we RE-RUN the read-only pre-check and let
    the gate verify the freshly-generated evidence hashes to the same value.
    If the cluster/plan drifted since the request, the hash won't match and the
    approval is correctly refused.
    """
    cur = approval_gate.get_current()
    if not cur or cur.get("target_version") != target_version:
        print(f"No pending request for {target_version}. Run the pre-check flow first.")
        return 1

    _banner("HUMAN APPROVAL")
    print(f"Cluster: {cur.get('cluster_name')}  |  {cur.get('current_version')} -> {target_version}")
    print("This is IRREVERSIBLE.\n")

    # Get evidence to hash against: reuse the just-generated one, or regenerate.
    if evidence is None:
        print("Re-running read-only pre-checks to verify the plan hasn't changed...\n")
        run_precheck, _ = _import_crew()
        evidence = run_precheck(target_version)
        print("\n----- PRE-CHECK & PLAN EVIDENCE -----")
        print(evidence)
        print("----- END EVIDENCE -----\n")

    if non_interactive:
        decision, reason = "APPROVE", f"approved non-interactively by {actor} (--approve)"
    else:
        decision = input("Type APPROVE to approve, anything else to reject: ").strip()
        reason = input("Reason: ").strip() if decision == "APPROVE" else "rejected at prompt"

    if decision != "APPROVE":
        approval_gate.record_rejection(target_version, actor=actor, reason=reason)
        print("Rejected. Nothing will be applied.")
        return 1

    try:
        status = approval_gate.record_approval(
            target_version, evidence=evidence, actor=actor, reason=reason,
        )
    except ValueError as e:
        # Most likely: evidence hash changed (plan drifted) or duplicate approver.
        print(f"Approval refused: {e}")
        return 1

    print(f"Approval recorded by {actor}. Status: {status}")
    if status == "APPROVED":
        print(f"Ready to apply: python main.py --apply --target-version {target_version}")
    else:
        print("More approvers required before apply is unlocked.")
    return 0


def do_apply(target_version: str) -> int:
    """Deterministic apply path.

    The pass/fail decision is made from the executor tool's OWN return strings
    (SUCCESS / COMPLETED WITH ALARM / BLOCKED / FAILED), never from an LLM
    summary — so a degraded or failed upgrade can't be narrated into a success.
    Evidence-hash + TTL are re-verified here (regenerating fresh evidence) so
    plan drift between approval and apply is actually caught.
    """
    ok, reason = _preapply_gate(target_version)
    if not ok:
        print(f"Apply BLOCKED: {reason}")
        return 1

    _banner(f"APPLYING EKS UPGRADE -> {target_version}")

    # Re-verify the approval against FRESH evidence (drift detection) + TTL.
    print("Re-verifying approval against current cluster state (drift + TTL)...")
    fresh_evidence = run_precheck(target_version)
    verified, vreason = approval_gate.approval_check(
        target_version, fresh_evidence,
        aws_account_id=_read_account_id(), region=settings.AWS_REGION,
    )
    if not verified:
        print(f"Apply BLOCKED: approval no longer valid — {vreason}. "
              f"(Plan may have drifted since approval, or the approval expired.) "
              f"Re-run the pre-check + approval flow.")
        return 1

    print("[3/4] Approval verified (hash + TTL). Executing (control plane, then nodes)...\n")
    # Call the deterministic tools directly — do NOT rely on the LLM summary
    # for the success/failure decision.
    from tools.upgrade_tools import terraform_apply_upgrade, verify_eks_version
    from tools.health_tools import wait_for_healthy, compare_to_baseline

    def _run_tool(tool, **kw):
        fn = getattr(tool, "func", None) or getattr(tool, "_run", None) or tool
        return fn(**kw)

    apply_result = _run_tool(terraform_apply_upgrade, target_version=target_version)
    _banner("APPLY RESULT")
    print(apply_result)

    # Deterministic decision from the tool's own status prefix.
    if apply_result.startswith("BLOCKED") or "FAILED" in apply_result:
        print("\nApply did not succeed — review the output above.")
        return 1
    if apply_result.startswith("COMPLETED WITH ALARM"):
        print("\n🚨 Upgrade applied but an availability breach occurred during rollover. "
              "Treating as FAILURE — investigate before considering this done.")
        return 1

    # Post-upgrade validation (deterministic).
    _banner("POST-UPGRADE VALIDATION")
    ver = _run_tool(verify_eks_version, target_version=target_version)
    print(ver)
    health = _run_tool(wait_for_healthy)
    print(health)
    regression = _run_tool(compare_to_baseline)
    print(regression)

    if any(s.startswith("ALERT") for s in (ver, health, regression)) or \
       any("ALERT" in s for s in (ver, health, regression)):
        print("\nPost-upgrade validation FAILED — review the output above.")
        return 1

    print("\nUpgrade workflow complete and validated.")
    return 0


# ══════════════════════════════════════════════════════════════════════
# Additional cluster operations: launch / scale / addon / teardown.
#
# Every one reuses the SAME approval gate and the SAME typed prompts as
# upgrade. The only differences are the operation-specific guardrail set, the
# operation-specific "target" the approval is bound to, and the gated executor
# tool that runs. There is deliberately NO auto-approve / skip path.
# ══════════════════════════════════════════════════════════════════════

# Human-readable description of what each op's "target" means.
_OP_TARGET_LABEL = {
    "launch": "version",
    "scale": "desired node count",
    "addon": "addon name",
    "teardown": "cluster name",
}


def _op_target(operation: str, args) -> str:
    """Resolve the operation-specific target from CLI args."""
    if operation == "launch":
        return args.target_version
    if operation == "scale":
        return str(args.nodes)
    if operation == "addon":
        return (args.addon or "").strip()
    if operation == "teardown":
        return settings.CLUSTER_NAME
    return args.target_version


def _build_op_evidence(operation: str, target: str) -> str:
    """Deterministic, human-readable evidence string for a non-upgrade op.

    Unlike upgrade (which uses the terraform plan from the crew), these ops bind
    the approval to a stable description of exactly what will run. It is still
    SHA-256 hashed and drift-checked identically.
    """
    return (f"OPERATION={operation}\nCLUSTER={settings.CLUSTER_NAME}\n"
            f"REGION={settings.AWS_REGION}\nTARGET={target}\n"
            f"TERRAFORM_DIR={settings.TERRAFORM_DIR}\n"
            f"This authorizes a Terraform-driven '{operation}' with the above parameters only.")


def request_and_check_op(operation: str, args) -> tuple:
    """Pre-check + guardrails + open request for a non-upgrade operation.

    Returns (return_code, evidence_or_None).
    """
    import guardrails as _gr
    from gates import required_approvers as _req

    target = _op_target(operation, args)
    cluster = settings.CLUSTER_NAME
    label = _OP_TARGET_LABEL.get(operation, "target")

    if not target:
        print(f"ABORT: {operation} requires a {label}. Nothing recorded.")
        return 1, None

    _banner(f"EKS {operation.upper()} — GUARDRAILS — {label.upper()}: {target}")

    status = _read_cluster_status()
    print(f"Cluster '{cluster}' (region {settings.AWS_REGION}) status: {status}")

    evidence = _build_op_evidence(operation, target)
    print("\n----- OPERATION EVIDENCE (bound to approval) -----")
    print(evidence)
    print("----- END EVIDENCE -----\n")

    # Typed cluster-name confirmation (teardown demands it twice).
    confirmed = input("Type the cluster name to confirm you mean it: ").strip()
    confirmed_2 = ""
    if operation == "teardown":
        print("TEARDOWN is IRREVERSIBLE and destroys the entire cluster.")
        confirmed_2 = input("Type the cluster name AGAIN to confirm teardown: ").strip()

    if operation == "launch":
        report = _gr.run_launch_guardrails(
            target_version=target, expected_cluster=cluster, confirmed_cluster=confirmed,
            region=settings.AWS_REGION, cluster_status=status, evidence=evidence,
        )
    elif operation == "scale":
        report = _gr.run_scale_guardrails(
            desired_nodes=target, expected_cluster=cluster, confirmed_cluster=confirmed,
            region=settings.AWS_REGION, cluster_status=status, evidence=evidence,
        )
    elif operation == "addon":
        report = _gr.run_addon_guardrails(
            addon_name=target, expected_cluster=cluster, confirmed_cluster=confirmed,
            region=settings.AWS_REGION, cluster_status=status, evidence=evidence,
        )
    elif operation == "teardown":
        report = _gr.run_teardown_guardrails(
            expected_cluster=cluster, confirmed_cluster=confirmed, confirmed_cluster_2=confirmed_2,
            region=settings.AWS_REGION, cluster_status=status, evidence=evidence,
        )
    else:
        print(f"ABORT: unknown operation '{operation}'.")
        return 1, None

    print("\n" + report.render() + "\n")
    if report.blocked:
        print(f"Guardrails BLOCKED this {operation}. Nothing recorded, nothing applied.")
        return 1, None

    required = _req(cluster, operation)
    if required > 1:
        print(f"NOTE: '{operation}' on '{cluster}' requires {required} distinct approvers.")

    approval_gate.record_request(
        target_version=(target if operation == "launch" else ""),
        current_version=_read_current_version() if operation != "launch" else "none",
        evidence=evidence, cluster_name=cluster, required_approvers=required,
        aws_account_id=_read_account_id(), region=settings.AWS_REGION,
        operation=operation, target=target,
    )
    print(f"\n{operation.upper()} request opened for {label}={target} on {cluster}.")
    print(f"Approve with: python main.py --operation {operation} "
          f"{_approve_hint(operation, args)} --actor <name> --approve")
    print(f"Then apply with: python main.py --operation {operation} "
          f"{_approve_hint(operation, args)} --apply")
    return 0, evidence


def _approve_hint(operation: str, args) -> str:
    if operation == "launch":
        return f"--target-version {args.target_version}"
    if operation == "scale":
        return f"--nodes {args.nodes}"
    if operation == "addon":
        return f"--addon {args.addon}"
    return ""  # teardown needs no extra arg (cluster comes from env)


def record_approval_op(operation: str, args, non_interactive: bool, evidence: str = None) -> int:
    """Record one human approval for a non-upgrade operation."""
    target = _op_target(operation, args)
    cur = approval_gate.get_current()
    rec_op = (cur or {}).get("operation", "upgrade")
    rec_target = (cur or {}).get("target", (cur or {}).get("target_version"))
    if not cur or rec_op != operation or rec_target != target:
        print(f"No pending {operation} request for {target}. Run the pre-check flow first.")
        return 1

    _banner(f"HUMAN APPROVAL — {operation.upper()}")
    print(f"Cluster: {cur.get('cluster_name')}  |  {operation} target: {target}")
    if operation == "teardown":
        print("This DESTROYS the cluster and is IRREVERSIBLE.\n")
    else:
        print("This changes live infrastructure.\n")

    if evidence is None:
        evidence = _build_op_evidence(operation, target)

    if non_interactive:
        decision, reason = "APPROVE", f"approved non-interactively by {args.actor} (--approve)"
    else:
        decision = input("Type APPROVE to approve, anything else to reject: ").strip()
        reason = input("Reason: ").strip() if decision == "APPROVE" else "rejected at prompt"

    if decision != "APPROVE":
        approval_gate.record_rejection(target_version="", actor=args.actor, reason=reason,
                                       operation=operation, target=target)
        print("Rejected. Nothing will be applied.")
        return 1

    try:
        status = approval_gate.record_approval(
            target_version="", evidence=evidence, actor=args.actor, reason=reason,
            operation=operation, target=target,
        )
    except ValueError as e:
        print(f"Approval refused: {e}")
        return 1

    print(f"Approval recorded by {args.actor}. Status: {status}")
    if status == "APPROVED":
        print(f"Ready to apply: python main.py --operation {operation} "
              f"{_approve_hint(operation, args)} --apply")
    else:
        print("More approvers required before apply is unlocked.")
    return 0


def do_apply_op(operation: str, args) -> int:
    """Deterministic apply path for a non-upgrade operation.

    Pass/fail comes from the executor tool's own SUCCESS/BLOCKED/FAILED prefix,
    never from an LLM. The pre-apply gate + a fresh approval_check re-verify the
    (operation, target) approval (hash + TTL + approver count) before executing.
    """
    from gates import preapply_gate as _preapply
    target = _op_target(operation, args)

    ok, reason = _preapply(target_version=(target if operation == "launch" else ""),
                           operation=operation, target=target)
    if not ok:
        print(f"Apply BLOCKED: {reason}")
        return 1

    _banner(f"APPLYING EKS {operation.upper()} -> {target}")

    # Re-verify against fresh (deterministic) evidence + TTL + account/region.
    fresh_evidence = _build_op_evidence(operation, target)
    verified, vreason = approval_gate.approval_check(
        target_version="", evidence=fresh_evidence,
        aws_account_id=_read_account_id(), region=settings.AWS_REGION,
        operation=operation, target=target,
    )
    if not verified:
        print(f"Apply BLOCKED: approval no longer valid — {vreason}.")
        return 1

    print(f"[apply] Approval verified (hash + TTL). Executing '{operation}'...\n")
    from tools.operation_tools import (
        terraform_launch_cluster, terraform_scale_nodegroup,
        terraform_update_addon, terraform_teardown_cluster,
    )

    def _run_tool(tool, **kw):
        fn = getattr(tool, "func", None) or getattr(tool, "_run", None) or tool
        return fn(**kw)

    if operation == "launch":
        result = _run_tool(terraform_launch_cluster, target_version=target)
    elif operation == "scale":
        result = _run_tool(terraform_scale_nodegroup, desired_nodes=int(target))
    elif operation == "addon":
        result = _run_tool(terraform_update_addon, addon_name=target)
    elif operation == "teardown":
        result = _run_tool(terraform_teardown_cluster, cluster_name=target)
    else:
        print(f"Apply BLOCKED: unknown operation '{operation}'.")
        return 1

    _banner("APPLY RESULT")
    print(result)

    if result.startswith("BLOCKED") or "FAILED" in result:
        print(f"\n{operation} did not succeed — review the output above.")
        return 1
    print(f"\n{operation} workflow complete.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Human-approved EKS cluster agent (guardrails + gates)")
    p.add_argument("--operation", default="upgrade",
                   choices=["upgrade", "launch", "scale", "addon", "teardown"],
                   help="Cluster operation to perform (default: upgrade)")
    p.add_argument("--target-version", help="Target K8s version, e.g. 1.34 (upgrade/launch)")
    p.add_argument("--nodes", type=int, help="Desired node count (scale)")
    p.add_argument("--addon", help="Addon name (addon)")
    p.add_argument("--actor", default="operator", help="Who is acting (for the audit log)")
    p.add_argument("--approve", action="store_true", help="Record an approval non-interactively")
    p.add_argument("--apply", action="store_true", help="Run the apply (after approvals recorded)")
    p.add_argument("--status", action="store_true", help="Show current approval gate status")
    p.add_argument("--reset", action="store_true", help="Reset the approval gate")
    args = p.parse_args()

    if args.status:
        return show_status()
    if args.reset:
        approval_gate.reset()
        print("Approval gate reset.")
        return 0

    operation = args.operation

    # Validate operation-specific required args early.
    if operation in ("upgrade", "launch") and not args.target_version:
        p.error(f"--target-version is required for {operation} (e.g. --target-version 1.34)")
    if operation == "scale" and args.nodes is None:
        p.error("--nodes is required for scale (e.g. --nodes 4)")
    if operation == "addon" and not args.addon:
        p.error("--addon is required for addon (e.g. --addon vpc-cni)")

    try:
        # ── Upgrade: unchanged original path ──────────────────────────────
        if operation == "upgrade":
            if args.apply:
                return do_apply(args.target_version)
            if args.approve:
                return record_approval_interactive(args.target_version, args.actor,
                                                   non_interactive=True)
            rc, evidence = request_and_check(args.target_version, args.actor)
            if rc != 0:
                return rc
            return record_approval_interactive(
                args.target_version, args.actor, non_interactive=False, evidence=evidence,
            )

        # ── Other operations: launch / scale / addon / teardown ───────────
        if args.apply:
            return do_apply_op(operation, args)
        if args.approve:
            return record_approval_op(operation, args, non_interactive=True)
        rc, evidence = request_and_check_op(operation, args)
        if rc != 0:
            return rc
        return record_approval_op(operation, args, non_interactive=False, evidence=evidence)
    except KeyboardInterrupt:
        print("\nInterrupted. No changes made beyond what is shown above.")
        return 130
    except Exception as exc:  # noqa: BLE001
        logger.error("Workflow error: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
