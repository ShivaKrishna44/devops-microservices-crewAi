"""Unit tests for the deterministic guardrails. No AWS/kubectl/terraform."""
import guardrails as g


# ─── version format ─────────────────────────────────────────────────────

def test_version_format_valid():
    assert g.gr_version_format("1.34").passed is True
    assert g.gr_version_format("1.34").blocked is False


def test_version_format_invalid_blocks():
    for bad in ["abc", "1", "", "v1.34", "1.x"]:
        r = g.gr_version_format(bad)
        assert r.blocked is True, f"{bad!r} should block"
        assert r.severity == "CRITICAL"


# ─── single minor step ──────────────────────────────────────────────────

def test_single_minor_step_valid():
    r = g.gr_single_minor_step("1.33", "1.34")
    assert r.passed is True and r.blocked is False


def test_single_minor_step_downgrade_blocks():
    r = g.gr_single_minor_step("1.34", "1.33")
    assert r.blocked is True and r.severity == "CRITICAL"


def test_single_minor_step_noop_blocks():
    r = g.gr_single_minor_step("1.33", "1.33")
    assert r.blocked is True


def test_single_minor_step_skip_blocks():
    r = g.gr_single_minor_step("1.33", "1.35")
    assert r.blocked is True and "skip" in r.detail.lower()


def test_single_minor_step_major_change_blocks():
    r = g.gr_single_minor_step("1.33", "2.0")
    assert r.blocked is True


def test_single_minor_step_unparseable_blocks():
    r = g.gr_single_minor_step("bad", "1.34")
    assert r.blocked is True and r.severity == "CRITICAL"


# ─── cluster name confirmation ──────────────────────────────────────────

def test_cluster_name_match_passes():
    r = g.gr_cluster_name_matches("expense-dev", "expense-dev")
    assert r.passed is True and r.blocked is False


def test_cluster_name_mismatch_blocks():
    r = g.gr_cluster_name_matches("expense-dev", "expense-prod")
    assert r.blocked is True and r.severity == "CRITICAL"


def test_cluster_name_empty_blocks():
    r = g.gr_cluster_name_matches("expense-dev", "")
    assert r.blocked is True


# ─── region allow-list ──────────────────────────────────────────────────

def test_region_allowed(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "ALLOWED_REGIONS", ["us-east-1"])
    assert g.gr_region_allowed("us-east-1").blocked is False


def test_region_not_allowed_blocks(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "ALLOWED_REGIONS", ["us-east-1"])
    r = g.gr_region_allowed("eu-west-1")
    assert r.blocked is True and r.severity == "CRITICAL"


def test_region_empty_allowlist_allows_any(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "ALLOWED_REGIONS", [])
    assert g.gr_region_allowed("ap-south-1").blocked is False


# ─── prod two-person (warning, not block) ───────────────────────────────

def test_prod_cluster_triggers_two_person_warning(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "PROD_CLUSTER_MARKERS", ["prod", "production", "live"])
    monkeypatch.setattr(settings, "REQUIRE_TWO_PERSON_FOR_PROD", True)
    r = g.gr_prod_requires_two_person("expense-prod")
    assert r.blocked is False           # it's a WARN, not a hard block
    assert r.severity == "WARN"


def test_nonprod_cluster_no_warning(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "PROD_CLUSTER_MARKERS", ["prod", "production", "live"])
    monkeypatch.setattr(settings, "REQUIRE_TWO_PERSON_FOR_PROD", True)
    r = g.gr_prod_requires_two_person("expense-dev")
    assert r.severity != "WARN"


# ─── precheck verdict scan ──────────────────────────────────────────────

def test_precheck_verdict_clean_passes():
    r = g.gr_precheck_verdict("All checks PASS. SAFE to upgrade.")
    assert r.blocked is False


def test_precheck_verdict_unsafe_blocks():
    r = g.gr_precheck_verdict("Node readiness ALERT. Overall verdict: UNSAFE")
    assert r.blocked is True


def test_precheck_verdict_no_compatible_addon_blocks():
    r = g.gr_precheck_verdict("addon vpc-cni: NO compatible version found for 1.34")
    assert r.blocked is True


# ─── full report ────────────────────────────────────────────────────────

def test_run_all_guardrails_clean(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "ALLOWED_REGIONS", ["us-east-1"])
    report = g.run_all_guardrails(
        current_version="1.33",
        target_version="1.34",
        expected_cluster="expense-dev",
        confirmed_cluster="expense-dev",
        region="us-east-1",
        evidence="All checks PASS. SAFE.",
    )
    assert report.blocked is False
    assert "CLEARED" in report.render()


def test_run_all_guardrails_blocks_on_bad_jump(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "ALLOWED_REGIONS", ["us-east-1"])
    report = g.run_all_guardrails(
        current_version="1.33",
        target_version="1.36",           # skips minors -> block
        expected_cluster="expense-dev",
        confirmed_cluster="expense-dev",
        region="us-east-1",
        evidence="All checks PASS.",
    )
    assert report.blocked is True
    assert "BLOCKED" in report.render()


def test_run_all_guardrails_blocks_on_wrong_cluster(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "ALLOWED_REGIONS", ["us-east-1"])
    report = g.run_all_guardrails(
        current_version="1.33",
        target_version="1.34",
        expected_cluster="expense-dev",
        confirmed_cluster="typo-cluster",   # mismatch -> block
        region="us-east-1",
        evidence="All checks PASS.",
    )
    assert report.blocked is True


# ─── surge-capacity freeze signals block via precheck_verdict ────────────

def test_precheck_verdict_blocks_on_surge_freeze():
    evidence = (
        "check_ec2_surge_quota: ALERT: EC2 vCPU quota headroom is too low for the node "
        "surge — the rollover could FREEZE mid-upgrade. quota=40, in-use~=32, surge needs ~32."
    )
    r = g.gr_precheck_verdict(evidence)
    assert r.blocked is True and r.severity == "CRITICAL"


def test_precheck_verdict_allows_when_surge_ok():
    evidence = "check_ec2_surge_quota: PASS: enough EC2 vCPU quota headroom for the node surge."
    r = g.gr_precheck_verdict(evidence)
    assert r.blocked is False
