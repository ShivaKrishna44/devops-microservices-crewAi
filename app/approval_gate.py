"""
Approval gate — the core human-in-the-loop safety mechanism (hardened).

An EKS upgrade apply is BLOCKED until humans record APPROVE decisions for a
specific target version. Hardened properties:

  1. Evidence binding — approval is tied to a SHA-256 hash of the exact
     pre-check + plan evidence that was reviewed. If the plan changes, the old
     approval no longer matches and the apply is refused.
  2. Expiry (TTL) — approvals go stale after APPROVAL_TTL_MINUTES so a
     long-forgotten approval can't be used to apply a now-different cluster.
  3. Two-person option — production clusters can require N distinct approvers.
  4. Version binding — approving 1.34 never approves 1.35.

Everything is persisted to a JSON store (append-only history + current record)
so the whole decision chain is auditable.
"""
import hashlib
import json
import os
from datetime import datetime, timezone, timedelta

from config import settings, logger


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def evidence_hash(evidence: str) -> str:
    """Stable SHA-256 of the evidence text the human reviewed."""
    return hashlib.sha256((evidence or "").encode("utf-8")).hexdigest()


def _load() -> dict:
    path = settings.APPROVAL_STORE
    if not os.path.exists(path):
        return {"current": None, "history": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("Approval store unreadable; starting fresh.")
        return {"current": None, "history": []}


def _save(data: dict) -> None:
    path = settings.APPROVAL_STORE
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ─── operation identity ─────────────────────────────────────────────────
# The gate was originally single-operation ("upgrade") and keyed purely on
# target_version. It now supports multiple cluster operations (upgrade, launch,
# scale, addon, teardown). Identity is the (operation, target) pair.
#
# Backward compatibility is deliberate and load-bearing:
#   * `operation` defaults to "upgrade" everywhere.
#   * A stored record with NO "operation" key is treated as "upgrade" (older
#     records + existing tests keep matching exactly as before).
#   * For "upgrade", the target IS the target_version, so the historical
#     target_version binding is preserved unchanged.

DEFAULT_OPERATION = "upgrade"


def _record_operation(rec: dict) -> str:
    """Operation of a stored record; missing key => 'upgrade' (back-compat)."""
    return (rec or {}).get("operation", DEFAULT_OPERATION)


def _record_target(rec: dict) -> str:
    """Canonical target of a stored record.

    For upgrade this is target_version (preserving the original binding). For
    other operations it's the explicit 'target' field, falling back to
    target_version if absent.
    """
    rec = rec or {}
    return rec.get("target", rec.get("target_version", ""))


def _identity_matches(rec: dict, operation: str, target: str) -> bool:
    """True if a stored record is for exactly this (operation, target)."""
    return _record_operation(rec) == operation and _record_target(rec) == target


# ─── recording ────────────────────────────────────────────────────────

def record_request(target_version: str, current_version: str, evidence: str,
                   cluster_name: str, required_approvers: int = 1,
                   aws_account_id: str = "", region: str = "",
                   operation: str = DEFAULT_OPERATION, target: str = None) -> str:
    """Open a PENDING request bound to the reviewed evidence.

    Also binds the AWS account + region so an approval for 'expense-dev' in one
    account can't authorize an operation on a same-named cluster in another.
    Returns the evidence hash the approver(s) are approving against.

    `operation` is the cluster operation (upgrade/launch/scale/addon/teardown).
    `target` is the operation-specific descriptor the approval is keyed on
    (for upgrade it is the target_version; for other ops it can be a node count,
    addon name, or the cluster name). Defaults preserve the original
    upgrade-only behavior exactly.
    """
    if target is None:
        target = target_version
    ev_hash = evidence_hash(evidence)
    data = _load()
    data["current"] = {
        "operation": operation,
        "target": target,
        "target_version": target_version,
        "current_version": current_version,
        "cluster_name": cluster_name,
        "aws_account_id": aws_account_id,
        "region": region,
        "status": "PENDING",
        "evidence_hash": ev_hash,   # hash only — raw plan text is NOT persisted
        "required_approvers": max(1, int(required_approvers)),
        "approvers": [],          # list of {actor, reason, at}
        "requested_at": _iso(_now()),
        "decided_at": None,
    }
    data["history"].append({"event": "REQUESTED", "operation": operation,
                            "target": target, "target_version": target_version,
                            "cluster": cluster_name, "account": aws_account_id,
                            "region": region, "evidence_hash": ev_hash, "at": _iso(_now())})
    _save(data)
    logger.info("%s request opened: target=%s on %s (needs %d approver(s))",
                operation.upper(), target, cluster_name, required_approvers)
    return ev_hash


def record_approval(target_version: str, evidence: str, actor: str, reason: str,
                    operation: str = DEFAULT_OPERATION, target: str = None) -> str:
    """Record ONE human approval. Returns the resulting status.

    Verifies the evidence still matches what was requested. When the number of
    distinct approvers reaches required_approvers, status becomes APPROVED.

    Matching is on the (operation, target) identity. Defaults preserve the
    original upgrade behavior: operation='upgrade', target=target_version.
    """
    if target is None:
        target = target_version
    data = _load()
    cur = data.get("current")
    if not cur or not _identity_matches(cur, operation, target):
        raise ValueError(f"No pending {operation} request for {target}.")
    if cur.get("status") == "REJECTED":
        raise ValueError("Request already REJECTED; reset before approving.")
    if cur.get("evidence_hash") != evidence_hash(evidence):
        raise ValueError("Evidence changed since request — refusing to approve a different plan.")

    approvers = cur.get("approvers", [])
    if any(a["actor"] == actor for a in approvers):
        raise ValueError(f"{actor} has already approved; a distinct second approver is required.")

    approvers.append({"actor": actor, "reason": reason or "approved", "at": _iso(_now())})
    cur["approvers"] = approvers

    if len(approvers) >= cur.get("required_approvers", 1):
        cur["status"] = "APPROVED"
        cur["decided_at"] = _iso(_now())
    else:
        cur["status"] = "PARTIALLY_APPROVED"

    data["current"] = cur
    data["history"].append({"event": "APPROVAL", "operation": operation,
                            "target": target, "target_version": target_version,
                            "actor": actor, "reason": reason, "at": _iso(_now())})
    _save(data)
    logger.info("Approval by %s recorded (%d/%d). Status=%s",
                actor, len(approvers), cur.get("required_approvers", 1), cur["status"])
    return cur["status"]


def record_rejection(target_version: str, actor: str, reason: str,
                     operation: str = DEFAULT_OPERATION, target: str = None) -> None:
    if target is None:
        target = target_version
    data = _load()
    cur = data.get("current")
    if not cur or not _identity_matches(cur, operation, target):
        raise ValueError(f"No pending {operation} request for {target}.")
    cur["status"] = "REJECTED"
    cur["decided_at"] = _iso(_now())
    data["current"] = cur
    data["history"].append({"event": "REJECTED", "operation": operation,
                            "target": target, "target_version": target_version,
                            "actor": actor, "reason": reason, "at": _iso(_now())})
    _save(data)
    logger.info("Rejection by %s recorded for %s", actor, target_version)


# ─── verification (used by the gated executor) ──────────────────────────

def approval_check(target_version: str, evidence: str,
                   aws_account_id: str = "", region: str = "",
                   operation: str = DEFAULT_OPERATION, target: str = None) -> tuple[bool, str]:
    """Return (ok, reason). The executor calls this and refuses apply if not ok.

    Verifies ALL of: exists, right (operation, target), status APPROVED,
    evidence hash matches, enough approvers, not expired, and (when provided)
    the AWS account + region match what was approved — so an approval for a
    cluster in one account can't authorize a same-named cluster in another.
    """
    if target is None:
        target = target_version
    cur = _load().get("current")
    if not cur:
        return False, "no approval on record"
    if not _identity_matches(cur, operation, target):
        return False, (f"approval on file is for {_record_operation(cur)}/{_record_target(cur)}, "
                       f"not {operation}/{target}")
    if cur.get("status") != "APPROVED":
        return False, f"status is {cur.get('status')}, not APPROVED"
    if cur.get("evidence_hash") != evidence_hash(evidence):
        return False, "evidence/plan changed since approval — refusing"
    if len(cur.get("approvers", [])) < cur.get("required_approvers", 1):
        return False, "not enough distinct approvers"

    # Account/region binding — only enforced when the caller supplies them AND
    # the approval recorded them (backward compatible with older records).
    if aws_account_id and cur.get("aws_account_id") and cur.get("aws_account_id") != aws_account_id:
        return False, (f"approval is for account {cur.get('aws_account_id')}, "
                       f"not {aws_account_id} — refusing")
    if region and cur.get("region") and cur.get("region") != region:
        return False, f"approval is for region {cur.get('region')}, not {region} — refusing"

    # TTL check
    ttl = settings.APPROVAL_TTL_MINUTES
    decided_at = cur.get("decided_at")
    if ttl and decided_at:
        try:
            decided = datetime.fromisoformat(decided_at)
            if _now() - decided > timedelta(minutes=ttl):
                return False, f"approval expired (older than {ttl} min) — re-approve"
        except ValueError:
            return False, "approval timestamp unreadable — re-approve"

    return True, "approved"


def is_approved(target_version: str, evidence: str = "",
                operation: str = DEFAULT_OPERATION, target: str = None) -> bool:
    ok, _ = approval_check(target_version, evidence, operation=operation, target=target)
    return ok


def get_approved_target():
    cur = _load().get("current")
    if cur and cur.get("status") == "APPROVED":
        return cur.get("target_version")
    return None


def get_approved_identity():
    """Return (operation, target) of the current APPROVED record, else None."""
    cur = _load().get("current")
    if cur and cur.get("status") == "APPROVED":
        return _record_operation(cur), _record_target(cur)
    return None


def get_current():
    return _load().get("current")


def reset() -> None:
    data = _load()
    if data.get("current"):
        data["history"].append({"event": "RESET",
                                "target_version": data["current"].get("target_version"),
                                "at": _iso(_now())})
    data["current"] = None
    _save(data)
    logger.info("Approval gate reset.")
