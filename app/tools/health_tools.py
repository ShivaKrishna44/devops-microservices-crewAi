"""
Cluster health tools — baseline snapshot, poll-until-healthy loop, regression
compare. These make the upgrade *availability-aware*, not just version-aware.

Flow they support:
  BEFORE apply:  snapshot_cluster_health()  -> saves a baseline (healthy workloads)
  AFTER apply:   wait_for_healthy()          -> polls until nodes Ready + pods healthy
                 compare_to_baseline()        -> flags workloads that were healthy
                                                  before but are unhealthy now (regression)

All read-only against the cluster. The baseline is persisted next to the
approval store so it survives across the request -> apply steps (separate procs).
"""
import json
import os
import subprocess
import time

try:
    from crewai.tools import tool  # production
except Exception:  # noqa: BLE001 - test env without CrewAI
    from _toolshim import tool

from config import settings, logger


def _kubectl(cmd: str, timeout: int = 30) -> str:
    try:
        r = subprocess.run(f"kubectl {cmd}", shell=True, capture_output=True,
                           text=True, timeout=timeout)
        if r.returncode != 0:
            return f"ERROR: {r.stderr.strip() or r.stdout.strip()}"
        return r.stdout.strip()
    except subprocess.TimeoutExpired:
        return f"ERROR: kubectl timed out after {timeout}s"
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"


def _baseline_path() -> str:
    store_dir = os.path.dirname(settings.APPROVAL_STORE) or "."
    return os.path.join(store_dir, "health_baseline.json")


def _collect_health() -> dict:
    """Return a structured snapshot of current cluster health (read-only).

    Captures enough to enforce the post-upgrade acceptance criteria:
      - node name -> status         (detect NotReady AND old nodes lingering)
      - unhealthy pods              (regression detection)
      - degraded workloads          (ready != desired right now)
      - replica map ns/name -> desired  (match against the pre-flight baseline)
    """
    snap = {
        "nodes": {},
        "node_names": [],
        "unhealthy_pods": [],
        "degraded_workloads": [],
        "replica_map": {},   # ns/name -> desired replicas
        "ready_map": {},     # ns/name -> ready (healthy) replicas — the live availability signal
        "collected_at": time.time(),
    }

    nodes = _kubectl("get nodes --no-headers")
    if not nodes.startswith("ERROR"):
        for line in nodes.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                snap["nodes"][parts[0]] = parts[1]
        snap["node_names"] = sorted(snap["nodes"].keys())

    pods = _kubectl("get pods -A --no-headers")
    if not pods.startswith("ERROR"):
        for line in pods.splitlines():
            parts = line.split()
            # NS NAME READY STATUS RESTARTS AGE
            if len(parts) >= 4:
                ns, name, ready, status = parts[0], parts[1], parts[2], parts[3]
                if status not in ("Running", "Completed"):
                    snap["unhealthy_pods"].append(f"{ns}/{name}:{status}")

    deploys = _kubectl(
        "get deploy -A --no-headers "
        "-o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,"
        "READY:.status.readyReplicas,DESIRED:.spec.replicas"
    )
    if not deploys.startswith("ERROR"):
        for line in deploys.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                ns, name, ready, desired = parts[0], parts[1], parts[2], parts[3]
                ready = "0" if ready in ("<none>", "", "None") else ready
                key = f"{ns}/{name}"
                snap["replica_map"][key] = desired
                try:
                    snap["ready_map"][key] = int(ready)
                except ValueError:
                    snap["ready_map"][key] = 0
                if ready != desired:
                    snap["degraded_workloads"].append(f"{key}:{ready}/{desired}")

    return snap


@tool("Snapshot Cluster Health Baseline")
def snapshot_cluster_health(label: str = "pre-upgrade") -> str:
    """Capture a health baseline BEFORE the upgrade. Read-only.

    Persists which workloads are currently healthy so we can detect regressions
    after the upgrade (something that was fine before but broke).
    """
    logger.info("Capturing cluster health baseline (%s)", label)
    snap = _collect_health()
    snap["label"] = label
    try:
        with open(_baseline_path(), "w", encoding="utf-8") as f:
            json.dump(snap, f, indent=2)
    except OSError as e:
        return f"WARN: could not persist baseline ({e}). Post-upgrade regression compare will be limited."

    return (f"Baseline captured: {len(snap['nodes'])} nodes, "
            f"{len(snap['unhealthy_pods'])} already-unhealthy pods, "
            f"{len(snap['degraded_workloads'])} already-degraded workloads.")


def _load_baseline() -> dict:
    path = _baseline_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


@tool("Wait For Cluster Healthy")
def wait_for_healthy(timeout_seconds: int = 1200, interval_seconds: int = 30) -> str:
    """Post-upgrade validation loop. Poll cluster health every `interval_seconds`
    (default 30s) for up to `timeout_seconds` (default 1200s = 20 min).

    Read-only. Returns PASS only when ALL acceptance criteria hold together:
      1. Every node is Ready.
      2. Every OLD node from the pre-flight baseline is fully terminated/gone
         (a stalled rollover that leaves old nodes around must NOT pass).
      3. No pods are in a bad state.
      4. Deployment replica counts MATCH the pre-flight baseline (no workload
         silently lost or gained replicas during the upgrade).

    On timeout, returns ALERT with exactly which criteria are still unmet.
    """
    logger.info("Post-upgrade validation loop: every %ss for up to %ss",
                interval_seconds, timeout_seconds)

    baseline = _load_baseline()
    baseline_nodes = set(baseline.get("node_names", []))
    baseline_replicas = baseline.get("replica_map", {})

    deadline = time.time() + max(timeout_seconds, interval_seconds)
    last = "no data"

    while time.time() < deadline:
        snap = _collect_health()
        current_nodes = set(snap.get("node_names", []))

        # 1. all nodes Ready
        not_ready = [n for n, s in snap["nodes"].items() if s != "Ready"]
        # 2. old baseline nodes fully gone
        old_still_present = sorted(baseline_nodes & current_nodes) if baseline_nodes else []
        # 3. no bad pods
        bad_pods = snap["unhealthy_pods"]
        # 4. replica counts match baseline (only if we have a baseline)
        replica_mismatch = []
        if baseline_replicas:
            now_replicas = snap.get("replica_map", {})
            for key, want in baseline_replicas.items():
                have = now_replicas.get(key)
                if have is None:
                    replica_mismatch.append(f"{key}: missing (baseline {want})")
                elif have != want:
                    replica_mismatch.append(f"{key}: {have} != baseline {want}")

        if not not_ready and not old_still_present and not bad_pods and not replica_mismatch:
            return (f"PASS: upgrade validated — all {len(current_nodes)} nodes Ready, "
                    f"all old nodes terminated, all pods healthy, replica counts match the "
                    f"pre-flight baseline.")

        last = (f"nodes NotReady={not_ready or 'none'}; "
                f"old nodes still present={old_still_present or 'none'}; "
                f"bad pods={bad_pods[:10] or 'none'}; "
                f"replica mismatch={replica_mismatch[:10] or 'none'}")
        logger.info("Not fully validated yet: %s", last)
        time.sleep(interval_seconds)

    return (f"ALERT: upgrade NOT fully validated within {timeout_seconds}s "
            f"({interval_seconds}s interval). Unmet: {last}")


@tool("Compare Health To Baseline (Regression Check)")
def compare_to_baseline() -> str:
    """Compare current health to the pre-upgrade baseline. Read-only.

    Flags REGRESSIONS: workloads/pods that were healthy in the baseline but are
    unhealthy now. Pre-existing problems (already broken before the upgrade) are
    NOT counted as regressions. Returns PASS if no new breakage.
    """
    path = _baseline_path()
    if not os.path.exists(path):
        return "WARN: no baseline found — cannot run regression compare. Validate manually."
    try:
        with open(path, "r", encoding="utf-8") as f:
            base = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return f"WARN: baseline unreadable ({e}). Validate manually."

    now = _collect_health()

    base_bad_pods = set(base.get("unhealthy_pods", []))
    now_bad_pods = set(now.get("unhealthy_pods", []))
    # A regression = unhealthy now but wasn't (by name/ns) before.
    def _names(items):
        return {i.rsplit(":", 1)[0] for i in items}
    new_bad_pods = _names(now_bad_pods) - _names(base_bad_pods)

    base_degraded = _names(set(base.get("degraded_workloads", [])))
    now_degraded = _names(set(now.get("degraded_workloads", [])))
    new_degraded = now_degraded - base_degraded

    if new_bad_pods or new_degraded:
        parts = []
        if new_bad_pods:
            parts.append("newly-unhealthy pods: " + ", ".join(sorted(new_bad_pods)))
        if new_degraded:
            parts.append("newly-degraded workloads: " + ", ".join(sorted(new_degraded)))
        return "ALERT: REGRESSION vs baseline — " + "; ".join(parts)

    return "PASS: no regressions vs the pre-upgrade baseline (nothing that was healthy broke)."


# ─── live availability monitoring during the node rollover ───────────────

def _is_critical(key: str) -> bool:
    """A deployment 'ns/name' is critical if its namespace is in CRITICAL_NAMESPACES.

    If CRITICAL_NAMESPACES is empty, every multi-replica app deployment is
    treated as critical (safer default). kube-system etc. are always excluded —
    EKS manages those during the upgrade.
    """
    ns = key.split("/", 1)[0]
    if ns in ("kube-system", "kube-node-lease", "kube-public"):
        return False
    crit = settings.CRITICAL_NAMESPACES
    if crit:
        return ns in crit
    return True  # no explicit list -> treat all app namespaces as critical


def check_availability_breach(baseline: dict = None) -> tuple[bool, list]:
    """Single-shot: has any critical deployment dropped below its availability
    floor vs the pre-flight baseline healthy count?

    Floor = ceil(baseline_ready * (1 - AVAILABILITY_DROP_THRESHOLD)).
    E.g. baseline 5 ready, 20% threshold -> floor 4; dropping to 3 is a breach.

    Returns (breached, breaches[]) where each breach is a human-readable string.
    Pure/testable: pass a baseline dict, or it loads the persisted one.
    """
    import math
    if baseline is None:
        baseline = _load_baseline()
    base_ready = baseline.get("ready_map", {})
    if not base_ready:
        return False, []  # no baseline healthy counts -> nothing to compare (degrade gracefully)

    threshold = settings.AVAILABILITY_DROP_THRESHOLD
    now = _collect_health().get("ready_map", {})

    breaches = []
    for key, base_n in base_ready.items():
        if base_n <= 1 or not _is_critical(key):
            continue  # single-replica or non-critical: not an availability-floor concern here
        floor = math.ceil(base_n * (1.0 - threshold))
        current = now.get(key, 0)
        if current < floor:
            breaches.append(
                f"{key}: {current}/{base_n} healthy (floor {floor}, "
                f">{int(threshold*100)}% drop)"
            )
    return (len(breaches) > 0), breaches


@tool("Check Availability Breach vs Baseline")
def check_availability_breach_tool() -> str:
    """Read-only single check: are any critical deployments below their
    availability floor vs the pre-flight baseline? Returns PASS or ALARM.
    """
    breached, breaches = check_availability_breach()
    if breached:
        return "ALARM: availability breach — " + "; ".join(breaches)
    return "PASS: all critical deployments at or above their availability floor."


def sound_alarm(message: str) -> None:
    """Sound the alarm immediately: structured error log + optional webhook.

    Kept dependency-free (urllib) so it works anywhere. Never raises — an alarm
    must not itself crash the run.
    """
    logger.error("AVAILABILITY ALARM: %s", message)
    url = settings.ALARM_WEBHOOK_URL
    if not url:
        return
    try:
        import json as _json
        import urllib.request
        data = _json.dumps({"text": f":rotating_light: EKS upgrade availability alarm: {message}"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)  # noqa: S310 - user-configured webhook
    except Exception as e:  # noqa: BLE001 - alarm delivery is best-effort
        logger.error("Alarm webhook delivery failed: %s", e)


def halt_node_draining(baseline: dict = None) -> dict:
    """Stop further node draining so a human can debug — safely, at the K8s layer.

    A managed-node-group `terraform apply` cannot be killed mid-instance without
    risking corrupt state, so we DON'T kill terraform. Instead we stop the drain
    *wave* where Kubernetes controls it: **cordon** every OLD (pre-flight
    baseline) node still present. Cordoning marks them unschedulable; the managed
    node group's rolling update drains one node at a time and will not make
    progress evicting from cordoned nodes the way it would otherwise — and no new
    work lands on them. Combined with strict PDBs, further evictions are refused.

    This is best-effort and never raises (a halt attempt must not crash the run).
    Returns a structured result of what was cordoned.
    """
    if baseline is None:
        baseline = _load_baseline()
    baseline_nodes = set(baseline.get("node_names", []))

    result = {"halted": False, "cordoned": [], "errors": [], "note": ""}

    # Which OLD nodes are still in the cluster right now?
    current = _collect_health().get("node_names", [])
    old_present = sorted(baseline_nodes & set(current))

    if not old_present:
        result["note"] = ("No old baseline nodes remain to cordon (rollover may be nearly done). "
                          "Draining could not be halted at the node layer — rely on strict PDBs "
                          "and manual intervention.")
        logger.error("HALT requested but no old baseline nodes remain to cordon.")
        return result

    for node in old_present:
        out = _kubectl(f"cordon {node}", timeout=30)
        if out.startswith("ERROR"):
            result["errors"].append(f"{node}: {out}")
        else:
            result["cordoned"].append(node)

    result["halted"] = len(result["cordoned"]) > 0
    logger.error(
        "HALT: cordoned %d old node(s) to stop further draining: %s. "
        "A human should now debug. To RESUME: fix the workload, `kubectl uncordon <node>` "
        "the cordoned nodes, then re-run `terraform apply` to finish the rollover.",
        len(result["cordoned"]), ", ".join(result["cordoned"]) or "none",
    )
    return result


def monitor_during_rollover(stop_event, interval_seconds: int = 15) -> dict:
    """Live monitor loop — run in a thread ALONGSIDE the node rollover.

    Polls availability every `interval_seconds`. The MOMENT a critical
    deployment drops below its floor, it sounds the alarm and records the first
    breach. Keeps monitoring (so you see the full picture) until `stop_event`
    is set (rollover finished). Returns a summary dict.

    `stop_event` is a threading.Event; the caller sets it when the apply ends.
    """
    baseline = _load_baseline()
    result = {"alarm": False, "first_breach": None, "samples": 0,
              "breaches_seen": [], "halted": False, "halt_detail": None,
              "monitor_active": True, "warning": None}

    # If there's no baseline healthy-count data, the monitor CANNOT detect a
    # breach — surface that loudly instead of silently no-opping.
    if not baseline.get("ready_map"):
        result["monitor_active"] = False
        result["warning"] = ("no pre-flight baseline healthy counts on record — the live "
                              "availability monitor is INACTIVE for this run. Availability "
                              "breaches during the rollover will NOT be auto-detected/halted.")
        logger.error("LIVE MONITOR INACTIVE: %s", result["warning"])

    while not stop_event.is_set():
        breached, breaches = check_availability_breach(baseline)
        result["samples"] += 1
        if breached:
            if not result["alarm"]:
                result["alarm"] = True
                result["first_breach"] = breaches
                sound_alarm("during node rollover — " + "; ".join(breaches))
                # Stop the bleeding: halt further node draining so a human can debug.
                if settings.HALT_ON_AVAILABILITY_BREACH:
                    halt = halt_node_draining(baseline)
                    result["halted"] = halt.get("halted", False)
                    result["halt_detail"] = halt
                    if result["halted"]:
                        logger.error("Node draining HALTED after availability breach — "
                                     "awaiting human intervention.")
            for b in breaches:
                if b not in result["breaches_seen"]:
                    result["breaches_seen"].append(b)
        stop_event.wait(interval_seconds)

    return result
