"""
Guardrails — deterministic, non-LLM safety checks for EKS upgrades.

These run BEFORE any apply and do not depend on the LLM's judgement. Even if an
agent (or a bug) tried to proceed, these checks must pass first. This mirrors
the non-LLM "guardian" pattern from the End-End orchestrator: pattern-based
rules that cannot be talked out of a "no".

Each check returns a GuardrailResult. The executor runs ALL of them and blocks
the apply if ANY returns blocked=True.
"""
from dataclasses import dataclass, field
from typing import Optional

from config import settings, logger


@dataclass
class GuardrailResult:
    name: str
    passed: bool
    blocked: bool          # True = hard stop, refuse the apply
    detail: str
    severity: str = "INFO"  # INFO | WARN | CRITICAL


@dataclass
class GuardrailReport:
    results: list = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(r.blocked for r in self.results)

    @property
    def warnings(self) -> list:
        return [r for r in self.results if r.severity == "WARN" and not r.blocked]

    def add(self, result: GuardrailResult) -> None:
        self.results.append(result)

    def render(self) -> str:
        lines = ["GUARDRAIL REPORT", "=" * 40]
        for r in self.results:
            mark = "BLOCK" if r.blocked else ("WARN" if r.severity == "WARN" else "OK ")
            lines.append(f"[{mark:5}] {r.name}: {r.detail}")
        lines.append("=" * 40)
        lines.append("RESULT: BLOCKED" if self.blocked else "RESULT: CLEARED (guardrails passed)")
        return "\n".join(lines)


def _parse_minor(version: str):
    try:
        major, minor = version.strip().split(".")[:2]
        return int(major), int(minor)
    except (ValueError, AttributeError):
        return None


# ─── individual guardrails ───────────────────────────────────────────────

def gr_version_format(target_version: str) -> GuardrailResult:
    """Target must look like a valid K8s minor version (e.g. 1.34)."""
    if _parse_minor(target_version) is None:
        return GuardrailResult(
            "version_format", passed=False, blocked=True, severity="CRITICAL",
            detail=f"target '{target_version}' is not a valid version (expected e.g. 1.34)",
        )
    return GuardrailResult("version_format", True, False, f"target '{target_version}' is well-formed")


def gr_single_minor_step(current_version: str, target_version: str) -> GuardrailResult:
    """EKS: exactly one minor version up, never down, never skip, never major."""
    cur = _parse_minor(current_version)
    tgt = _parse_minor(target_version)
    if cur is None or tgt is None:
        return GuardrailResult(
            "single_minor_step", passed=False, blocked=True,
            detail="cannot compare versions (unparseable)", severity="CRITICAL",
        )
    cur_major, cur_minor = cur
    tgt_major, tgt_minor = tgt
    if tgt_major != cur_major:
        return GuardrailResult("single_minor_step", False, True,
                               f"major change {current_version}->{target_version} not supported", "CRITICAL")
    if tgt_minor <= cur_minor:
        return GuardrailResult("single_minor_step", False, True,
                               f"{current_version}->{target_version} is a downgrade/no-op — refused", "CRITICAL")
    if tgt_minor > cur_minor + 1:
        return GuardrailResult("single_minor_step", False, True,
                               f"cannot skip minors ({current_version}->{target_version}); next allowed is "
                               f"{cur_major}.{cur_minor + 1}", "CRITICAL")
    return GuardrailResult("single_minor_step", True, False,
                           f"{current_version}->{target_version} is a valid single-minor step")


def gr_cluster_name_matches(expected_cluster: str, confirmed_cluster: str) -> GuardrailResult:
    """The human must have typed the exact cluster name they intend to upgrade."""
    if confirmed_cluster != expected_cluster:
        return GuardrailResult(
            "cluster_name_confirmation", passed=False, blocked=True, severity="CRITICAL",
            detail=f"confirmed cluster '{confirmed_cluster}' != target cluster '{expected_cluster}'",
        )
    return GuardrailResult("cluster_name_confirmation", True, False,
                           f"cluster '{expected_cluster}' confirmed by human")


def gr_region_allowed(region: str) -> GuardrailResult:
    """Block upgrades in regions not on the allow-list (prevents wrong-account/region mistakes)."""
    allowed = settings.ALLOWED_REGIONS
    if allowed and region not in allowed:
        return GuardrailResult(
            "region_allowlist", passed=False, blocked=True, severity="CRITICAL",
            detail=f"region '{region}' not in allow-list {allowed}",
        )
    return GuardrailResult("region_allowlist", True, False, f"region '{region}' allowed")


def gr_prod_requires_two_person(cluster_name: str) -> GuardrailResult:
    """Production clusters should require two-person approval if configured."""
    is_prod = any(tag in cluster_name.lower() for tag in settings.PROD_CLUSTER_MARKERS)
    if is_prod and settings.REQUIRE_TWO_PERSON_FOR_PROD:
        return GuardrailResult(
            "prod_two_person", passed=True, blocked=False, severity="WARN",
            detail=f"'{cluster_name}' looks like production — two-person approval is REQUIRED",
        )
    return GuardrailResult("prod_two_person", True, False,
                           f"'{cluster_name}' two-person rule not triggered")


def gr_precheck_verdict(evidence: str) -> GuardrailResult:
    """Block if the read-only pre-check evidence contained an UNSAFE / NO-GO verdict."""
    low = (evidence or "").lower()
    bad_signals = [
        "unsafe", "no-go", "downgrade", "cannot skip", "no compatible version",
        "could freeze", "quota headroom is too low",  # surge-capacity shortfall
        "pdbs too loose", "pdbs too strict", "pdbs look misconfigured",  # PDB-strength failures
    ]
    hit = next((s for s in bad_signals if s in low), None)
    if hit:
        return GuardrailResult(
            "precheck_verdict", passed=False, blocked=True, severity="CRITICAL",
            detail=f"pre-check evidence contains blocking signal: '{hit}'",
        )
    return GuardrailResult("precheck_verdict", True, False, "pre-check evidence has no blocking signals")


# ─── operation-specific guardrails (launch / scale / addon / teardown) ────

def gr_cluster_absent_for_launch(cluster_status: str) -> GuardrailResult:
    """Launch must NOT run against a cluster that already exists.

    `cluster_status` is what AWS reports: 'ABSENT' (does not exist) is the only
    safe state to create into. Anything else (ACTIVE/CREATING/UPDATING/...) means
    a cluster with this name already exists — refuse, so a launch can never
    clobber or duplicate a live cluster.
    """
    status = (cluster_status or "").strip().upper()
    if status != "ABSENT":
        return GuardrailResult(
            "cluster_absent_for_launch", passed=False, blocked=True, severity="CRITICAL",
            detail=f"cluster already exists (status '{status}') — refusing to launch over it",
        )
    return GuardrailResult("cluster_absent_for_launch", True, False,
                           "no existing cluster with this name — safe to launch")


def gr_cluster_active_for_op(cluster_status: str, operation: str) -> GuardrailResult:
    """Scale/addon/teardown require an existing ACTIVE cluster (not mid-update)."""
    status = (cluster_status or "").strip().upper()
    if status != "ACTIVE":
        return GuardrailResult(
            "cluster_active_for_op", passed=False, blocked=True, severity="CRITICAL",
            detail=f"cluster status '{status}' is not ACTIVE — refusing {operation}",
        )
    return GuardrailResult("cluster_active_for_op", True, False,
                           f"cluster ACTIVE — {operation} may proceed")


def gr_node_count_sane(desired: int, min_allowed: int = 1, max_allowed: int = 100) -> GuardrailResult:
    """Scale target must be a sane, bounded node count.

    Blocks scale-to-zero (which would take the cluster's workloads down) and
    absurd counts (fat-finger protection). Bounds are conservative defaults.
    """
    try:
        n = int(desired)
    except (TypeError, ValueError):
        return GuardrailResult("node_count_sane", False, True, severity="CRITICAL",
                               detail=f"desired node count '{desired}' is not an integer")
    if n < min_allowed:
        return GuardrailResult("node_count_sane", False, True, severity="CRITICAL",
                               detail=f"desired {n} < min {min_allowed} (scale-to-zero/negative refused)")
    if n > max_allowed:
        return GuardrailResult("node_count_sane", False, True, severity="CRITICAL",
                               detail=f"desired {n} > max {max_allowed} (likely a mistake — refused)")
    return GuardrailResult("node_count_sane", True, False, f"desired node count {n} within bounds")


def gr_addon_named(addon_name: str) -> GuardrailResult:
    """Addon update must name a specific addon (never a blank/all-addons update)."""
    name = (addon_name or "").strip()
    if not name:
        return GuardrailResult("addon_named", False, True, severity="CRITICAL",
                               detail="no addon name given — refusing a blanket addon change")
    return GuardrailResult("addon_named", True, False, f"addon '{name}' named")


def gr_teardown_double_confirm(expected_cluster: str, confirm1: str, confirm2: str) -> GuardrailResult:
    """Teardown requires the cluster name typed TWICE, both exact.

    Destroying a cluster is irreversible and total. On top of the shared
    cluster-name confirmation, teardown demands a second identical typed
    confirmation so it cannot happen from a single mistyped/auto-filled prompt.
    """
    if confirm1 != expected_cluster or confirm2 != expected_cluster:
        return GuardrailResult(
            "teardown_double_confirm", passed=False, blocked=True, severity="CRITICAL",
            detail="teardown requires the exact cluster name typed twice — mismatch, refusing",
        )
    return GuardrailResult("teardown_double_confirm", True, False,
                           f"teardown of '{expected_cluster}' double-confirmed by human")


def gr_teardown_not_prod(cluster_name: str) -> GuardrailResult:
    """Teardown of a production-looking cluster is blocked by default.

    Two-person approval is still required (see gates.ALWAYS_TWO_PERSON_OPERATIONS),
    but destroying something that looks like production is refused at the
    guardrail layer entirely — a deliberate hard stop, not a warning.
    """
    if any(tag in cluster_name.lower() for tag in settings.PROD_CLUSTER_MARKERS):
        return GuardrailResult(
            "teardown_not_prod", passed=False, blocked=True, severity="CRITICAL",
            detail=f"'{cluster_name}' looks like production — teardown refused by guardrail",
        )
    return GuardrailResult("teardown_not_prod", True, False,
                           f"'{cluster_name}' is not production — teardown may be considered")


# ─── orchestration ─────────────────────────────────────────────────────

def run_all_guardrails(
    *,
    current_version: str,
    target_version: str,
    expected_cluster: str,
    confirmed_cluster: str,
    region: str,
    evidence: str,
) -> GuardrailReport:
    """Run every guardrail and return a report. Executor blocks if report.blocked."""
    report = GuardrailReport()
    report.add(gr_version_format(target_version))
    report.add(gr_single_minor_step(current_version, target_version))
    report.add(gr_cluster_name_matches(expected_cluster, confirmed_cluster))
    report.add(gr_region_allowed(region))
    report.add(gr_prod_requires_two_person(expected_cluster))
    report.add(gr_precheck_verdict(evidence))

    logger.info("Guardrails: %s", "BLOCKED" if report.blocked else "CLEARED")
    return report


def run_launch_guardrails(
    *,
    target_version: str,
    expected_cluster: str,
    confirmed_cluster: str,
    region: str,
    cluster_status: str,
    evidence: str,
) -> GuardrailReport:
    """Guardrails for CREATING a new cluster.

    Reuses the operation-agnostic checks (typed cluster-name match, region
    allow-list, precheck verdict) and swaps the upgrade-only single-minor-step
    check for "cluster must NOT already exist". Version must still be well-formed.
    """
    report = GuardrailReport()
    report.add(gr_version_format(target_version))
    report.add(gr_cluster_absent_for_launch(cluster_status))
    report.add(gr_cluster_name_matches(expected_cluster, confirmed_cluster))
    report.add(gr_region_allowed(region))
    report.add(gr_prod_requires_two_person(expected_cluster))
    report.add(gr_precheck_verdict(evidence))
    logger.info("Launch guardrails: %s", "BLOCKED" if report.blocked else "CLEARED")
    return report


def run_scale_guardrails(
    *,
    desired_nodes: int,
    expected_cluster: str,
    confirmed_cluster: str,
    region: str,
    cluster_status: str,
    evidence: str,
) -> GuardrailReport:
    """Guardrails for scaling a node group (change desired node count)."""
    report = GuardrailReport()
    report.add(gr_node_count_sane(desired_nodes))
    report.add(gr_cluster_active_for_op(cluster_status, "scale"))
    report.add(gr_cluster_name_matches(expected_cluster, confirmed_cluster))
    report.add(gr_region_allowed(region))
    report.add(gr_prod_requires_two_person(expected_cluster))
    report.add(gr_precheck_verdict(evidence))
    logger.info("Scale guardrails: %s", "BLOCKED" if report.blocked else "CLEARED")
    return report


def run_addon_guardrails(
    *,
    addon_name: str,
    expected_cluster: str,
    confirmed_cluster: str,
    region: str,
    cluster_status: str,
    evidence: str,
) -> GuardrailReport:
    """Guardrails for updating a cluster addon."""
    report = GuardrailReport()
    report.add(gr_addon_named(addon_name))
    report.add(gr_cluster_active_for_op(cluster_status, "addon"))
    report.add(gr_cluster_name_matches(expected_cluster, confirmed_cluster))
    report.add(gr_region_allowed(region))
    report.add(gr_prod_requires_two_person(expected_cluster))
    report.add(gr_precheck_verdict(evidence))
    logger.info("Addon guardrails: %s", "BLOCKED" if report.blocked else "CLEARED")
    return report


def run_teardown_guardrails(
    *,
    expected_cluster: str,
    confirmed_cluster: str,
    confirmed_cluster_2: str,
    region: str,
    cluster_status: str,
    evidence: str,
) -> GuardrailReport:
    """Guardrails for DESTROYING a cluster — the strictest set.

    On top of the shared checks: the cluster name must be typed TWICE, the
    cluster must currently be ACTIVE (you can't tear down something mid-update),
    and a production-looking cluster is refused outright at the guardrail layer.
    Two-person approval is additionally enforced by the approval gate.
    """
    report = GuardrailReport()
    report.add(gr_cluster_active_for_op(cluster_status, "teardown"))
    report.add(gr_cluster_name_matches(expected_cluster, confirmed_cluster))
    report.add(gr_teardown_double_confirm(expected_cluster, confirmed_cluster, confirmed_cluster_2))
    report.add(gr_teardown_not_prod(expected_cluster))
    report.add(gr_region_allowed(region))
    report.add(gr_precheck_verdict(evidence))
    logger.info("Teardown guardrails: %s", "BLOCKED" if report.blocked else "CLEARED")
    return report
