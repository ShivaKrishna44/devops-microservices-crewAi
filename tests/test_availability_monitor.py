"""
Unit tests for the live availability breach detector. No cluster.

We pass a baseline dict directly and monkeypatch `_collect_health` for the
"current" reading, exercising the pure floor math and critical-namespace logic.
"""
import health_tools as ht


def _snap(ready_map):
    return {
        "nodes": {"ip-1": "Ready"},
        "node_names": ["ip-1"],
        "unhealthy_pods": [],
        "degraded_workloads": [],
        "replica_map": {k: str(v) for k, v in ready_map.items()},
        "ready_map": dict(ready_map),
        "collected_at": 0,
    }


def _baseline(ready_map):
    return _snap(ready_map)


def test_no_breach_when_fully_healthy(monkeypatch):
    base = _baseline({"order-service/order": 5})
    monkeypatch.setattr(ht, "_collect_health", lambda: _snap({"order-service/order": 5}))
    breached, breaches = ht.check_availability_breach(base)
    assert breached is False and breaches == []


def test_no_breach_at_exactly_floor(monkeypatch, ):
    # 20% threshold, baseline 5 -> floor = ceil(5*0.8) = 4. Current 4 is OK.
    monkeypatch.setattr(ht.settings, "AVAILABILITY_DROP_THRESHOLD", 0.20)
    monkeypatch.setattr(ht.settings, "CRITICAL_NAMESPACES", ["order-service"])
    base = _baseline({"order-service/order": 5})
    monkeypatch.setattr(ht, "_collect_health", lambda: _snap({"order-service/order": 4}))
    breached, _ = ht.check_availability_breach(base)
    assert breached is False


def test_breach_when_below_floor(monkeypatch):
    # baseline 5, floor 4, current 3 -> breach (>20% drop)
    monkeypatch.setattr(ht.settings, "AVAILABILITY_DROP_THRESHOLD", 0.20)
    monkeypatch.setattr(ht.settings, "CRITICAL_NAMESPACES", ["order-service"])
    base = _baseline({"order-service/order": 5})
    monkeypatch.setattr(ht, "_collect_health", lambda: _snap({"order-service/order": 3}))
    breached, breaches = ht.check_availability_breach(base)
    assert breached is True
    assert any("order-service/order" in b for b in breaches)


def test_ignores_non_critical_namespace(monkeypatch):
    # only payment-service is critical; order-service dropping is ignored
    monkeypatch.setattr(ht.settings, "AVAILABILITY_DROP_THRESHOLD", 0.20)
    monkeypatch.setattr(ht.settings, "CRITICAL_NAMESPACES", ["payment-service"])
    base = _baseline({"order-service/order": 5})
    monkeypatch.setattr(ht, "_collect_health", lambda: _snap({"order-service/order": 0}))
    breached, _ = ht.check_availability_breach(base)
    assert breached is False


def test_ignores_single_replica_workload(monkeypatch):
    # baseline healthy = 1 -> not an availability-floor concern (base_n <= 1)
    monkeypatch.setattr(ht.settings, "CRITICAL_NAMESPACES", ["order-service"])
    base = _baseline({"order-service/singleton": 1})
    monkeypatch.setattr(ht, "_collect_health", lambda: _snap({"order-service/singleton": 0}))
    breached, _ = ht.check_availability_breach(base)
    assert breached is False


def test_no_baseline_ready_map_degrades_gracefully(monkeypatch):
    # empty baseline ready_map -> nothing to compare, no false alarm
    monkeypatch.setattr(ht, "_collect_health", lambda: _snap({"order-service/order": 0}))
    breached, breaches = ht.check_availability_breach({"ready_map": {}})
    assert breached is False and breaches == []


def test_kube_system_never_critical(monkeypatch):
    monkeypatch.setattr(ht.settings, "CRITICAL_NAMESPACES", [])  # empty = all app ns
    base = _baseline({"kube-system/coredns": 2})
    monkeypatch.setattr(ht, "_collect_health", lambda: _snap({"kube-system/coredns": 0}))
    breached, _ = ht.check_availability_breach(base)
    assert breached is False  # kube-system is excluded from the availability alarm


def test_tool_wrapper_returns_alarm_string(monkeypatch):
    monkeypatch.setattr(ht.settings, "AVAILABILITY_DROP_THRESHOLD", 0.20)
    monkeypatch.setattr(ht.settings, "CRITICAL_NAMESPACES", ["order-service"])
    monkeypatch.setattr(ht, "_load_baseline", lambda: _baseline({"order-service/order": 5}))
    monkeypatch.setattr(ht, "_collect_health", lambda: _snap({"order-service/order": 2}))
    fn = getattr(ht.check_availability_breach_tool, "func", None) or ht.check_availability_breach_tool
    out = fn()
    assert "ALARM" in out


def test_sound_alarm_never_raises(monkeypatch):
    # no webhook configured -> just logs, must not raise
    monkeypatch.setattr(ht.settings, "ALARM_WEBHOOK_URL", "")
    ht.sound_alarm("test message")  # should return cleanly


# ─── halt_node_draining (stop the bleeding) ──────────────────────────────

def test_halt_cordons_only_old_nodes_still_present(monkeypatch):
    # baseline had old-1, old-2; now old-1 remains + a new node -> cordon old-1 only
    baseline = {"node_names": ["old-1", "old-2"]}
    monkeypatch.setattr(ht, "_collect_health",
                        lambda: {"node_names": ["old-1", "new-1"]})
    cordoned = []
    def fake_kubectl(cmd, timeout=30):
        if cmd.startswith("cordon "):
            cordoned.append(cmd.split()[1])
            return "node/... cordoned"
        return "ERROR: unexpected"
    monkeypatch.setattr(ht, "_kubectl", fake_kubectl)

    result = ht.halt_node_draining(baseline)
    assert result["halted"] is True
    assert cordoned == ["old-1"]          # only the old node still present
    assert "old-2" not in cordoned        # already gone -> not cordoned


def test_halt_when_no_old_nodes_left(monkeypatch):
    baseline = {"node_names": ["old-1"]}
    monkeypatch.setattr(ht, "_collect_health", lambda: {"node_names": ["new-1", "new-2"]})
    monkeypatch.setattr(ht, "_kubectl", lambda cmd, timeout=30: "should not be called")
    result = ht.halt_node_draining(baseline)
    assert result["halted"] is False
    assert "no old baseline nodes" in result["note"].lower()


def test_halt_never_raises_on_kubectl_error(monkeypatch):
    baseline = {"node_names": ["old-1"]}
    monkeypatch.setattr(ht, "_collect_health", lambda: {"node_names": ["old-1"]})
    monkeypatch.setattr(ht, "_kubectl", lambda cmd, timeout=30: "ERROR: forbidden")
    result = ht.halt_node_draining(baseline)   # must not raise
    assert result["halted"] is False
    assert result["errors"]                     # error recorded, not raised


# ─── monitor triggers halt on breach (enabled) / not (disabled) ──────────

class _FakeStop:
    """Minimal threading.Event stand-in: 'set' after N wait() calls."""
    def __init__(self, stop_after=1):
        self._n = 0
        self._stop_after = stop_after
    def is_set(self):
        return self._n >= self._stop_after
    def wait(self, _):
        self._n += 1


def test_monitor_halts_on_breach_when_enabled(monkeypatch):
    monkeypatch.setattr(ht.settings, "HALT_ON_AVAILABILITY_BREACH", True)
    monkeypatch.setattr(ht, "_load_baseline", lambda: {"node_names": ["old-1"]})
    monkeypatch.setattr(ht, "check_availability_breach",
                        lambda baseline=None: (True, ["order-service/order: 1/5 healthy"]))
    monkeypatch.setattr(ht, "sound_alarm", lambda msg: None)
    halt_called = {"n": 0}
    monkeypatch.setattr(ht, "halt_node_draining",
                        lambda baseline=None: (halt_called.__setitem__("n", halt_called["n"] + 1)
                                               or {"halted": True, "cordoned": ["old-1"]}))
    result = ht.monitor_during_rollover(_FakeStop(stop_after=1), interval_seconds=0)
    assert result["alarm"] is True
    assert result["halted"] is True
    assert halt_called["n"] == 1          # halt fired exactly once


def test_monitor_alarm_only_when_halt_disabled(monkeypatch):
    monkeypatch.setattr(ht.settings, "HALT_ON_AVAILABILITY_BREACH", False)
    monkeypatch.setattr(ht, "_load_baseline", lambda: {"node_names": ["old-1"]})
    monkeypatch.setattr(ht, "check_availability_breach",
                        lambda baseline=None: (True, ["order-service/order: 1/5 healthy"]))
    monkeypatch.setattr(ht, "sound_alarm", lambda msg: None)
    halt_called = {"n": 0}
    monkeypatch.setattr(ht, "halt_node_draining",
                        lambda baseline=None: halt_called.__setitem__("n", halt_called["n"] + 1))
    result = ht.monitor_during_rollover(_FakeStop(stop_after=1), interval_seconds=0)
    assert result["alarm"] is True
    assert result["halted"] is False
    assert halt_called["n"] == 0          # halt NOT called when disabled
