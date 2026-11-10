from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))

from performance_client import (  # noqa: E402
    MAX_STALE_SECONDS,
    PerformanceConfigStore,
    PerformanceService,
)


class PerformanceConfigTests(unittest.TestCase):
    def test_defaults_validation_and_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "performance.json"
            store = PerformanceConfigStore(path)
            self.assertEqual(store.read(), PerformanceConfigStore.DEFAULTS)
            self.assertTrue(path.exists())

            saved = store.write({"cpu_enabled": False, "network_enabled": False})
            self.assertFalse(saved["cpu_enabled"])
            self.assertTrue(saved["memory_enabled"])
            self.assertEqual(PerformanceConfigStore(path).read(), saved)
            self.assertFalse(path.with_suffix(".tmp").exists())

            for key in PerformanceConfigStore.DEFAULTS:
                for invalid in (1, 0, "true", None):
                    with self.subTest(key=key, invalid=invalid):
                        with self.assertRaises(ValueError):
                            store.write({key: invalid})
            with self.assertRaises(ValueError):
                store.write([])  # type: ignore[arg-type]

    def test_read_tolerates_invalid_json_and_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "performance.json"
            store = PerformanceConfigStore(path)
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(store.read(), PerformanceConfigStore.DEFAULTS)

            path.write_text(json.dumps({
                "cpu_enabled": False,
                "memory_enabled": "yes",
                "disk_enabled": 1,
                "network_enabled": True,
            }), encoding="utf-8")
            self.assertEqual(store.read(), {
                "cpu_enabled": False,
                "memory_enabled": True,
                "disk_enabled": True,
                "network_enabled": True,
            })


class FakeClock:
    def __init__(self) -> None:
        self.monotonic_value = 100.0
        self.epoch_value = 1000.0

    def monotonic(self) -> float:
        return self.monotonic_value

    def time(self) -> float:
        return self.epoch_value

    def advance(self, seconds: float) -> None:
        self.monotonic_value += seconds
        self.epoch_value += seconds


class PerformanceServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = PerformanceConfigStore(Path(self.temporary.name) / "performance.json")
        self.clock = FakeClock()
        monotonic_patch = mock.patch("performance_client.time.monotonic", side_effect=self.clock.monotonic)
        time_patch = mock.patch("performance_client.time.time", side_effect=self.clock.time)
        self.addCleanup(monotonic_patch.stop)
        self.addCleanup(time_patch.stop)
        monotonic_patch.start()
        time_patch.start()
        psutil_patch = mock.patch("performance_client.psutil")
        self.addCleanup(psutil_patch.stop)
        self.psutil = psutil_patch.start()
        self.psutil.cpu_percent.return_value = 25.0
        self.psutil.virtual_memory.return_value = SimpleNamespace(percent=40.0)
        self.set_counters(1000, 2000, 10000, 20000)
        self.service = PerformanceService(self.config)

    def set_counters(self, disk_read: int, disk_write: int, network_rx: int, network_tx: int) -> None:
        self.psutil.disk_io_counters.return_value = SimpleNamespace(
            read_bytes=disk_read, write_bytes=disk_write,
        )
        self.psutil.net_io_counters.return_value = SimpleNamespace(
            bytes_recv=network_rx, bytes_sent=network_tx,
        )

    def test_first_sample_initializes_io_without_fake_rates(self) -> None:
        self.service._sample_once()
        snapshot = self.service.snapshot()

        self.assertTrue(snapshot["connected"])
        self.assertFalse(snapshot["stale"])
        self.assertEqual(snapshot["sampled_at_epoch"], 1000)
        self.assertEqual(snapshot["error"], "")
        self.assertEqual(snapshot["cpu"], {"available": True, "percent": 25.0})
        self.assertEqual(snapshot["memory"], {"available": True, "percent": 40.0})
        self.assertFalse(snapshot["disk"]["available"])
        self.assertFalse(snapshot["network"]["available"])
        self.assertEqual(snapshot["disk"]["read_bytes_per_sec"], 0.0)
        self.assertEqual(snapshot["network"]["rx_bytes_per_sec"], 0.0)
        self.psutil.cpu_percent.assert_called_once_with(interval=None)
        self.psutil.disk_io_counters.assert_called_once_with()
        self.psutil.net_io_counters.assert_called_once_with()

    def test_rates_use_counter_and_monotonic_deltas(self) -> None:
        self.service._sample_once()
        self.clock.advance(2)
        self.set_counters(7000, 12000, 22000, 34000)
        self.service._sample_once()
        snapshot = self.service.snapshot()

        self.assertEqual(snapshot["disk"], {
            "available": True,
            "read_bytes_per_sec": 3000.0,
            "write_bytes_per_sec": 5000.0,
        })
        self.assertEqual(snapshot["network"], {
            "available": True,
            "rx_bytes_per_sec": 6000.0,
            "tx_bytes_per_sec": 7000.0,
        })
        self.assertEqual(snapshot["sampled_at_epoch"], 1002)

    def test_counter_regression_and_none_reinitialize_baselines(self) -> None:
        self.service._sample_once()
        self.clock.advance(1)
        self.set_counters(500, 3000, 12000, 15000)
        self.service._sample_once()
        snapshot = self.service.snapshot()
        self.assertFalse(snapshot["disk"]["available"])
        self.assertFalse(snapshot["network"]["available"])

        self.clock.advance(1)
        self.set_counters(1500, 5000, 14000, 18000)
        self.service._sample_once()
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot["disk"]["read_bytes_per_sec"], 1000.0)
        self.assertEqual(snapshot["disk"]["write_bytes_per_sec"], 2000.0)
        self.assertEqual(snapshot["network"]["rx_bytes_per_sec"], 2000.0)
        self.assertEqual(snapshot["network"]["tx_bytes_per_sec"], 3000.0)

        self.clock.advance(1)
        self.psutil.disk_io_counters.return_value = None
        self.psutil.net_io_counters.return_value = None
        self.service._sample_once()
        snapshot = self.service.snapshot()
        self.assertFalse(snapshot["disk"]["available"])
        self.assertFalse(snapshot["network"]["available"])
        self.assertTrue(snapshot["connected"])

        self.clock.advance(1)
        self.set_counters(3000, 7000, 18000, 24000)
        self.service._sample_once()
        self.assertFalse(self.service.snapshot()["disk"]["available"])
        self.clock.advance(1)
        self.set_counters(5000, 10000, 22000, 30000)
        self.service._sample_once()
        self.assertTrue(self.service.snapshot()["disk"]["available"])
        self.assertTrue(self.service.snapshot()["network"]["available"])

    def test_percentage_clamping_and_invalid_values(self) -> None:
        self.psutil.cpu_percent.return_value = -5
        self.psutil.virtual_memory.return_value = SimpleNamespace(percent=150)
        self.service._sample_once()
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot["cpu"]["percent"], 0.0)
        self.assertEqual(snapshot["memory"]["percent"], 100.0)

        self.clock.advance(1)
        self.psutil.cpu_percent.return_value = float("nan")
        self.psutil.virtual_memory.return_value = SimpleNamespace(percent=float("inf"))
        self.service._sample_once()
        snapshot = self.service.snapshot()
        self.assertFalse(snapshot["cpu"]["available"])
        self.assertFalse(snapshot["memory"]["available"])
        self.assertEqual(snapshot["cpu"]["percent"], 0.0)
        self.assertEqual(snapshot["memory"]["percent"], 0.0)

    def test_exception_disconnects_and_success_clears_error(self) -> None:
        self.service._sample_once()
        first_epoch = self.service.snapshot()["sampled_at_epoch"]
        self.psutil.cpu_percent.side_effect = RuntimeError("sensor failed")
        self.clock.advance(1)
        self.service._sample_once()
        failed = self.service.snapshot()
        self.assertFalse(failed["connected"])
        self.assertIn("sensor failed", failed["error"])
        self.assertEqual(failed["sampled_at_epoch"], first_epoch)
        self.assertTrue(all(not failed[name]["available"] for name in ("cpu", "memory", "disk", "network")))

        self.psutil.cpu_percent.side_effect = None
        self.clock.advance(1)
        self.set_counters(3000, 6000, 14000, 26000)
        self.service._sample_once()
        recovered = self.service.snapshot()
        self.assertTrue(recovered["connected"])
        self.assertEqual(recovered["error"], "")
        self.assertTrue(recovered["cpu"]["available"])
        self.assertEqual(recovered["disk"]["read_bytes_per_sec"], 1000.0)
        self.assertEqual(recovered["network"]["tx_bytes_per_sec"], 3000.0)

    def test_snapshot_becomes_stale_after_five_seconds(self) -> None:
        self.assertTrue(self.service.snapshot()["stale"])
        self.assertFalse(self.service.snapshot()["connected"])
        self.service._sample_once()
        self.clock.advance(MAX_STALE_SECONDS)
        self.assertFalse(self.service.snapshot()["stale"])
        self.clock.advance(0.01)
        stale = self.service.snapshot()
        self.assertTrue(stale["stale"])
        self.assertFalse(stale["connected"])
        self.assertEqual(stale["sampled_at_epoch"], 1000)

        self.service._sample_once()
        self.assertFalse(self.service.snapshot()["stale"])
        self.assertTrue(self.service.snapshot()["connected"])

    def test_snapshot_merges_config_without_disabling_collection(self) -> None:
        self.config.write({
            "cpu_enabled": False,
            "memory_enabled": False,
            "disk_enabled": False,
            "network_enabled": False,
        })
        self.service._sample_once()
        snapshot = self.service.snapshot()
        for key in PerformanceConfigStore.DEFAULTS:
            self.assertFalse(snapshot[key])
        self.assertTrue(snapshot["connected"])
        self.assertTrue(snapshot["cpu"]["available"])
        self.assertTrue(snapshot["memory"]["available"])

    def test_background_thread_is_daemon_and_stops_reliably(self) -> None:
        sampled = threading.Event()
        with mock.patch.object(self.service, "_sample_once", side_effect=sampled.set) as sample:
            self.service.start()
            self.assertTrue(sampled.wait(1))
            self.assertTrue(self.service._thread.daemon)
            self.service.stop()
            self.assertFalse(self.service._thread.is_alive())
            sample.assert_called()


if __name__ == "__main__":
    unittest.main()
