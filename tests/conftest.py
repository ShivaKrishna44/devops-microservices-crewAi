"""
Pytest setup for the EKS upgrade agent tests.

- Puts the app/ directory on sys.path so `import guardrails`, `import config`,
  and `import approval_gate` work the same way the app runs them.
- Points the approval store at a fresh temp file per test so tests never touch
  real state and don't interfere with each other.

None of these tests touch AWS, kubectl, or terraform — they exercise the pure
safety logic (guardrails + approval gate) only.
"""
import os
import sys

import pytest

APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))
TOOLS_DIR = os.path.join(APP_DIR, "tools")
# Tests import both top-level app modules (config, guardrails, approval_gate, main)
# and tool modules (eks_tools, upgrade_tools, health_tools) by bare name, so put
# BOTH directories on the path.
for _p in (APP_DIR, TOOLS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Give every test its own approval store and reset cached settings."""
    store = tmp_path / "approvals.json"
    monkeypatch.setenv("APPROVAL_STORE", str(store))

    # config.settings is instantiated at import time, so update the live object
    # too (in case config was already imported by a previous test).
    import config
    config.settings.APPROVAL_STORE = str(store)
    yield
