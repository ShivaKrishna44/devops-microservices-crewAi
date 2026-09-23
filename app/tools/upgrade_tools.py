"""
EKS upgrade executor tools.

`terraform plan` is safe and read-only-ish (no changes applied).
`terraform apply` is GATED: it refuses to run unless a valid approval token
exists for the exact target version that was reviewed. This is the core safety
mechanism — the agent cannot apply an upgrade the human did not approve.
"""
import os
import subprocess

try:
    from crewai.tools import tool  # production
except Exception:  # noqa: BLE001 - test env without CrewAI
    from _toolshim import tool

from config import settings, logger
import approval_gate


def _run_terraform(args: str, timeout: int) -> str:
    """Run a terraform command inside the configured Terraform dir. Never raises."""
    tf_dir = settings.TERRAFORM_DIR
    if not os.path.isdir(tf_dir):
        return f"ERROR: terraform dir not found: {tf_dir}"
    try:
        result = subprocess.run(
            f"terraform {args}",
            shell=True,
            cwd=tf_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (result.stdout or "") + (result.stderr or "")
        # Guard the context window — terraform output can be huge.
        if len(out) > 15000:
            out = out[:15000] + "\n... (output truncated)"
        if result.returncode != 0:
            return f"ERROR (exit {result.returncode}):\n{out.strip()}"
        return out.strip()
    except subprocess.TimeoutExpired:
        return f"ERROR: terraform {args.split()[0]} timed out after {timeout}s"
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"


@tool("Terraform Init")
def terraform_init() -> str:
    """Initialize the Terraform working directory (providers, modules, backend).

    Safe to run repeatedly. Required before plan/apply.
    """
    logger.info("terraform init")
    out = _run_terraform("init -input=false", timeout=300)
    if out.startswith("ERROR"):
        return f"terraform init failed: {out}"
    return "PASS: terraform initialized."


@tool("Terraform Plan EKS Upgrade")
def terraform_plan_upgrade(target_version: str) -> str:
    """Run `terraform plan` for the EKS version upgrade WITHOUT applying anything.

    Passes the target version as -var eks_version=<target>. Returns the plan
    output so the human can review exactly what will change before approving.
    Read-only: makes no changes to infrastructure.
    """
    logger.info("terraform plan for eks_version=%s", target_version)
    out = _run_terraform(
        f'plan -input=false -no-color -var="eks_version={target_version}"',
        timeout=600,
    )
    if out.startswith("ERROR"):
        return f"terraform plan failed: {out}"
    return f"terraform plan for target {target_version}:\n{out}"


@tool("Terraform Apply EKS Upgrade (Approval Gated)")
def terraform_apply_upgrade(target_version: str) -> str:
    """Apply the EKS version upgrade — ONLY if humans approved this exact target.

    Defense in depth, in order:
      1. There must be a current approval record for THIS target version.
      2. Its status must be APPROVED (enough distinct approvers), evidence hash
         must still match the reviewed plan, and it must not be expired (TTL).
      3. A final re-run of the deterministic guardrails against the recorded
         evidence — if anything now blocks, refuse.
    Any failure returns BLOCKED and touches nothing.

    The evidence to verify against is read back from the approval store (it was
    persisted at request time), so the check is bound to exactly what the human
    reviewed — the agent cannot substitute its own text.
    """
    logger.info("terraform apply requested for eks_version=%s", target_version)

    cur = approval_gate.get_current()
    if not cur:
        return (f"BLOCKED: no approval request on record for {target_version}. "
                f"A human must run the pre-check + approval flow first. Nothing applied.")

    # Verify directly against the stored approval record. The executor never
    # trusts agent-provided text — it validates the record the human created.
    ok, reason = _verify_against_store(target_version)
    if not ok:
        return (f"BLOCKED: {reason}. Refusing to apply {target_version}. "
                f"Nothing changed.")

    logger.info("Approval + guardrails verified for %s — proceeding with PHASED apply.", target_version)

    # ── PHASE 1: control plane only ──────────────────────────────────────
    # Target the EKS cluster module so the control plane upgrades first. Node
    # groups are intentionally NOT touched yet.
    logger.info("Phase 1/2: upgrading control plane...")
    p1 = _run_terraform(
        f'apply -input=false -auto-approve -no-color '
        f'-target=module.eks.aws_eks_cluster.this '
        f'-var="eks_version={target_version}"',
        timeout=1800,
    )
    if p1.startswith("ERROR"):
        return (f"terraform apply FAILED in PHASE 1 (control plane) for {target_version}: {p1}\n"
                f"Node groups were NOT touched. Investigate before retrying.")

    # ── SEQUENCE GATE: control plane must report the new version before nodes ──
    cp_ok, cp_msg = _control_plane_ready(target_version)
    if not cp_ok:
        return (f"HALTED after PHASE 1: {cp_msg}. Control plane apply ran but did not settle "
                f"on {target_version}. NOT proceeding to node groups. Investigate.")
    logger.info("Control plane on %s — proceeding to node groups.", target_version)

    # ── PHASE 2: node groups (rolling replacement) — with LIVE monitoring ──
    # The node rollover drains/replaces nodes. We run terraform in a background
    # thread while a live availability monitor polls in the foreground and sounds
    # the alarm the MOMENT a critical deployment drops below its floor.
    logger.info("Phase 2/2: upgrading managed node groups (live availability monitor ON)...")
    import threading
    from tools.health_tools import monitor_during_rollover

    apply_result = {"out": None}

    def _do_node_apply():
        apply_result["out"] = _run_terraform(
            f'apply -input=false -auto-approve -no-color -var="eks_version={target_version}"',
            timeout=2400,  # node rollover (surge + drain) is the slow part
        )

    stop_event = threading.Event()
    apply_thread = threading.Thread(target=_do_node_apply, daemon=True)
    apply_thread.start()

    # Foreground: monitor availability until the apply thread finishes.
    monitor_stop = threading.Event()
    monitor_result = {"data": None}

    def _do_monitor():
        monitor_result["data"] = monitor_during_rollover(monitor_stop, interval_seconds=15)

    monitor_thread = threading.Thread(target=_do_monitor, daemon=True)
    monitor_thread.start()

    apply_thread.join()          # wait for the node rollover to complete/fail
    monitor_stop.set()           # tell the monitor to stop
    monitor_thread.join(timeout=30)

    p2 = apply_result["out"] or "ERROR: node apply produced no output"
    mon = monitor_result["data"] or {"alarm": False, "breaches_seen": []}

    alarm_note = ""
    if mon.get("monitor_active") is False and mon.get("warning"):
        alarm_note += f"\n⚠️ LIVE MONITOR INACTIVE: {mon['warning']}"
    if mon.get("alarm"):
        alarm_note = ("\n🚨 AVAILABILITY ALARM raised DURING the node rollover — a critical "
                      "deployment dropped below its floor: " + "; ".join(mon.get("breaches_seen", [])))
        if mon.get("halted"):
            cordoned = (mon.get("halt_detail") or {}).get("cordoned", [])
            alarm_note += ("\n⛔ NODE DRAINING HALTED — cordoned old node(s) to stop further "
                           f"eviction so you can debug: {', '.join(cordoned) or 'none'}.\n"
                           "   To RESUME: fix the workload, `kubectl uncordon` those nodes, "
                           "then re-run `terraform apply` to finish the rollover.")

    if p2.startswith("ERROR"):
        return (f"terraform apply FAILED in PHASE 2 (node groups) for {target_version}: {p2}\n"
                f"Control plane is already on {target_version}. Nodes may be partially rolled — "
                f"check node health and re-run apply to finish.{alarm_note}")

    if mon.get("alarm"):
        return (f"COMPLETED WITH ALARM: EKS upgrade to {target_version} applied, but availability "
                f"dropped below the safe threshold during the node rollover.{alarm_note}\n"
                f"Investigate the affected workloads (PDBs, replica counts, anti-affinity).\n"
                f"Phase1:\n{p1}\nPhase2:\n{p2}")

    return (f"SUCCESS: EKS upgrade to {target_version} applied in two phases "
            f"(control plane, then node groups). No availability breach during rollover.\n"
            f"Phase1:\n{p1}\nPhase2:\n{p2}")


def _describe_cluster_version_status(target_version: str) -> tuple[bool, str]:
    """AWS API gate: does the control plane report target version AND ACTIVE?"""
    try:
        r = subprocess.run(
            f"aws eks describe-cluster --name {settings.CLUSTER_NAME} "
            f"--region {settings.AWS_REGION} "
            f"--query 'cluster.[version,status]' --output text",
            shell=True, capture_output=True, text=True, timeout=60,
        )
        parts = r.stdout.split()
        if len(parts) >= 2:
            version, status = parts[0], parts[1]
            if version == target_version and status == "ACTIVE":
                return True, f"AWS: ACTIVE on {version}"
            return False, f"AWS: version={version} status={status}"
        return False, f"AWS: unexpected describe output '{r.stdout.strip()}'"
    except Exception as e:  # noqa: BLE001
        return False, f"AWS: describe error {e}"


def _apiserver_responsive() -> tuple[bool, float, str]:
    """K8s API gate: does `kubectl get nodes` respond within the latency threshold?

    Returns (ok, elapsed_seconds, message). A control plane can report ACTIVE in
    the AWS API while the API server is still slow/unresponsive right after an
    upgrade — this measures the real thing.
    """
    import time
    threshold = settings.APISERVER_LATENCY_THRESHOLD_S
    start = time.monotonic()
    try:
        r = subprocess.run(
            "kubectl get nodes --no-headers",
            shell=True, capture_output=True, text=True,
            timeout=max(5, int(threshold) + 5),
        )
        elapsed = time.monotonic() - start
        if r.returncode != 0:
            return False, elapsed, f"kubectl error: {r.stderr.strip() or 'nonzero exit'}"
        if elapsed > threshold:
            return False, elapsed, f"kubectl responded but slow ({elapsed:.1f}s > {threshold}s)"
        return True, elapsed, f"kubectl responded in {elapsed:.1f}s"
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        return False, elapsed, f"kubectl timed out (> {threshold}s) — API server not responsive"
    except Exception as e:  # noqa: BLE001
        return False, time.monotonic() - start, f"kubectl failed: {e}"


def _control_plane_ready(target_version: str, retries: int = 30, interval: int = 20) -> tuple[bool, str]:
    """Sequence gate between phase 1 (control plane) and phase 2 (nodes).

    The agent BLOCKS and LOOPS until BOTH are true on the SAME iteration:
      1. AWS API reports the control plane is ACTIVE on the target version.
      2. The Kubernetes API server responds to `kubectl get nodes` within a
         normal latency threshold (APISERVER_LATENCY_THRESHOLD_S).

    Requiring both, together, and stable for a couple of consecutive checks
    avoids touching nodes while the API server is still settling after the
    control-plane upgrade. Returns (ok, message).
    """
    import time
    stable_needed = 2   # require consecutive good checks so a one-off blip doesn't pass the gate
    stable = 0
    last = "no data"

    for attempt in range(1, retries + 1):
        aws_ok, aws_msg = _describe_cluster_version_status(target_version)
        if not aws_ok:
            stable = 0
            last = aws_msg
            logger.info("Gate [%d/%d]: %s — waiting.", attempt, retries, aws_msg)
            time.sleep(interval)
            continue

        api_ok, elapsed, api_msg = _apiserver_responsive()
        if not api_ok:
            stable = 0
            last = f"{aws_msg}; {api_msg}"
            logger.info("Gate [%d/%d]: control plane ACTIVE but API not ready — %s",
                        attempt, retries, api_msg)
            time.sleep(interval)
            continue

        stable += 1
        last = f"{aws_msg}; {api_msg}; stable={stable}/{stable_needed}"
        logger.info("Gate [%d/%d]: %s", attempt, retries, last)
        if stable >= stable_needed:
            return True, (f"control plane ACTIVE on {target_version} and API server responsive "
                          f"({api_msg}) — safe to proceed to node groups.")
        time.sleep(interval)

    return False, (f"control plane readiness gate NOT satisfied within "
                   f"{retries * interval}s. Last: {last}. NOT proceeding to node groups.")


def _verify_against_store(target_version: str) -> tuple[bool, str]:
    """Verify the current approval record directly (status, version, approvers, TTL).

    This does NOT accept externally-supplied evidence — it validates the record
    the human created, so an agent cannot forge or replace the reviewed plan.
    """
    cur = approval_gate.get_current()
    if not cur:
        return False, "no approval on record"
    if cur.get("target_version") != target_version:
        return False, f"approval on file is for {cur.get('target_version')}, not {target_version}"
    if cur.get("status") != "APPROVED":
        return False, f"status is {cur.get('status')}, not APPROVED"
    if len(cur.get("approvers", [])) < cur.get("required_approvers", 1):
        return False, "not enough distinct approvers on record"

    # TTL: reuse the same window approval_gate enforces.
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


@tool("Verify EKS Version After Upgrade")
def verify_eks_version(target_version: str) -> str:
    """Post-upgrade check: confirm the live control-plane version matches target.

    Read-only. Returns PASS if the cluster now reports the target version.
    """
    logger.info("Verifying EKS version is now %s", target_version)
    try:
        result = subprocess.run(
            f"aws eks describe-cluster --name {settings.CLUSTER_NAME} "
            f"--region {settings.AWS_REGION} --query cluster.version --output text",
            shell=True, capture_output=True, text=True, timeout=60,
        )
        current = result.stdout.strip()
    except Exception as e:  # noqa: BLE001
        return f"WARN: could not verify version ({e}). Check manually."

    if current == target_version:
        return f"PASS: cluster is now running {current}."
    return f"ALERT: expected {target_version} but cluster reports {current}. Investigate."
