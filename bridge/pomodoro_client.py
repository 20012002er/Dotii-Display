"""番茄钟配置：计时在设备本地运行，管理中心只下发启用开关与最大时长。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

POMODORO_MIN_MINUTES = 1
POMODORO_MAX_MINUTES = 120
POMODORO_DEFAULT_MAX_MINUTES = 60


def default_pomodoro_config() -> dict[str, Any]:
    return {"max_minutes": POMODORO_DEFAULT_MAX_MINUTES}


def validate_pomodoro_config(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("pomodoro 配置必须是对象")
    minutes = payload.get("max_minutes", POMODORO_DEFAULT_MAX_MINUTES)
    if type(minutes) is not int or not POMODORO_MIN_MINUTES <= minutes <= POMODORO_MAX_MINUTES:
        raise ValueError(
            f"max_minutes 必须是 {POMODORO_MIN_MINUTES}-{POMODORO_MAX_MINUTES} 的整数"
        )
    return {"max_minutes": minutes}


class PomodoroConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()

    def read(self) -> dict[str, Any]:
        with self.lock:
            if not self.path.is_file():
                return default_pomodoro_config()
            return validate_pomodoro_config(json.loads(self.path.read_text(encoding="utf-8")))

    def write(self, config: dict[str, Any]) -> dict[str, Any]:
        validated = validate_pomodoro_config(config)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary_name = tempfile.mkstemp(prefix="pomodoro-", suffix=".json", dir=self.path.parent)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(validated, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                os.replace(temporary_name, self.path)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        return validated