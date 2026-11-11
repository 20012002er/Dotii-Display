from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))

from trae_client import (  # noqa: E402
    HOOK_EVENTS,
    TraeMonitor,
    hook_command,
    hooks_installed,
    permission_hook_command,
    update_hooks,
)


def event(name, session="session-1", cwd="/Users/dev/workplace/Dotii-Display", **extra):
    return {"hook_event_name": name, "session_id": session, "cwd": cwd, **extra}


class TraeEventMappingTests(unittest.TestCase):
    def test_lifecycle_events_map_to_statuses(self) -> None:
        monitor = TraeMonitor()
        cases = {
            "UserPromptSubmit": "working",
            "PreToolUse": "working",
            "PostToolUse": "working",
            "Stop": "completed",
            "SessionStart": "idle",
        }
        for name, expected in cases.items():
            self.assertEqual(monitor.record_event(event(name)), expected, msg=name)

    def test_notification_maps_by_type(self) -> None:
        monitor = TraeMonitor()
        self.assertEqual(
            monitor.record_event(event("Notification", notification_type="permission_prompt")),
            "waiting_user",
        )
        self.assertEqual(
            monitor.record_event(event("Notification", notification_type="idle_prompt")),
            "completed",
        )
        self.assertEqual(
            monitor.record_event(event("Notification", notification_type="document_review")),
            "waiting_user",
        )
        self.assertEqual(
            monitor.record_event(event("Notification", notification_type="ask_user_question")),
            "waiting_user",
        )
        self.assertEqual(
            monitor.record_event(event("Notification", notification_type="browser_interaction")),
            "waiting_user",
        )
        self.assertIsNone(
            monitor.record_event(event("Notification", notification_type="unknown_type")),
        )

    def test_unknown_events_and_missing_session_are_ignored(self) -> None:
        monitor = TraeMonitor()
        self.assertIsNone(monitor.record_event(event("UnknownEvent")))
        self.assertIsNone(monitor.record_event({"hook_event_name": "Stop"}))
        self.assertEqual(monitor.snapshot()["session_count"], 0)

    def test_no_session_end_event(self) -> None:
        """TRAE 没有 SessionEnd 事件，会话依赖 TTL 清理。"""
        monitor = TraeMonitor()
        monitor.record_event(event("SessionStart"))
        self.assertEqual(monitor.snapshot()["session_count"], 1)
        # 没有 SessionEnd 可以触发删除
        self.assertIsNone(monitor.record_event(event("SessionEnd")))

    def test_session_aggregation_and_limit(self) -> None:
        monitor = TraeMonitor()
        for i in range(20):
            monitor.record_event(event("UserPromptSubmit", session=f"s-{i}"))
        self.assertEqual(monitor.snapshot()["session_count"], 16)

    def test_set_enabled_clears_sessions(self) -> None:
        monitor = TraeMonitor()
        monitor.record_event(event("UserPromptSubmit"))
        self.assertEqual(monitor.snapshot()["session_count"], 1)
        monitor.set_enabled(False)
        self.assertEqual(monitor.snapshot()["session_count"], 0)
        self.assertEqual(monitor.snapshot()["status"], "idle")

    def test_snapshot_structure(self) -> None:
        monitor = TraeMonitor()
        monitor.record_event(event("UserPromptSubmit"))
        snap = monitor.snapshot()
        self.assertTrue(snap["connected"])
        self.assertEqual(snap["source"], "trae_hooks")
        self.assertEqual(snap["status"], "working")
        self.assertEqual(snap["project"], "Dotii-Display")
        self.assertEqual(snap["session_count"], 1)
        self.assertEqual(len(snap["sessions"]), 1)

    def test_snapshot_disabled(self) -> None:
        monitor = TraeMonitor()
        monitor.record_event(event("UserPromptSubmit"))
        snap = monitor.snapshot(module_enabled=False)
        self.assertFalse(snap["connected"])
        self.assertEqual(snap["status"], "idle")
        self.assertEqual(snap["service_state"], "disabled")


class TraeHookCommandTests(unittest.TestCase):
    def test_event_hook_command(self) -> None:
        cmd = hook_command(8787)
        self.assertIn("127.0.0.1:8787", cmd)
        self.assertIn("/api/v1/admin/trae/event", cmd)
        self.assertIn("-m 2", cmd)

    def test_permission_hook_command(self) -> None:
        cmd = permission_hook_command(8787)
        self.assertIn("127.0.0.1:8787", cmd)
        self.assertIn("/api/v1/admin/trae/permission", cmd)
        self.assertIn("-m 35", cmd)


class TraeHooksInstallTests(unittest.TestCase):
    def test_install_and_remove_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.json"
            result = update_hooks(path, 8787, "install")
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["changed"]), len(HOOK_EVENTS) + 1)  # +1 for PreToolUse

            settings = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(settings["version"], 1)
            self.assertIn("hooks", settings)
            for event_name in HOOK_EVENTS:
                self.assertIn(event_name, settings["hooks"])
            self.assertIn("PreToolUse", settings["hooks"])

            # 备份文件应存在
            backup = path.with_name(path.name + ".dotii-backup")
            self.assertFalse(backup.exists())  # 首次创建时原文件不存在，无备份

            # 幂等安装
            result2 = update_hooks(path, 8787, "install")
            self.assertEqual(result2["changed"], [])

            # 检查已安装
            self.assertTrue(hooks_installed(path))

            # 移除
            result3 = update_hooks(path, 8787, "remove")
            self.assertTrue(result3["ok"])
            self.assertEqual(len(result3["changed"]), len(HOOK_EVENTS) + 1)
            self.assertFalse(hooks_installed(path))

    def test_install_preserves_existing_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.json"
            existing = {
                "version": 1,
                "hooks": {
                    "SessionStart": [
                        {"matcher": "", "hooks": [{"type": "command", "command": "echo hello"}]}
                    ]
                },
            }
            path.write_text(json.dumps(existing), encoding="utf-8")

            result = update_hooks(path, 8787, "install")
            self.assertTrue(result["ok"])

            settings = json.loads(path.read_text(encoding="utf-8"))
            # 用户原有 hook 保留
            user_hooks = [h for h in settings["hooks"]["SessionStart"] if "echo hello" in str(h)]
            self.assertEqual(len(user_hooks), 1)
            # 我们的 hook 也添加了
            self.assertEqual(len(settings["hooks"]["SessionStart"]), 2)

            # 备份应包含原始内容
            backup = path.with_name(path.name + ".dotii-backup")
            self.assertTrue(backup.exists())
            backup_data = json.loads(backup.read_text(encoding="utf-8"))
            self.assertEqual(len(backup_data["hooks"]["SessionStart"]), 1)

    def test_remove_only_our_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.json"
            existing = {
                "version": 1,
                "hooks": {
                    "SessionStart": [
                        {"matcher": "", "hooks": [{"type": "command", "command": "echo hello"}]}
                    ]
                },
            }
            path.write_text(json.dumps(existing), encoding="utf-8")
            update_hooks(path, 8787, "install")
            update_hooks(path, 8787, "remove")

            settings = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(settings["hooks"]["SessionStart"]), 1)
            self.assertIn("echo hello", str(settings["hooks"]["SessionStart"][0]))

    def test_pretooluse_has_longer_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.json"
            update_hooks(path, 8787, "install")
            settings = json.loads(path.read_text(encoding="utf-8"))
            pretool = settings["hooks"]["PreToolUse"][0]["hooks"][0]
            self.assertEqual(pretool["timeout"], 35)
            # 普通事件用默认超时
            session = settings["hooks"]["SessionStart"][0]["hooks"][0]
            self.assertEqual(session["timeout"], 30)


if __name__ == "__main__":
    unittest.main()
