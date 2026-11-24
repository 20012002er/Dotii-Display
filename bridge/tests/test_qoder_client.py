from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))

from qoder_client import (  # noqa: E402
    COSY_CUSTOM_ALPHABET,
    COSY_SECRET_B64,
    HOOK_EVENTS,
    QoderMonitor,
    QoderUsageConfigStore,
    QoderUsageError,
    QoderUsageService,
    cosy_encode,
    cosy_signature,
    hook_command,
    hooks_installed,
    parse_usage,
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


def plan_payload(used=250, total=2000, percentage=12.5):
    return {
        "qoderUsage": {
            "userType": "personal_pro",
            "expiresAt": 1893456000000,
            "isQuotaExceeded": False,
            "userQuota": {"total": total, "used": used, "remaining": total - used, "percentage": percentage},
            "addOnQuota": {"total": 700, "used": 0, "remaining": 700, "percentage": 0},
        }
    }


class QoderUsageParseTests(unittest.TestCase):
    def test_plan_quota_is_primary_bucket(self) -> None:
        usage = parse_usage(plan_payload())
        self.assertTrue(usage["available"])
        self.assertEqual(usage["bucket"], "plan")
        self.assertEqual(usage["used"], 250)
        self.assertEqual(usage["total"], 2000)
        self.assertEqual(usage["percent"], 12)
        self.assertEqual(usage["plan"], "personal_pro")
        self.assertFalse(usage["exceeded"])
        self.assertEqual(usage["error"], "")

    def test_percent_clamped_when_used_exceeds_total(self) -> None:
        usage = parse_usage(plan_payload(used=2500, total=2000, percentage=125))
        self.assertEqual(usage["percent"], 100)

    def test_org_resource_package_fallback(self) -> None:
        usage = parse_usage({"qoderUsage": {
            "userType": "enterprise",
            "orgResourcePackage": {"used": 100, "cap": 500, "remaining": 400, "percentage": 20, "available": True},
        }})
        self.assertTrue(usage["available"])
        self.assertEqual(usage["bucket"], "org")
        self.assertEqual(usage["used"], 100)
        self.assertEqual(usage["total"], 500)
        self.assertEqual(usage["percent"], 20)

    def test_unwrapped_root_payload(self) -> None:
        usage = parse_usage({"userQuota": {"total": 100, "used": 40, "remaining": 60, "percentage": 40}})
        self.assertTrue(usage["available"])
        self.assertEqual(usage["percent"], 40)

    def test_empty_payload_reports_unavailable(self) -> None:
        usage = parse_usage({})
        self.assertFalse(usage["available"])
        self.assertEqual(usage["used"], 0)
        self.assertEqual(usage["total"], 0)
        self.assertTrue(usage["error"])

    def test_zero_total_is_unavailable(self) -> None:
        usage = parse_usage(plan_payload(used=0, total=0, percentage=0))
        self.assertFalse(usage["available"])


class QoderUsageConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "qoder-usage.json"
        self.addCleanup(self._tmp.cleanup)

    def test_defaults_and_public_masking(self) -> None:
        store = QoderUsageConfigStore(self.path)
        config = store.read()
        self.assertEqual(config["personal_token"], "")
        self.assertIs(config["display_enabled"], False)
        public = store.public()
        self.assertFalse(public["configured"])
        self.assertFalse(public["has_personal_token"])
        self.assertNotIn("personal_token", public)

    def test_write_persists_token_and_display_switch(self) -> None:
        store = QoderUsageConfigStore(self.path)
        public = store.write({"personal_token": "pt-secret", "display_enabled": True})
        self.assertTrue(public["configured"])
        self.assertTrue(public["display_enabled"])
        self.assertEqual(store.read()["personal_token"], "pt-secret")
        self.assertNotIn("pt-secret", json.dumps(public))

    def test_preserve_secret_keeps_existing_token(self) -> None:
        store = QoderUsageConfigStore(self.path)
        store.write({"personal_token": "pt-secret", "display_enabled": False})
        store.write({"personal_token": "", "display_enabled": True}, preserve_secret=True)
        self.assertEqual(store.read()["personal_token"], "pt-secret")
        self.assertTrue(store.read()["display_enabled"])

    def test_token_with_whitespace_rejected(self) -> None:
        store = QoderUsageConfigStore(self.path)
        with self.assertRaises(ValueError):
            store.write({"personal_token": "pt bad token"})


class QoderCosyProtocolTests(unittest.TestCase):
    def test_cosy_encode_rearrangement_and_alphabet(self) -> None:
        # b"hello" -> 标准 base64 "aGVsbG8="（n=8, a=2）-> 重排 "8=VsbGaG"
        std = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        mapping = {std[i]: COSY_CUSTOM_ALPHABET[i] for i in range(64)}
        mapping["="] = "$"
        expected = "".join(mapping[ch] for ch in "8=VsbGaG")
        self.assertEqual(cosy_encode(b"hello"), expected)
        self.assertTrue(set(cosy_encode(b"hello")) <= set(COSY_CUSTOM_ALPHABET + "$"))

    def test_cosy_encode_roundtrip(self) -> None:
        std = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        reverse = {COSY_CUSTOM_ALPHABET[i]: std[i] for i in range(64)}
        reverse["$"] = "="
        for sample in (b"", b"a", b"hello world", json.dumps({"personalToken": "pt-x"}).encode()):
            encoded = cosy_encode(sample)
            plain_b64 = "".join(reverse[ch] for ch in encoded)
            n = len(plain_b64)
            a = n // 3
            restored = plain_b64[n - a:] + plain_b64[a:n - a] + plain_b64[:a]
            import base64
            self.assertEqual(base64.b64decode(restored), sample)

    def test_signature_is_md5_of_appcode_secret_date(self) -> None:
        import hashlib
        date = "Tue, 24 Nov 2026 10:00:00 GMT"
        expected = hashlib.md5(f"cosy&{COSY_SECRET_B64}&{date}".encode()).hexdigest()
        self.assertEqual(cosy_signature(date), expected)


class QoderUsageServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "qoder-usage.json"
        self.addCleanup(self._tmp.cleanup)
        self.config = QoderUsageConfigStore(self.path)
        self.config.write({"personal_token": "pt-secret", "display_enabled": True})
        self.service = QoderUsageService(self.config, lambda: True)

    def test_fetch_exchanges_token_then_reads_usage(self) -> None:
        calls = []

        def fake_http(method, url, headers, body=None):
            calls.append((method, url, headers))
            if "jobToken" in url:
                return {"securityOauthToken": "jt-abc", "expireTime": 4102444800000, "name": "dev", "userType": "personal_pro"}
            return plan_payload()

        self.service._http_json = fake_http
        usage = self.service._fetch()
        self.assertTrue(usage["available"])
        self.assertEqual(usage["used"], 250)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "POST")
        self.assertIn("gateway.qoder.com.cn", calls[0][1])
        self.assertEqual(calls[1][0], "GET")
        self.assertIn("/sash/api/v2/me/usage", calls[1][1])
        self.assertEqual(calls[1][2]["Authorization"], "Bearer jt-abc")

    def test_token_cached_until_expiry(self) -> None:
        exchanges = []

        def fake_http(method, url, headers, body=None):
            if "jobToken" in url:
                exchanges.append(url)
                return {"securityOauthToken": "jt-abc", "expireTime": 4102444800000}
            return plan_payload()

        self.service._http_json = fake_http
        self.service._fetch()
        self.service._fetch()
        self.assertEqual(len(exchanges), 1)

    def test_missing_token_raises(self) -> None:
        self.config.write({"personal_token": "", "display_enabled": True})
        with self.assertRaises(QoderUsageError):
            self.service._fetch()

    def test_probe_reports_auth_failure(self) -> None:
        def fail_exchange(pat):
            raise QoderUsageError("PAT 认证失败：personal token is invalid", "auth")

        self.service._exchange_token = fail_exchange
        result = self.service.probe()
        self.assertFalse(result["ok"])
        self.assertFalse(result["auth"]["ok"])
        self.assertIn("认证失败", result["detail"])

    def test_probe_success_reports_usage(self) -> None:
        self.service._exchange_token = lambda pat: ("jt-abc", 4102444800.0)
        self.service._request_usage = lambda token: parse_usage(plan_payload())
        result = self.service.probe()
        self.assertTrue(result["ok"])
        self.assertTrue(result["network"]["ok"])
        self.assertTrue(result["auth"]["ok"])
        self.assertTrue(result["parse"]["ok"])
        self.assertEqual(result["used"], 250)
        self.assertEqual(result["total"], 2000)

    def test_probe_without_token_asks_to_configure(self) -> None:
        self.config.write({"personal_token": "", "display_enabled": True})
        result = self.service.probe()
        self.assertFalse(result["ok"])
        self.assertIn("PAT", result["detail"])

    def test_snapshot_online_with_data(self) -> None:
        import time as _time
        self.service._usage = parse_usage(plan_payload())
        self.service._last_success = _time.time()
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot["service_state"], "online")
        self.assertTrue(snapshot["connected"])
        self.assertTrue(snapshot["display_enabled"])
        self.assertEqual(snapshot["used"], 250)
        self.assertEqual(snapshot["total"], 2000)
        self.assertEqual(snapshot["percent"], 12)
        self.assertEqual(snapshot["source"], "qoder_usage_api")

    def test_snapshot_disabled_when_module_off(self) -> None:
        service = QoderUsageService(self.config, lambda: False)
        snapshot = service.snapshot()
        self.assertEqual(snapshot["service_state"], "disabled")
        self.assertFalse(snapshot["connected"])

    def test_display_switch_off_stops_polling(self) -> None:
        self.config.write({"personal_token": "pt-secret", "display_enabled": False})
        self.assertFalse(self.service._enabled())
        self.assertEqual(self.service.snapshot()["service_state"], "disabled")

    def test_snapshot_needs_configuration(self) -> None:
        self.config.write({"personal_token": "", "display_enabled": True})
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot["service_state"], "needs_configuration")
        self.assertFalse(snapshot["configured"])


if __name__ == "__main__":
    unittest.main()