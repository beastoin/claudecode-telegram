"""Tests for CPU load and network bandwidth monitoring."""
from __future__ import annotations

import json
import subprocess

# Allow imports from project root
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bridge
import shared
from claudecode import HostHealthState, ResourceAlertState
from shared import ResourceAlertConfig, _res_cfg

METRICS_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "host_metrics.sh"


class _FakeClock:
    def __init__(self, t: float = 10000.0) -> None:
        self.t = t

    def time(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s

    def advance(self, s: float) -> None:
        self.t += s


# ---------------------------------------------------------------------------
# host_metrics.sh script tests — verify the shell script works locally
# ---------------------------------------------------------------------------


class TestMetricsCpuScript:
    """Test metric_cpu in host_metrics.sh."""

    def test_cpu_returns_valid_json(self) -> None:
        r = subprocess.run(
            ["bash", str(METRICS_SCRIPT), "cpu"],
            capture_output=True, text=True, timeout=5,
        )
        assert r.returncode == 0, f"script failed: {r.stderr}"
        data = json.loads(r.stdout)
        assert "load1" in data
        assert "load5" in data
        assert "load15" in data
        assert "ncpu" in data
        assert isinstance(data["ncpu"], int)
        assert data["ncpu"] >= 1
        assert data["load1"] >= 0
        assert data["load5"] >= 0
        assert data["load15"] >= 0

    def test_cpu_has_normalized_load(self) -> None:
        r = subprocess.run(
            ["bash", str(METRICS_SCRIPT), "cpu"],
            capture_output=True, text=True, timeout=5,
        )
        data = json.loads(r.stdout)
        assert "norm5" in data
        expected = round(data["load5"] / data["ncpu"], 2)
        assert abs(data["norm5"] - expected) < 0.02


class TestMetricsNetScript:
    """Test metric_net in host_metrics.sh."""

    def test_net_returns_valid_json(self) -> None:
        r = subprocess.run(
            ["bash", str(METRICS_SCRIPT), "net"],
            capture_output=True, text=True, timeout=5,
        )
        assert r.returncode == 0, f"script failed: {r.stderr}"
        data = json.loads(r.stdout)
        assert "rx_bytes" in data
        assert "tx_bytes" in data
        assert isinstance(data["rx_bytes"], int)
        assert isinstance(data["tx_bytes"], int)
        assert data["rx_bytes"] >= 0
        assert data["tx_bytes"] >= 0

    def test_net_counters_are_cumulative(self) -> None:
        """Two calls should return same or increasing counters."""
        r1 = subprocess.run(
            ["bash", str(METRICS_SCRIPT), "net"],
            capture_output=True, text=True, timeout=5,
        )
        r2 = subprocess.run(
            ["bash", str(METRICS_SCRIPT), "net"],
            capture_output=True, text=True, timeout=5,
        )
        d1 = json.loads(r1.stdout)
        d2 = json.loads(r2.stdout)
        assert d2["rx_bytes"] >= d1["rx_bytes"]
        assert d2["tx_bytes"] >= d1["tx_bytes"]


class TestMetricsAllIncludesNewMetrics:
    """Test that 'all' mode includes cpu and net in output text."""

    def test_all_output_mentions_cpu_and_net(self) -> None:
        r = subprocess.run(
            ["bash", str(METRICS_SCRIPT), "all"],
            capture_output=True, text=True, timeout=15,
        )
        assert r.returncode == 0, f"script failed: {r.stderr}"
        assert '"cpu":' in r.stdout
        assert '"net":' in r.stdout
        assert '"load1"' in r.stdout
        assert '"rx_bytes"' in r.stdout


# ---------------------------------------------------------------------------
# Bridge-level CPU load alert tests
# ---------------------------------------------------------------------------


class TestProbeCpuLoad:
    """Tests _probe_cpu_load_all_hosts alerting."""

    def setup_method(self) -> None:
        self.alerts: list[tuple[str, str]] = []
        self.orig_alert = bridge._watchdog_alert
        bridge._watchdog_alert = lambda cat, txt, **kw: self.alerts.append((cat, txt)) if txt else None

        self.clock = _FakeClock(10000.0)
        self.orig_clock_shared = shared._clock
        self.orig_clock_bridge = bridge._clock
        shared._clock = self.clock
        bridge._clock = self.clock

        self.orig_run_metric = bridge._run_metric
        self.metric_value: dict | None = None
        bridge._run_metric = lambda metric, host=None: self.metric_value

        bridge.host_health.reset()

    def teardown_method(self) -> None:
        bridge._watchdog_alert = self.orig_alert
        shared._clock = self.orig_clock_shared
        bridge._clock = self.orig_clock_bridge
        bridge._run_metric = self.orig_run_metric
        bridge.host_health.reset()

    def _probe(self) -> None:
        bridge._probe_cpu_load_all_hosts(set())

    def test_cpu_load_alert_fires_when_critical(self) -> None:
        self.metric_value = {"load1": 40.0, "load5": 35.0, "load15": 30.0, "ncpu": 8, "norm5": 4.4}
        self._probe()
        assert len(self.alerts) == 1
        assert "CPU load" in self.alerts[0][1]
        assert "4.4" in self.alerts[0][1]

    def test_no_alert_when_below_threshold(self) -> None:
        self.metric_value = {"load1": 2.0, "load5": 1.5, "load15": 1.0, "ncpu": 8, "norm5": 0.19}
        self._probe()
        assert len(self.alerts) == 0

    def test_cpu_load_recovery(self) -> None:
        self.metric_value = {"load1": 40.0, "load5": 35.0, "load15": 30.0, "ncpu": 8, "norm5": 4.4}
        self._probe()
        assert len(self.alerts) == 1
        self.metric_value = {"load1": 2.0, "load5": 1.5, "load15": 1.0, "ncpu": 8, "norm5": 0.19}
        self._probe()
        assert len(self.alerts) == 2
        assert "recovered" in self.alerts[1][1].lower()

    def test_cpu_load_re_alert(self) -> None:
        self.metric_value = {"load1": 40.0, "load5": 35.0, "load15": 30.0, "ncpu": 8, "norm5": 4.4}
        self._probe()
        self.clock.advance(_res_cfg.cpu_load_re_alert)
        self._probe()
        assert len(self.alerts) == 2
        assert "STILL" in self.alerts[1][1]

    def test_cpu_load_stores_usage(self) -> None:
        self.metric_value = {"load1": 4.0, "load5": 3.5, "load15": 3.0, "ncpu": 8, "norm5": 0.44}
        self._probe()
        assert "VPS" in bridge.host_health.cpu_load_usage
        assert bridge.host_health.cpu_load_usage["VPS"]["load5"] == 3.5

    def test_cpu_load_skips_downed_host(self) -> None:
        self.metric_value = {"load1": 40.0, "load5": 35.0, "load15": 30.0, "ncpu": 8, "norm5": 4.4}
        bridge.host_health.down["remote-host"] = True
        bridge._probe_cpu_load_all_hosts({"remote-host"})
        # Only VPS alert fires, downed remote-host skipped
        assert len(self.alerts) == 1
        assert "VPS" in self.alerts[0][1]

    def test_cpu_load_alert_shows_all_load_averages(self) -> None:
        self.metric_value = {"load1": 40.0, "load5": 35.0, "load15": 30.0, "ncpu": 8, "norm5": 4.4}
        self._probe()
        text = self.alerts[0][1]
        assert "40.0" in text or "40" in text
        assert "35.0" in text or "35" in text
        assert "8" in text  # ncpu


# ---------------------------------------------------------------------------
# Bridge-level network bandwidth alert tests
# ---------------------------------------------------------------------------


class TestProbeNet:
    """Tests _probe_net_all_hosts rate computation and alerting."""

    def setup_method(self) -> None:
        self.alerts: list[tuple[str, str]] = []
        self.orig_alert = bridge._watchdog_alert
        bridge._watchdog_alert = lambda cat, txt, **kw: self.alerts.append((cat, txt)) if txt else None

        self.clock = _FakeClock(10000.0)
        self.orig_clock_shared = shared._clock
        self.orig_clock_bridge = bridge._clock
        shared._clock = self.clock
        bridge._clock = self.clock

        self.orig_run_metric = bridge._run_metric
        self.metric_value: dict | None = None
        bridge._run_metric = lambda metric, host=None: self.metric_value

        bridge.host_health.reset()

    def teardown_method(self) -> None:
        bridge._watchdog_alert = self.orig_alert
        shared._clock = self.orig_clock_shared
        bridge._clock = self.orig_clock_bridge
        bridge._run_metric = self.orig_run_metric
        bridge.host_health.reset()

    def _probe(self) -> None:
        bridge._probe_net_all_hosts(set())

    def test_first_probe_no_alert_no_rate(self) -> None:
        """First probe has no previous sample, so no rate and no alert."""
        self.metric_value = {"rx_bytes": 1_000_000_000, "tx_bytes": 500_000_000}
        self._probe()
        assert len(self.alerts) == 0
        usage = bridge.host_health.net_usage.get("VPS")
        assert usage is not None
        assert usage["rx_mbps"] == 0.0  # no rate on first sample

    def test_rate_computed_on_second_probe(self) -> None:
        """Rate computed from delta between two probes."""
        self.metric_value = {"rx_bytes": 1_000_000_000, "tx_bytes": 500_000_000}
        self._probe()
        self.clock.advance(10.0)  # 10 seconds later
        # 100MB more received, 50MB more sent
        self.metric_value = {"rx_bytes": 1_100_000_000, "tx_bytes": 550_000_000}
        self._probe()
        usage = bridge.host_health.net_usage["VPS"]
        # 100MB in 10s = 10 MB/s, but in MiB/s it's 100000000/10/1048576 ≈ 9.5
        assert usage["rx_mbps"] > 9.0
        assert usage["rx_mbps"] < 11.0
        assert usage["tx_mbps"] > 4.0
        assert usage["tx_mbps"] < 6.0

    def test_net_alert_fires_on_high_bandwidth(self) -> None:
        """Alert fires when bandwidth exceeds threshold."""
        self.metric_value = {"rx_bytes": 0, "tx_bytes": 0}
        self._probe()
        self.clock.advance(1.0)
        # 600MB in 1 second = 600 MB/s — well above threshold
        self.metric_value = {"rx_bytes": 600_000_000, "tx_bytes": 0}
        self._probe()
        assert len(self.alerts) == 1
        assert "Network" in self.alerts[0][1] or "net" in self.alerts[0][1].lower()

    def test_no_alert_below_threshold(self) -> None:
        """Normal bandwidth doesn't alert."""
        self.metric_value = {"rx_bytes": 0, "tx_bytes": 0}
        self._probe()
        self.clock.advance(10.0)
        # 10MB in 10s = 1 MB/s — well below threshold
        self.metric_value = {"rx_bytes": 10_000_000, "tx_bytes": 5_000_000}
        self._probe()
        assert len(self.alerts) == 0

    def test_net_recovery(self) -> None:
        """Alert then recovery when bandwidth drops."""
        self.metric_value = {"rx_bytes": 0, "tx_bytes": 0}
        self._probe()
        self.clock.advance(1.0)
        self.metric_value = {"rx_bytes": 600_000_000, "tx_bytes": 0}
        self._probe()
        assert len(self.alerts) == 1
        self.clock.advance(10.0)
        # Only 1MB in 10s = normal
        self.metric_value = {"rx_bytes": 601_000_000, "tx_bytes": 0}
        self._probe()
        assert len(self.alerts) == 2
        assert "recovered" in self.alerts[1][1].lower()

    def test_net_re_alert(self) -> None:
        """Sustained high bandwidth triggers re-alert."""
        self.metric_value = {"rx_bytes": 0, "tx_bytes": 0}
        self._probe()
        self.clock.advance(1.0)
        self.metric_value = {"rx_bytes": 600_000_000, "tx_bytes": 0}
        self._probe()
        assert len(self.alerts) == 1
        self.clock.advance(_res_cfg.net_re_alert)
        # Sustained: 600MB/s * 900s = ~540GB more in the interval
        bytes_in_interval = int(600_000_000 * _res_cfg.net_re_alert)
        self.metric_value = {"rx_bytes": 600_000_000 + bytes_in_interval, "tx_bytes": 0}
        self._probe()
        assert len(self.alerts) == 2
        assert "STILL" in self.alerts[1][1]

    def test_counter_wraparound_ignored(self) -> None:
        """If counters decrease (reboot), skip rate computation."""
        self.metric_value = {"rx_bytes": 1_000_000_000, "tx_bytes": 500_000_000}
        self._probe()
        self.clock.advance(10.0)
        # Counters reset (reboot)
        self.metric_value = {"rx_bytes": 100, "tx_bytes": 50}
        self._probe()
        usage = bridge.host_health.net_usage["VPS"]
        assert usage["rx_mbps"] == 0.0
        assert usage["tx_mbps"] == 0.0
        assert len(self.alerts) == 0


# ---------------------------------------------------------------------------
# HostHealthState has new fields
# ---------------------------------------------------------------------------


class TestHostHealthStateNewFields:
    def test_cpu_load_fields_exist(self) -> None:
        h = HostHealthState()
        assert hasattr(h, "cpu_load_usage")
        assert hasattr(h, "cpu_load_alert")
        assert isinstance(h.cpu_load_usage, dict)
        assert isinstance(h.cpu_load_alert, dict)

    def test_net_fields_exist(self) -> None:
        h = HostHealthState()
        assert hasattr(h, "net_usage")
        assert hasattr(h, "net_alert")
        assert hasattr(h, "_net_prev")
        assert isinstance(h.net_usage, dict)
        assert isinstance(h.net_alert, dict)

    def test_reset_clears_new_fields(self) -> None:
        h = HostHealthState()
        h.cpu_load_usage["VPS"] = {"load1": 1.0}
        h.net_usage["VPS"] = {"rx_mbps": 10.0}
        h._net_prev["VPS"] = {"rx_bytes": 100}
        h.reset()
        assert len(h.cpu_load_usage) == 0
        assert len(h.net_usage) == 0
        assert len(h._net_prev) == 0


# ---------------------------------------------------------------------------
# ResourceAlertConfig has new thresholds
# ---------------------------------------------------------------------------


class TestResourceAlertConfigNewFields:
    def test_cpu_load_config(self) -> None:
        cfg = ResourceAlertConfig()
        assert hasattr(cfg, "cpu_load_norm5_threshold")
        assert cfg.cpu_load_norm5_threshold > 0
        assert hasattr(cfg, "cpu_load_cooldown")
        assert hasattr(cfg, "cpu_load_re_alert")

    def test_net_config(self) -> None:
        cfg = ResourceAlertConfig()
        assert hasattr(cfg, "net_mbps_threshold")
        assert cfg.net_mbps_threshold > 0
        assert hasattr(cfg, "net_cooldown")
        assert hasattr(cfg, "net_re_alert")


# ---------------------------------------------------------------------------
# Watchdog resource checks includes new probes
# ---------------------------------------------------------------------------


class TestWatchdogResourceChecksIncludesNew:
    """Verify _watchdog_resource_checks calls the new probe functions."""

    def setup_method(self) -> None:
        self.calls: list[str] = []
        self.orig_probes = {}
        for name in [
            "_probe_cpu_load_all_hosts",
            "_probe_net_all_hosts",
            "_probe_disk_all_hosts",
            "_probe_mem_all_hosts",
        ]:
            self.orig_probes[name] = getattr(bridge, name)

        bridge._probe_cpu_load_all_hosts = lambda remote_hosts: self.calls.append("cpu_load")
        bridge._probe_net_all_hosts = lambda remote_hosts: self.calls.append("net")
        bridge._probe_disk_all_hosts = lambda remote_hosts: self.calls.append("disk")
        bridge._probe_mem_all_hosts = lambda remote_hosts: self.calls.append("mem")

    def teardown_method(self) -> None:
        for name, fn in self.orig_probes.items():
            setattr(bridge, name, fn)

    def test_cpu_load_and_net_probes_called(self) -> None:
        bridge._watchdog_resource_checks(set())
        assert "cpu_load" in self.calls
        assert "net" in self.calls
