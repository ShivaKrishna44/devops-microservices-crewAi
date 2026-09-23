"""
Cluster operation executor tools (launch / scale / addon / teardown).

These mirror the safety model of `upgrade_tools.terraform_apply_upgrade`:
every one is APPROVAL-GATED and re-verifies the persisted approval record
against its own (operation, target) identity before touching anything. None of
them trust agent-provided text — they validate the record the human created.

All are Terraform-driven (same TERRAFORM_DIR as upgrade), so infrastructure
stays declaratively managed. Each returns one of the standard status prefixes
the deterministic apply path keys off:

    SUCCESS  | BLOCKED | FAILED

(Only the phased upgrade path can produce COMPLETED WITH ALARM; these simpler
operations do not run the live availability monitor, so they never emit it.)

Blast-radius note: `terraform_teardown_cluster` DESTROYS the cluster and is
irreversible. It re-checks that the approval is a two-person 'teardown'
approval for THIS cluster before running, in addition to the CLI-side guardrails.
"""
import subprocess

try:
    from crewai.tools import tool  # production
except Exception:  # noqa: BLE001 - test env without CrewAI
    try:
        from _toolshim import tool          # when tools/ is on sys.path (tests)
    except ImportError:
        from tools._toolshim import tool     # when imported as a package (app)

from config import settings, logger
import approval_gate


def _run_terraform(args: str, timeout: int) -> str:
    """Run a terraform command inside the configured Terraform dir. Never raises.

    Kept local (not imported from upgrade_tools) so this module has no import-time
    dependency on the upgrade path and stays independently testable.
    """
    import os
    tf_dir = settings.TERRAFORM_DIR
    if not os.path.isdir(tf_dir):
        return f"ERROR: terraform dir not found: {tf_dir}"
    try:
        result = subprocess.run(
            f"terraform {args}",
            shell=True, cwd=tf_dir, capture_output=True, text=True, timeout=timeout,
        )
        out = (result.stdout or "") + (result.stderr or "")
        if len(out) > 15000:
            out = out[:15000] + "\n... (output truncated)"
        if result.returncode != 0:
            return f"ERROR (exit {result.returncode}):\n{out.strip()}"
        return out.strip()
    except subprocess.TimeoutExpired:
        return f"ERROR: terraform {args.split()[0]} timed out after {timeout}s"
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"


def _verify_op_against_store(operation: str, target: str,
                             require_two_person: bool = False) -> tuple[bool, str]:
    """Verify the current approval record for a specific (operation, target).

    Independent of externally-supplied evidence — validates the record the
    human created, mirroring upgrade_tools._verify_against_store but keyed on
    the generalized (operation, target) identity.
    """
    cur = approval_gate.get_current()
    if not cur:
        return False, "no approval on record"
    rec_op = cur.get("operation", "upgrade")
    rec_target = cur.get("target", cur.get("target_version"))
    if rec_op != operation or rec_target != target:
        return False, f"approval on file is for {rec_op}/{rec_target}, not {operation}/{target}"
    if cur.get("status") != "APPROVED":
        return False, f"status is {cur.get('status')}, not APPROVED"

    distinct = len({a.get("actor") for a in cur.get("approvers", [])})
    needed = cur.get("required_approvers", 1)
    if require_two_person:
        needed = max(needed, 2)
    if distinct < needed:
        return False, f"needs {needed} distinct approver(s), only {distinct} recorded"

    # TTL — reuse the window approval_gate enforces.
    from datetime import datetime, timezone, timedelta
    ttl = settings.APPROVAL_TTL_MINUTES
    decided_at = cur.get("decided_at")
    if ttl and decided_at:
        try:
            decided = datetime.fromisoformat(decided_at)
            if datetime.now(timezone.utc) - decided > timedelta(minutes=ttl):
                return False, f"approval expired (older than {ttl} min) — re-approve"
        except ValueError:
            return False, "approval timestamp unreadable — re-approve"
    return True, "approved"


@tool("Terraform Launch EKS Cluster (Approval Gated)")
def terraform_launch_cluster(target_version: str) -> str:
    """Create a NEW EKS cluster at the given version — ONLY if humans approved.

    Gated on a 'launch' approval for this version. Terraform-driven: applies the
    full configuration in TERRAFORM_DIR with -var eks_version=<target>. Returns
    SUCCESS / BLOCKED / FAILED.
    """
    logger.info("terraform launch requested for eks_version=%s", target_version)
    ok, reason = _verify_op_against_store("launch", target_version)
    if not ok:
        return f"BLOCKED: {reason}. Refusing to launch a cluster. Nothing changed."

    out = _run_terraform(
        f'apply -input=false -auto-approve -no-color -var="eks_version={target_version}"',
        timeout=3000,  # full cluster create is slow
    )
    if out.startswith("ERROR"):
        return (f"terraform launch FAILED for a new cluster at {target_version}: {out}\n"
                f"Check partial resources; you may need `terraform destroy` to clean up.")
    return (f"SUCCESS: EKS cluster launched at {target_version} via Terraform.\n{out}")


@tool("Terraform Scale Node Group (Approval Gated)")
def terraform_scale_nodegroup(desired_nodes: int) -> str:
    """Scale the managed node group to a desired node count — ONLY if approved.

    Gated on a 'scale' approval whose target is the desired node count. Drives
    Terraform with -var desired_size=<n>. Returns SUCCESS / BLOCKED / FAILED.
    """
    target = str(desired_nodes)
    logger.info("terraform scale requested to desired_size=%s", target)
    ok, reason = _verify_op_against_store("scale", target)
    if not ok:
        return f"BLOCKED: {reason}. Refusing to scale node group. Nothing changed."

    out = _run_terraform(
        f'apply -input=false -auto-approve -no-color -var="desired_size={target}"',
        timeout=1800,
    )
    if out.startswith("ERROR"):
        return f"terraform scale FAILED (desired_size={target}): {out}"
    return f"SUCCESS: node group scaled to desired_size={target} via Terraform.\n{out}"


@tool("Terraform Update Addon (Approval Gated)")
def terraform_update_addon(addon_name: str) -> str:
    """Update a specific cluster addon — ONLY if humans approved this addon.

    Gated on an 'addon' approval whose target is the addon name. Targets just the
    addon resource so the apply is scoped. Returns SUCCESS / BLOCKED / FAILED.
    """
    name = (addon_name or "").strip()
    logger.info("terraform addon update requested for '%s'", name)
    ok, reason = _verify_op_against_store("addon", name)
    if not ok:
        return f"BLOCKED: {reason}. Refusing to update addon. Nothing changed."

    # Scope the apply to the addon resources so unrelated infra isn't touched.
    out = _run_terraform(
        f'apply -input=false -auto-approve -no-color -target=aws_eks_addon.{name}',
        timeout=1200,
    )
    if out.startswith("ERROR"):
        return f"terraform addon update FAILED for '{name}': {out}"
    return f"SUCCESS: addon '{name}' updated via Terraform.\n{out}"


@tool("Terraform Teardown EKS Cluster (Approval Gated, Two-Person)")
def terraform_teardown_cluster(cluster_name: str) -> str:
    """DESTROY the EKS cluster — irreversible. ONLY with a two-person approval.

    Gated on a 'teardown' approval for THIS cluster name that carries TWO
    distinct approvers. Runs `terraform destroy`. Returns SUCCESS / BLOCKED /
    FAILED. This is the highest-blast-radius operation in the system.
    """
    logger.info("terraform TEARDOWN requested for cluster=%s", cluster_name)
    # Teardown independently insists on two-person here, on top of the gate/CLI.
    ok, reason = _verify_op_against_store("teardown", cluster_name, require_two_person=True)
    if not ok:
        return f"BLOCKED: {reason}. Refusing to tear down '{cluster_name}'. Nothing changed."

    out = _run_terraform(
        "destroy -input=false -auto-approve -no-color",
        timeout=3000,
    )
    if out.startswith("ERROR"):
        return (f"terraform teardown FAILED for '{cluster_name}': {out}\n"
                f"The cluster may be partially destroyed — inspect state before retrying.")
    return f"SUCCESS: cluster '{cluster_name}' torn down via Terraform destroy.\n{out}"
