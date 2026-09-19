from monitor.performance_monitor import PerformanceMonitor


class EmptyStats:
    def get_stats(self):
        return {}


def make_monitor():
    return PerformanceMonitor(EmptyStats(), EmptyStats())


def test_repeated_threshold_breach_creates_one_active_alert():
    monitor = make_monitor()

    for _ in range(5):
        monitor._check_threshold("agent_avg_ms", 4500, "general")

    active = monitor.summary()["active_alerts"]
    assert len(active) == 1
    assert active[0]["metric"] == "agent_avg_ms:general"
    assert active[0]["occurrences"] == 5


def test_alert_resolves_after_sustained_recovery_without_flapping():
    monitor = make_monitor()

    for _ in range(3):
        monitor._check_threshold("agent_avg_ms", 4500, "general")
    for value in (2900, 2800, 2600):
        monitor._check_threshold("agent_avg_ms", value, "general")
    assert len(monitor.summary()["active_alerts"]) == 1

    for _ in range(2):
        monitor._check_threshold("agent_avg_ms", 2600, "general")

    summary = monitor.summary()
    assert summary["active_alerts"] == []
    assert len(summary["recent_resolved_alerts"]) == 1
    assert summary["recent_resolved_alerts"][0]["resolved"] is True
