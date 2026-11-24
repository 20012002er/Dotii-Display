from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))

from qoder_client import (  # noqa: E402
    HOOK_EVENTS,
    QoderMonitor,
    hook_command,
    hooks_installed,
    update_hooks,
)


def event(name, session="session-1", cwd="/Users/dev/workplace/Dotii-Display", **extra):
    return {"hook_event_name": name, "session_id": session, "cwd": cwd, **extra}


class QoderEventMappingTests(unittest.TestCase):
    def test_lifecycle_events_map_to_statuses(self) -> None:
        monitor = QoderMonitor()
        cases = {
            "UserPromptSubmit": "working",
            "PreToolUse": "working",
            "PostToolUse": "working",
            # 单个工具失败是 Agent 正常工作的一部分，不代表任务失败
            "PostToolUseFailure": "working",
            "Stop": "completed",
        }
        for name, expected in cases.items():
            self.assertEqual(monitor.record_event(event(name)), expected, msg=name)

    def test_unsupported_events_are_ignored(self) -> None:
        """Qoder IDE 只支持 5 种事件，SessionStart/Notification 等不应映射。"""
        monitor = QoderMonitor()
        for name in ("SessionStart", "SessionEnd", "Notification", "UnknownEvent"):
            self.assertIsNone(monitor.record_event(event(name)), msg=name)
        self.assertEqual(monitor.snapshot()["session_count"], 0)

    def test_missing_session_is_ignored(self) -> None:
        monitor = QoderMonitor()
        self.assertIsNone(monitor.record_event({"hook_event_name": "Stop"}))
        self.assertEqual(monitor.snapshot()["session_count"], 0)

    def test_session_aggregation_and_limit(self) -> None:
        monitor = QoderMonitor()
        for i in range(20):
            monitor.record_event(event("UserPromptSubmit", session=f"s-{i}"))
        self.assertEqual(monitor.snapshot()["session_count"], 16)

    def test_set_enabled_clears_sessions(self) -> None:
        monitor = QoderMonitor()
        monitor.record_event(event("UserPromptSubmit"))
        self.assertEqual(monitor.snapshot()["session_count"], 1)
        monitor.set_enabled(False)
        self.assertEqual(monitor.snapshot()["session_count"], 0)
        self.assertEqual(monitor.snapshot()["status"], "idle")

    def test_snapshot_structure(self) -> None:
        monitor = QoderMonitor()
        monitor.record_event(event("UserPromptSubmit"))
        snap = monitor.snapshot()
        self.assertTrue(snap["connected"])
        self.assertEqual(snap["source"], "qoder_hooks")
        self.assertEqual(snap["status"], "working")
        self.assertEqual(snap["project"], "Dotii-Display")
        self.assertEqual(snap["session_count"], 1)
        self.assertEqual(len(snap["sessions"]), 1)

    def test_snapshot_disabled(self) -> None:
        monitor = QoderMonitor()
        monitor.record_event(event("UserPromptSubmit"))
        snap = monitor.snapshot(module_enabled=False)
        self.assertFalse(snap["connected"])
        self.assertEqual(snap["status"], "idle")
        self.assertEqual(snap["service_state"], "disabled")


class QoderHookCommandTests(unittest.TestCase):
    def test_event_hook_command(self) -> None:
        cmd = hook_command(8787)
        self.assertIn("127.0.0.1:8787", cmd)
        self.assertIn("/api/v1/admin/qoder/event", cmd)
        self.assertIn("-m 2", cmd)


class QoderHooksInstallTests(unittest.TestCase):
    def test_install_and_remove_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            result = update_hooks(path, 8787, "install")
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["changed"]), len(HOOK_EVENTS))

            settings = json.loads(path.read_text(encoding="utf-8"))
            # settings.json 是扁平结构，不应引入 TRAE 式的 version 字段
            self.assertNotIn("version", settings)
            for event_name in HOOK_EVENTS:
                self.assertIn(event_name, settings["hooks"])

            # 首次创建时原文件不存在，无备份
            backup = path.with_name(path.name + ".dotii-backup")
            self.assertFalse(backup.exists())

            # 幂等安装
            result2 = update_hooks(path, 8787, "install")
            self.assertEqual(result2["changed"], [])

            self.assertTrue(hooks_installed(path))

            result3 = update_hooks(path, 8787, "remove")
            self.assertTrue(result3["ok"])
            self.assertEqual(len(result3["changed"]), len(HOOK_EVENTS))
            self.assertFalse(hooks_installed(path))

    def test_install_preserves_other_settings(self) -> None:
        """settings.json 里还有 enabledPlugins 等无关配置，必须原样保留。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            existing = {
                "enabledPlugins": {"superpowers@qoder-marketplace": True},
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hello"}]}
                    ]
                },
            }
            path.write_text(json.dumps(existing), encoding="utf-8")

            result = update_hooks(path, 8787, "install")
            self.assertTrue(result["ok"])

            settings = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(settings["enabledPlugins"], existing["enabledPlugins"])
            # 用户原有 hook 保留，我们的 hook 追加
            user_hooks = [h for h in settings["hooks"]["PreToolUse"] if "echo hello" in str(h)]
            self.assertEqual(len(user_hooks), 1)
            self.assertEqual(len(settings["hooks"]["PreToolUse"]), 2)

            # 备份应包含原始内容
            backup = path.with_name(path.name + ".dotii-backup")
            self.assertTrue(backup.exists())
            backup_data = json.loads(backup.read_text(encoding="utf-8"))
            self.assertEqual(len(backup_data["hooks"]["PreToolUse"]), 1)

    def test_remove_only_our_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            existing = {
                "hooks": {
                    "Stop": [
                        {"matcher": "", "hooks": [{"type": "command", "command": "echo hello"}]}
                    ]
                },
            }
            path.write_text(json.dumps(existing), encoding="utf-8")
            update_hooks(path, 8787, "install")
            update_hooks(path, 8787, "remove")

            settings = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(settings["hooks"]["Stop"]), 1)
            self.assertIn("echo hello", str(settings["hooks"]["Stop"][0]))

    def test_all_hooks_use_default_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            update_hooks(path, 8787, "install")
            settings = json.loads(path.read_text(encoding="utf-8"))
            for event_name in HOOK_EVENTS:
                hook = settings["hooks"][event_name][0]["hooks"][0]
                self.assertEqual(hook["timeout"], 30, msg=event_name)

    def test_invalid_format_raises_and_keeps_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")
            with self.assertRaises(ValueError):
                update_hooks(path, 8787, "install")
            self.assertEqual(path.read_text(encoding="utf-8"), "[1, 2, 3]")
            self.assertFalse(hooks_installed(path))


if __name__ == "__main__":
    unittest.main()