"""
Pure apply-time gate logic — no CrewAI, no LLM, no cluster calls.

Extracted from main.py so it can be unit-tested in isolation (main.py imports
crew.py which pulls in CrewAI). These functions decide, from the persisted
approval record + policy settings, whether an apply may proceed.
"""
from config import settings
import approval_gate


def is_prod(cluster: str) -> bool:
    return any(tag in cluster.lower() for tag in settings.PROD_CLUSTER_MARKERS)


# Operations that ALWAYS require two distinct approvers regardless of whether
# the cluster looks like production — because their blast radius is severe.
# Teardown destroys the cluster; it is gated as hard as (or harder than) a prod
# change even on a dev cluster.
ALWAYS_TWO_PERSON_OPERATIONS = {"teardown"}


def required_approvers(cluster: str, operation: str = "upgrade") -> int:
    """Distinct approvers needed for an operation on a cluster.

    - Teardown (and any ALWAYS_TWO_PERSON_OPERATIONS) always needs two.
    - Production clusters need two when REQUIRE_TWO_PERSON_FOR_PROD is set.
    - Everything else needs one.
    """
    if operation in ALWAYS_TWO_PERSON_OPERATIONS:
        return 2
    if is_prod(cluster) and settings.REQUIRE_TWO_PERSON_FOR_PROD:
        return 2
    return 1


def preapply_gate(target_version: str, operation: str = "upgrade", target: str = None):
    """Return (ok, reason). The apply path calls this and refuses if not ok.

    Checks: an approved request exists for this (operation, target), status is
    APPROVED, and that at least the required number of DISTINCT approvers is on
    record (belt-and-suspenders, even if stored required_approvers was lower).
    Defaults preserve the original upgrade behavior (target = target_version).
    """
    if target is None:
        target = target_version
    cur = approval_gate.get_current()
    rec_op = (cur or {}).get("operation", "upgrade")
    rec_target = (cur or {}).get("target", (cur or {}).get("target_version"))
    if not cur or rec_op != operation or rec_target != target:
        return False, f"no approved request for {operation}/{target}"
    if cur.get("status") != "APPROVED":
        return False, f"status is {cur.get('status')} (need APPROVED)"

    cluster = cur.get("cluster_name", "")
    needed = required_approvers(cluster, operation)
    distinct = len({a.get("actor") for a in cur.get("approvers", [])})
    if distinct < needed:
        return False, (f"'{cluster}' ({operation}) requires {needed} distinct approver(s) but only "
                       f"{distinct} recorded — refusing to apply")
    return True, "approved"
