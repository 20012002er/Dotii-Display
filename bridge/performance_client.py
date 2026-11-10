"""本机性能配置与 psutil 指标采集。"""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path
from typing import Any

import psutil

POLL_INTERVAL_SECONDS = 1.0
MAX_STALE_SECONDS = 5.0


def _empty_metrics() -> dict[str, dict[str, Any]]:
    return {
        "cpu": {"available": False, "percent": 0.0},
        "memory": {"available": False, "percent": 0.0},
        "disk": {"available": False, "read_bytes_per_sec": 0.0, "write_bytes_per_sec": 0.0},
        "network": {"available": False, "rx_bytes_per_sec": 0.0, "tx_bytes_per_sec": 0.0},
    }


def _percent(value: Any) -> dict[str, Any]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return {"available": False, "percent": 0.0}
    if not math.isfinite(number):
        return {"available": False, "percent": 0.0}
    return {"available": True, "percent": max(0.0, min(100.0, number))}


def _counter_pair(counters: Any, first: str, second: str) -> tuple[float, float] | None:
    if counters is None:
        return None
    try:
        first_value = float(getattr(counters, first))
        second_value = float(getattr(counters, second))
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    if (not math.isfinite(first_value) or not math.isfinite(second_value)
            or first_value < 0 or second_value < 0):
        return None
    return first_value, second_value


def _io_rates(
    previous: tuple[float, float, float] | None,
    current: tuple[float, float] | None,
    now: float,
) -> tuple[bool, float, float, tuple[float, float, float] | None]:
    if current is None:
        return False, 0.0, 0.0, None
    first, second = current
    baseline = (first, second, now)
    if previous is None:
        return False, 0.0, 0.0, baseline
    previous_first, previous_second, previous_now = previous
    elapsed = now - previous_now
    if (first < previous_first or second < previous_second
            or not math.isfinite(elapsed) or elapsed <= 0):
        return False, 0.0, 0.0, baseline
    first_rate = (first - previous_first) / elapsed
    second_rate = (second - previous_second) / elapsed
    if not math.isfinite(first_rate) or not math.isfinite(second_rate):
        return False, 0.0, 0.0, baseline
    return True, first_rate, second_rate, baseline


class PerformanceConfigStore:
    DEFAULTS = {
        "cpu_enabled": True,
        "memory_enabled": True,
        "disk_enabled": True,
        "network_enabled": True,
    }

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        if not path.exists():
            self.write(dict(self.DEFAULTS))

    def read(self) -> dict[str, bool]:
        with self._lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
        if not isinstance(data, dict):
            data = {}
        return {
            key: data.get(key) if type(data.get(key)) is bool else default
            for key, default in self.DEFAULTS.items()
        }

    def validate(self, candidate: dict[str, Any]) -> dict[str, bool]:
        if not isinstance(candidate, dict):
            raise ValueError("性能配置必须是对象")
        current = self.read()
        validated: dict[str, bool] = {}
        for key in self.DEFAULTS:
            value = candidate.get(key, current[key])
            if type(value) is not bool:
                raise ValueError(f"{key} 必须是布尔值")
            validated[key] = value
        return validated

    def write(self, candidate: dict[str, Any]) -> dict[str, bool]:
        validated = self.validate(candidate)
        payload = json.dumps(validated, ensure_ascii=False, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with self._lock:
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(self.path)
        return validated


class PerformanceService:
    def __init__(self, config: PerformanceConfigStore) -> None:
        self.config_store = config
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._metrics = _empty_metrics()
        self._previous_disk: tuple[float, float, float] | None = None
        self._previous_network: tuple[float, float, float] | None = None
        self._last_success_monotonic: float | None = None
        self._sampled_at_epoch = 0.0
        self._error = ""
        self._thread = threading.Thread(
            target=self._run, name="performance-poller", daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)

    def _sample_once(self) -> None:
        now = time.monotonic()
        try:
            cpu = _percent(psutil.cpu_percent(interval=None))
            memory_info = psutil.virtual_memory()
            memory = _percent(getattr(memory_info, "percent", None))
            disk_counters = _counter_pair(psutil.disk_io_counters(), "read_bytes", "write_bytes")
            network_counters = _counter_pair(psutil.net_io_counters(), "bytes_recv", "bytes_sent")
            sampled_at_epoch = time.time()
        except Exception as error:
            with self._lock:
                self._metrics = _empty_metrics()
                self._error = str(error)[:200] or error.__class__.__name__
            return

        disk_available, disk_read, disk_write, previous_disk = _io_rates(
            self._previous_disk, disk_counters, now,
        )
        network_available, network_rx, network_tx, previous_network = _io_rates(
            self._previous_network, network_counters, now,
        )
        metrics = _empty_metrics()
        metrics["cpu"] = cpu
        metrics["memory"] = memory
        metrics["disk"] = {
            "available": disk_available,
            "read_bytes_per_sec": disk_read,
            "write_bytes_per_sec": disk_write,
        }
        metrics["network"] = {
            "available": network_available,
            "rx_bytes_per_sec": network_rx,
            "tx_bytes_per_sec": network_tx,
        }
        with self._lock:
            self._previous_disk = previous_disk
            self._previous_network = previous_network
            self._metrics = metrics
            self._last_success_monotonic = now
            self._sampled_at_epoch = sampled_at_epoch
            self._error = ""

    def _run(self) -> None:
        while not self._stop.is_set():
            self._sample_once()
            self._stop.wait(POLL_INTERVAL_SECONDS)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            metrics = {name: dict(values) for name, values in self._metrics.items()}
            last_success = self._last_success_monotonic
            sampled_at_epoch = self._sampled_at_epoch
            error = self._error
        stale = last_success is None or time.monotonic() - last_success > MAX_STALE_SECONDS
        connected = last_success is not None and not stale and not error
        return {
            **self.config_store.read(),
            "connected": connected,
            "stale": stale,
            "sampled_at_epoch": int(sampled_at_epoch) if last_success is not None else 0,
            "error": error,
            **metrics,
        }
