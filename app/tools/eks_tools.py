"""
EKS pre-upgrade check tools — READ ONLY.

These tools gather the evidence a human needs to approve (or reject) an EKS
version upgrade. None of them modify the cluster. They match the tool pattern
used across the companion crewAi project: a small subprocess helper with a
timeout, and structured PASS / ALERT string returns the agent can reason over.
"""
import json
import subprocess

try:
    from crewai.tools import tool  # production
except Exception:  # noqa: BLE001 - test env without CrewAI
    from _toolshim import tool

from config import settings, logger


# ─── subprocess helpers (bounded, never hang) ────────────────────────────────

def _run(cmd: str, timeout: int = 60) -> str:
    """Run a shell command and return stdout, or an error string. Never raises."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            return f"ERROR: {result.stderr.strip() or result.stdout.strip()}"
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s"
    except Exception as e:  # noqa: BLE001 - tools must degrade gracefully
        return f"ERROR: {e}"


def _run_kubectl(cmd: str, timeout: int = 30) -> str:
    return _run(f"kubectl {cmd}", timeout=timeout)


def _run_aws(cmd: str, timeout: int = 60) -> str:
    return _run(f"aws {cmd}", timeout=timeout)


def _parse_minor(version: str):
    """Return (major, minor) ints from a '1.33' style string, or None if invalid."""
    try:
        major, minor = version.strip().split(".")[:2]
        return int(major), int(minor)
    except (ValueError, AttributeError):
        return None


# ─── tools ────────────────────────────────────────────────────────────────

@tool("Get Current EKS Version")
def get_current_eks_version(cluster_name: str = "") -> str:
    """Return the current Kubernetes control-plane version of the EKS cluster.

    Reads it live from AWS. Read-only. If cluster_name is empty, uses the
    configured default.
    """
    cluster = cluster_name or settings.CLUSTER_NAME
    logger.info("Reading current EKS version for cluster: %s", cluster)

    out = _run_aws(
        f"eks describe-cluster --name {cluster} --region {settings.AWS_REGION} "
        f"--query cluster.version --output text"
    )
    if out.startswith("ERROR"):
        return f"Could not read current version for '{cluster}': {out}"
    return f"Current EKS version for '{cluster}' is {out}"


@tool("Check Cluster Is Ready To Upgrade")
def check_cluster_upgradeable(cluster_name: str = "") -> str:
    """Confirm the cluster EXISTS and is ACTIVE (not already mid-update). Read-only.

    Starting an upgrade against a cluster that is already UPDATING (a prior
    upgrade or config change still in flight) is unsafe. Also catches a
    wrong/typo'd cluster name or wrong account/region early. Returns PASS only
    if the cluster is found and status == ACTIVE.
    """
    cluster = cluster_name or settings.CLUSTER_NAME
    logger.info("Checking cluster '%s' exists and is ACTIVE", cluster)

    out = _run_aws(
        f"eks describe-cluster --name {cluster} --region {settings.AWS_REGION} "
        f"--query cluster.status --output text"
    )
    if out.startswith("ERROR"):
        # ResourceNotFound / auth / region errors surface here.
        return (f"ALERT: cannot describe cluster '{cluster}' in {settings.AWS_REGION} "
                f"({out}). Check the cluster name, account, and region. UNSAFE to proceed.")
    status = out.strip()
    if status != "ACTIVE":
        return (f"ALERT: cluster '{cluster}' status is '{status}', not ACTIVE — an update may "
                f"already be in progress. UNSAFE to start another upgrade now.")
    return f"PASS: cluster '{cluster}' exists and is ACTIVE — ready to plan an upgrade."


@tool("Validate EKS Upgrade Target")
def validate_upgrade_target(current_version: str, target_version: str) -> str:
    """Validate a proposed EKS upgrade jump.

    EKS supports upgrading exactly ONE minor version at a time and never
    downgrading. Returns PASS if target == current+1 minor, otherwise ALERT
    with the reason. This is the first gate — an invalid jump should stop the
    whole workflow.
    """
    cur = _parse_minor(current_version)
    tgt = _parse_minor(target_version)

    if cur is None:
        return f"ALERT: current version '{current_version}' is not valid (expected e.g. 1.33)"
    if tgt is None:
        return f"ALERT: target version '{target_version}' is not valid (expected e.g. 1.34)"

    cur_major, cur_minor = cur
    tgt_major, tgt_minor = tgt

    if tgt_major != cur_major:
        return f"ALERT: major version change {current_version} -> {target_version} is not supported by EKS."
    if tgt_minor == cur_minor:
        return f"ALERT: target {target_version} equals current {current_version} — nothing to upgrade."
    if tgt_minor < cur_minor:
        return f"ALERT: downgrade {current_version} -> {target_version} is NOT possible on EKS. Refuse."
    if tgt_minor > cur_minor + 1:
        return (
            f"ALERT: cannot skip minor versions. {current_version} -> {target_version} "
            f"is {tgt_minor - cur_minor} minors. Upgrade one at a time "
            f"(next allowed target is {cur_major}.{cur_minor + 1})."
        )
    return f"PASS: {current_version} -> {target_version} is a valid single-minor upgrade."


@tool("Scan Deprecated Kubernetes APIs")
def scan_deprecated_apis(target_version: str = "") -> str:
    """Scan the cluster for API versions removed/deprecated in the target release.

    Uses `kubectl get --raw /metrics`-independent approach: checks EKS upgrade
    insights if available, and lists API resources in use. Read-only. This is
    advisory evidence for the human — deprecated APIs are the #1 cause of
    broken workloads after an upgrade.
    """
    logger.info("Scanning for deprecated APIs ahead of target %s", target_version)

    # Prefer EKS-native upgrade insights when present (surfaces deprecated API usage).
    insights = _run_aws(
        f"eks list-insights --cluster-name {settings.CLUSTER_NAME} "
        f"--region {settings.AWS_REGION} --output json"
    )
    if not insights.startswith("ERROR"):
        try:
            data = json.loads(insights)
            items = data.get("insights", [])
            flagged = [
                i.get("name", "unknown")
                for i in items
                if i.get("insightStatus", {}).get("status") not in ("PASSING", None)
            ]
            if flagged:
                return "ALERT: EKS upgrade insights flagged: " + ", ".join(flagged)
            return "PASS: EKS upgrade insights report no blocking issues."
        except json.JSONDecodeError:
            pass  # fall through to the kubectl-based advisory

    # Fallback: list api-resources so the human can eyeball known-removed groups.
    api_resources = _run_kubectl("api-resources --no-headers")
    if api_resources.startswith("ERROR"):
        return f"WARN: could not scan APIs ({api_resources}). Review deprecated APIs manually before approving."
    return (
        "WARN: EKS upgrade insights unavailable; ran a basic api-resources listing instead. "
        "Manually confirm no removed APIs (e.g. old batch/*, policy/* betas) are in use before approving."
    )


@tool("Check EKS Addon Compatibility")
def check_addon_compatibility(target_version: str) -> str:
    """Check whether installed EKS managed addons support the target version.

    Read-only. Lists addons and their versions so the human can confirm each
    has a version compatible with the target Kubernetes release.
    """
    logger.info("Checking addon compatibility for target %s", target_version)

    addons = _run_aws(
        f"eks list-addons --cluster-name {settings.CLUSTER_NAME} "
        f"--region {settings.AWS_REGION} --query addons --output text"
    )
    if addons.startswith("ERROR"):
        return f"WARN: could not list addons ({addons}). Verify addon compatibility manually."
    if not addons:
        return "PASS: no managed addons installed — nothing to check for compatibility."

    lines = []
    for addon in addons.split():
        avail = _run_aws(
            f"eks describe-addon-versions --addon-name {addon} "
            f"--kubernetes-version {target_version} --region {settings.AWS_REGION} "
            f"--query 'addons[0].addonVersions[0].addonVersion' --output text"
        )
        if avail.startswith("ERROR") or avail in ("", "None"):
            lines.append(f"{addon}: NO compatible version found for {target_version}")
        else:
            lines.append(f"{addon}: compatible ({avail})")

    if any("NO compatible" in l for l in lines):
        return "ALERT: addon compatibility issues — " + "; ".join(lines)
    return "PASS: all addons have a version compatible with " + target_version + " — " + "; ".join(lines)


@tool("Check Node Readiness For Upgrade")
def check_node_readiness(cluster_name: str = "") -> str:
    """Confirm all nodes are Ready before upgrading. Read-only.

    Upgrading with NotReady nodes risks capacity loss during the rolling node
    replacement. Returns PASS only if every node is Ready.
    """
    logger.info("Checking node readiness before upgrade")

    out = _run_kubectl("get nodes --no-headers")
    if out.startswith("ERROR"):
        return f"WARN: could not read nodes ({out}). Confirm node health manually before approving."
    if not out:
        return "ALERT: no nodes found — cannot upgrade an empty cluster safely."

    not_ready = []
    total = 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            total += 1
            name, status = parts[0], parts[1]
            # status may be "Ready", "NotReady", or "Ready,SchedulingDisabled"
            # (a cordoned but healthy node). Treat a node as ready if its status
            # STARTS with "Ready" — so a cordoned-healthy node is not misflagged.
            if not status.startswith("Ready"):
                not_ready.append(f"{name} ({status})")

    if not_ready:
        return f"ALERT: {len(not_ready)}/{total} nodes not Ready: " + ", ".join(not_ready)
    return f"PASS: all {total} nodes are Ready."


# ─── availability pre-checks (zero-downtime readiness) ──────────────────

@tool("Check PodDisruptionBudget Coverage")
def check_pdb_coverage(cluster_name: str = "") -> str:
    """Check that workloads have PodDisruptionBudgets before a node rollover.

    Read-only. Node group upgrades drain and replace nodes; without PDBs the
    drain can evict all replicas of a workload at once, causing downtime. This
    lists Deployments and StatefulSets (>1 replica) that have NO PDB protecting
    them. Returns PASS only if every multi-replica workload is covered.
    """
    logger.info("Checking PodDisruptionBudget coverage")

    pdbs = _run_kubectl("get pdb -A --no-headers", timeout=30)
    if pdbs.startswith("ERROR"):
        return f"WARN: could not list PDBs ({pdbs}). Confirm disruption budgets manually before upgrading nodes."

    # Collect namespaces that have at least one PDB (coarse coverage signal).
    covered_ns = set()
    for line in pdbs.splitlines():
        parts = line.split()
        if parts:
            covered_ns.add(parts[0])

    deploys = _run_kubectl(
        "get deploy,statefulset -A --no-headers "
        "-o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,REP:.spec.replicas",
        timeout=30,
    )
    if deploys.startswith("ERROR"):
        return f"WARN: could not list workloads ({deploys}). Confirm PDBs manually."

    unprotected = []
    for line in deploys.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            ns, name, rep = parts[0], parts[1], parts[2]
            # Only multi-replica workloads risk downtime on eviction.
            try:
                replicas = int(rep)
            except ValueError:
                replicas = 1
            # Skip kube-system / managed addons — EKS manages those during upgrade.
            if ns in ("kube-system", "kube-node-lease", "kube-public"):
                continue
            if replicas > 1 and ns not in covered_ns:
                unprotected.append(f"{ns}/{name} (replicas={replicas})")

    if unprotected:
        return ("ALERT: multi-replica workloads without a PodDisruptionBudget "
                "(node rollover may cause downtime): " + ", ".join(unprotected))
    return "PASS: all multi-replica app workloads are covered by a PDB (or namespace has one)."


@tool("Check Cluster Capacity Headroom")
def check_capacity_headroom(cluster_name: str = "") -> str:
    """Check there is spare node capacity to absorb a rolling node replacement.

    Read-only. During a managed node group upgrade, a surge node is added and an
    old node is drained. If the cluster has only one node, or nodes are packed
    with no reschedule room, draining causes downtime. Returns PASS if there is
    more than one Ready node (so pods can reschedule during rollover).
    """
    logger.info("Checking capacity headroom for node rollover")

    out = _run_kubectl("get nodes --no-headers", timeout=30)
    if out.startswith("ERROR"):
        return f"WARN: could not read nodes ({out}). Confirm capacity manually."

    ready = 0
    total = 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            total += 1
            if parts[1] == "Ready":
                ready += 1

    if total == 0:
        return "ALERT: no nodes found — cannot upgrade safely."
    if ready < 2:
        return (f"ALERT: only {ready} Ready node(s). A node rollover would drain the "
                f"only node and cause downtime. Scale to >=2 nodes (and enable node "
                f"surge) before upgrading.")
    return (f"PASS: {ready}/{total} nodes Ready — enough headroom for pods to "
            f"reschedule during a rolling node replacement.")


# ─── surge-capacity / EC2 quota pre-check (prevents mid-upgrade freeze) ───

# vCPU count per instance type used by node groups. Extend as needed; unknown
# types fall back to a conservative default so we over-estimate the surge need
# rather than under-estimate it.
_INSTANCE_VCPU = {
    "t3.small": 2, "t3.medium": 2, "t3.large": 2, "t3.xlarge": 4, "t3.2xlarge": 8,
    "t3a.medium": 2, "t3a.large": 2, "t3a.xlarge": 4,
    "m5.large": 2, "m5.xlarge": 4, "m5.2xlarge": 8, "m5.4xlarge": 16,
    "m5a.large": 2, "m5a.xlarge": 4,
    "m6i.large": 2, "m6i.xlarge": 4, "m6i.2xlarge": 8,
    "c5.large": 2, "c5.xlarge": 4, "c5.2xlarge": 8,
    "c6i.large": 2, "c6i.xlarge": 4,
    "r5.large": 2, "r5.xlarge": 4, "r5.2xlarge": 8,
}
_DEFAULT_VCPU = 8  # conservative fallback for unknown types


def _vcpus_for(instance_type: str) -> int:
    return _INSTANCE_VCPU.get(instance_type.strip(), _DEFAULT_VCPU)


@tool("Check EC2 Surge Quota For Node Rollover")
def check_ec2_surge_quota(cluster_name: str = "") -> str:
    """Verify AWS has enough vCPU quota to launch SURGE nodes during the rollover.

    Read-only. True zero-downtime node upgrades bring up NEW nodes before draining
    old ones ("maxUnavailable"/"maxSurge"). Those surge instances count against the
    EC2 "Running On-Demand Standard instances" vCPU service quota (L-1216C47A).
    If the account is near that limit, the surge instance fails to launch and the
    node-group rollout FREEZES mid-upgrade with old nodes not yet drained.

    This estimates the surge vCPUs the upgrade will request and compares against
    remaining quota headroom. Returns:
      PASS  — quota headroom covers the surge
      ALERT — surge would exceed quota (request an increase BEFORE upgrading)
      WARN  — could not determine quota/usage (verify manually)
    """
    cluster = cluster_name or settings.CLUSTER_NAME
    logger.info("Checking EC2 surge quota for node rollover on %s", cluster)

    # 1. Determine node group instance types + max sizes (what a surge could add).
    ng_names = _run_aws(
        f"eks list-nodegroups --cluster-name {cluster} --region {settings.AWS_REGION} "
        f"--query nodegroups --output text"
    )
    if ng_names.startswith("ERROR"):
        return f"WARN: could not list node groups ({ng_names}). Verify EC2 surge quota manually."
    if not ng_names:
        return "WARN: no managed node groups found — verify surge capacity manually."

    surge_vcpus = 0
    detail = []
    for ng in ng_names.split():
        desc = _run_aws(
            f"eks describe-nodegroup --cluster-name {cluster} --nodegroup-name {ng} "
            f"--region {settings.AWS_REGION} "
            f"--query 'nodegroup.[instanceTypes[0],scalingConfig.maxSize,updateConfig.maxUnavailable]' "
            f"--output text"
        )
        if desc.startswith("ERROR"):
            return f"WARN: could not describe node group {ng} ({desc}). Verify surge quota manually."
        parts = desc.split()
        itype = parts[0] if parts else "unknown"
        # A managed node group update surges roughly one extra node per node group
        # (default maxUnavailable=1 → +1 surge node) unless configured higher.
        try:
            max_unavail = int(parts[2]) if len(parts) > 2 and parts[2] not in ("None", "") else 1
        except ValueError:
            max_unavail = 1
        surge_nodes = max(1, max_unavail)
        vcpu = _vcpus_for(itype) * surge_nodes
        surge_vcpus += vcpu
        detail.append(f"{ng}: {surge_nodes}x {itype} = {vcpu} vCPU")

    # 2. Read the On-Demand Standard vCPU quota (L-1216C47A).
    quota = _run_aws(
        "service-quotas get-service-quota --service-code ec2 "
        "--quota-code L-1216C47A --region " + settings.AWS_REGION +
        " --query Quota.Value --output text"
    )
    if quota.startswith("ERROR"):
        return (f"WARN: could not read EC2 vCPU quota ({quota}). Surge need is ~{surge_vcpus} vCPU "
                f"({'; '.join(detail)}). Verify quota manually before upgrading.")
    try:
        quota_vcpus = int(float(quota))
    except ValueError:
        return f"WARN: unexpected quota value '{quota}'. Verify manually. Surge need ~{surge_vcpus} vCPU."

    # 3. Estimate current running On-Demand vCPU usage.
    running = _run_aws(
        "ec2 describe-instances --region " + settings.AWS_REGION +
        " --filters Name=instance-state-name,Values=running "
        "--query 'Reservations[].Instances[].CpuOptions.[CoreCount,ThreadsPerCore]' --output text"
    )
    used_vcpus = 0
    if not running.startswith("ERROR") and running:
        for line in running.splitlines():
            nums = line.split()
            if len(nums) >= 2:
                try:
                    used_vcpus += int(nums[0]) * int(nums[1])
                except ValueError:
                    continue

    headroom = quota_vcpus - used_vcpus
    summary = (f"quota={quota_vcpus} vCPU, in-use~={used_vcpus} vCPU, headroom~={headroom} vCPU; "
               f"surge needs ~{surge_vcpus} vCPU ({'; '.join(detail)})")

    if headroom < surge_vcpus:
        return (f"ALERT: EC2 vCPU quota headroom is too low for the node surge — the rollover "
                f"could FREEZE mid-upgrade. {summary}. Request an increase to the 'Running "
                f"On-Demand Standard instances' quota (L-1216C47A) BEFORE upgrading.")
    return f"PASS: enough EC2 vCPU quota headroom for the node surge. {summary}."


@tool("Check PodDisruptionBudget Strength (preventive)")
def check_pdb_strength(cluster_name: str = "") -> str:
    """Verify each critical namespace's PDBs sit in the SAFE BAND for a node drain
    — neither too loose nor too strict. Read-only, PREVENTIVE control.

    A PDB that merely *exists* isn't enough. Two opposite failure modes both
    cause downtime, and this checks for both using each PDB's live status
    (disruptionsAllowed, currentHealthy, expectedPods):

      TOO LOOSE  (disruptionsAllowed / currentHealthy > AVAILABILITY_DROP_THRESHOLD):
        the drain can evict too many pods at once → breaches the availability
        floor → over-eviction downtime.

      TOO STRICT (disruptionsAllowed == 0, e.g. maxUnavailable: 0 or
        minAvailable == replica count): the drain can evict NO pods → the node
        rollover STALLS indefinitely, or force-evicts at timeout → downtime.

    Also flags misconfigured PDBs (currentHealthy=0 → bad selector).
    Returns PASS only if every evaluated PDB allows safe drain progress AND caps
    disruption within the threshold.
    """
    logger.info("Checking PodDisruptionBudget strength vs availability threshold")

    threshold = settings.AVAILABILITY_DROP_THRESHOLD
    crit = settings.CRITICAL_NAMESPACES  # empty = evaluate all app namespaces

    # Pull PDB status fields as JSON for reliable parsing.
    raw = _run_kubectl("get pdb -A -o json", timeout=30)
    if raw.startswith("ERROR"):
        return (f"WARN: could not read PDBs ({raw}). Confirm PDB strength manually — "
                f"critical deployments should cap disruption at <= {int(threshold*100)}%.")
    try:
        import json
        items = json.loads(raw).get("items", [])
    except json.JSONDecodeError:
        return "WARN: could not parse PDB JSON. Verify PDB strength manually."

    if not items:
        return (f"ALERT: no PodDisruptionBudgets found. Critical deployments need a PDB strict "
                f"enough to keep disruption <= {int(threshold*100)}% during node drains.")

    too_loose = []
    too_strict = []
    misconfigured = []
    evaluated = 0

    for pdb in items:
        ns = pdb.get("metadata", {}).get("namespace", "")
        name = pdb.get("metadata", {}).get("name", "")
        # Skip cluster-managed namespaces.
        if ns in ("kube-system", "kube-node-lease", "kube-public"):
            continue
        # If a critical list is set, only evaluate those namespaces.
        if crit and ns not in crit:
            continue

        status = pdb.get("status", {})
        allowed = status.get("disruptionsAllowed")
        healthy = status.get("currentHealthy")
        expected = status.get("expectedPods")
        if allowed is None or healthy is None:
            misconfigured.append(f"{ns}/{name} (status not populated yet)")
            continue
        if healthy == 0:
            misconfigured.append(f"{ns}/{name} (currentHealthy=0 — selector may be wrong)")
            continue

        evaluated += 1

        # ── TOO STRICT (deadlock) ────────────────────────────────────────
        # disruptionsAllowed == 0 means Kubernetes will let the drain evict
        # ZERO pods. During a node drain, terraform/eviction then stalls
        # indefinitely (or force-evicts at timeout → downtime). This happens
        # with maxUnavailable: 0, or minAvailable >= the current replica count.
        if allowed == 0:
            too_strict.append(
                f"{ns}/{name}: disruptionsAllowed=0 (currentHealthy={healthy}"
                + (f", expectedPods={expected}" if expected is not None else "")
                + ") — a node drain cannot evict any pod; the rollover will STALL "
                "or force-evict. Loosen to maxUnavailable>=1 or minAvailable<replicas."
            )
            continue

        # ── TOO LOOSE (over-eviction) ────────────────────────────────────
        disrupt_fraction = allowed / healthy
        if disrupt_fraction > threshold + 1e-9:
            too_loose.append(
                f"{ns}/{name}: allows {allowed}/{healthy} disrupted "
                f"({int(disrupt_fraction*100)}% > {int(threshold*100)}% cap)"
            )

    if misconfigured:
        return ("ALERT: PDBs look misconfigured (fix before upgrading): "
                + ", ".join(misconfigured))
    if too_strict:
        return ("ALERT: PDBs too STRICT — a node drain cannot make progress and the rollover "
                "will STALL or force-evict (downtime): " + ", ".join(too_strict))
    if too_loose:
        return ("ALERT: PDBs too LOOSE to guarantee the availability floor — tighten "
                "minAvailable/maxUnavailable so disruption stays within "
                f"{int(threshold*100)}%: " + ", ".join(too_loose))
    if evaluated == 0:
        return (f"WARN: no PDBs matched the critical namespaces {crit or '(all app ns)'} — "
                f"confirm critical deployments are protected.")
    return (f"PASS: all {evaluated} evaluated PDB(s) allow safe drain progress and cap "
            f"disruption within the {int(threshold*100)}% availability threshold.")
