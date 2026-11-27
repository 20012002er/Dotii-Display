"""番茄钟配置存储与管理中心接线测试。"""
import json
import sys
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest import mock

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))

from pomodoro_client import (  # noqa: E402
    PomodoroConfigStore,
    default_pomodoro_config,
    validate_pomodoro_config,
)
from codex_bridge import (  # noqa: E402
    BridgeHandler,
    BridgeServer,
    MODULE_DEFAULTS,
    default_dotii_config,
    validate_module_config,
)


class PomodoroConfigTests(unittest.TestCase):
    def test_default_max_minutes_is_60(self) -> None:
        self.assertEqual(default_pomodoro_config(), {"max_minutes": 60})

    def test_read_missing_file_returns_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = PomodoroConfigStore(Path(folder) / "pomodoro.json")
            self.assertEqual(store.read(), {"max_minutes": 60})

    def test_write_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pomodoro.json"
            store = PomodoroConfigStore(path)
            saved = store.write({"max_minutes": 90})
            self.assertEqual(saved, {"max_minutes": 90})
            self.assertEqual(store.read(), {"max_minutes": 90})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"max_minutes": 90})

    def test_write_accepts_boundary_values(self) -> None:
        self.assertEqual(validate_pomodoro_config({"max_minutes": 1}), {"max_minutes": 1})
        self.assertEqual(validate_pomodoro_config({"max_minutes": 120}), {"max_minutes": 120})

    def test_write_defaults_missing_field(self) -> None:
        self.assertEqual(validate_pomodoro_config({}), {"max_minutes": 60})

    def test_validate_rejects_non_object(self) -> None:
        for payload in (None, [], "25", 25):
            with self.assertRaises(ValueError):
                validate_pomodoro_config(payload)

    def test_validate_rejects_non_integer_minutes(self) -> None:
        for value in (True, 25.0, "25", None):
            with self.assertRaises(ValueError):
                validate_pomodoro_config({"max_minutes": value})

    def test_validate_rejects_out_of_range_minutes(self) -> None:
        for value in (0, -5, 121, 10000):
            with self.assertRaises(ValueError):
                validate_pomodoro_config({"max_minutes": value})

    def test_read_invalid_saved_value_raises(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "pomodoro.json"
            path.write_text('{"max_minutes": 0}', encoding="utf-8")
            store = PomodoroConfigStore(path)
            with self.assertRaises(ValueError):
                store.read()


class PomodoroBridgeWiringTests(unittest.TestCase):
    def test_module_defaults_include_pomodoro_disabled(self) -> None:
        self.assertIs(MODULE_DEFAULTS["pomodoro"], False)

    def test_validate_module_config_migrates_pomodoro(self) -> None:
        modules = validate_module_config({"codex": True})
        self.assertIs(modules["pomodoro"], False)

    def _snapshot_bridge(self, modules: dict[str, bool]) -> BridgeServer:
        bridge = object.__new__(BridgeServer)
        bridge.store = mock.Mock()
        bridge.store.read.return_value = {}
        bridge.custom = mock.Mock()
        bridge.custom.read.return_value = {"enabled": False}
        bridge.assets = mock.Mock()
        bridge.assets.source_info.return_value = {}
        bridge.assets.render_info.return_value = {}
        for name in ("bambu", "zai", "notion", "weather", "performance", "turntable",
                     "claudecode", "claudecode_permission", "trae", "trae_permission",
                     "qoder", "qoder_usage"):
            service = mock.Mock()
            service.snapshot.return_value = {}
            setattr(bridge, name, service)
        bridge.pomodoro_config = mock.Mock()
        bridge.pomodoro_config.read.return_value = {"max_minutes": 45}
        bridge.modules = mock.Mock()
        bridge.modules.read.return_value = modules
        bridge.dotii = mock.Mock()
        bridge.dotii.read.return_value = default_dotii_config()
        bridge.display = mock.Mock()
        bridge.display.read.return_value = {}
        return bridge

    def test_snapshot_embeds_pomodoro_config(self) -> None:
        bridge = self._snapshot_bridge(dict(MODULE_DEFAULTS))
        with mock.patch("codex_bridge.dotii_state", return_value={}):
            snapshot = BridgeServer.snapshot(bridge)
        self.assertEqual(snapshot["pomodoro"], {"enabled": False, "max_minutes": 45})

    def test_snapshot_pomodoro_enabled_follows_module_switch(self) -> None:
        bridge = self._snapshot_bridge({**MODULE_DEFAULTS, "pomodoro": True})
        with mock.patch("codex_bridge.dotii_state", return_value={}):
            snapshot = BridgeServer.snapshot(bridge)
        self.assertEqual(snapshot["pomodoro"], {"enabled": True, "max_minutes": 45})

    def test_pomodoro_config_route_validates_and_saves(self) -> None:
        handler = object.__new__(BridgeHandler)
        handler.path = "/api/v1/admin/pomodoro/config"
        handler.server = mock.Mock()
        handler._deny_nonlocal = mock.Mock(return_value=False)
        payload = {"max_minutes": 90}
        handler._read_admin_action = mock.Mock(return_value=payload)
        handler._send_json = mock.Mock()
        handler.server.pomodoro_config.write.return_value = payload

        BridgeHandler.do_POST(handler)

        handler.server.pomodoro_config.write.assert_called_once_with(payload)
        status, body = handler._send_json.call_args.args
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(body, {"ok": True, "pomodoro_config": payload})

    def test_pomodoro_config_route_rejects_invalid_payload(self) -> None:
        handler = object.__new__(BridgeHandler)
        handler.path = "/api/v1/admin/pomodoro/config"
        handler.server = mock.Mock()
        handler._deny_nonlocal = mock.Mock(return_value=False)
        handler._read_admin_action = mock.Mock(return_value={"max_minutes": 0})
        handler._send_json = mock.Mock()
        handler.server.pomodoro_config.write.side_effect = ValueError("max_minutes 必须是 1-120 的整数")

        BridgeHandler.do_POST(handler)

        status, body = handler._send_json.call_args.args
        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
        self.assertIn("max_minutes", body["error"])

    def test_pomodoro_module_toggle_saves_without_service(self) -> None:
        handler = object.__new__(BridgeHandler)
        handler.path = "/api/v1/admin/modules"
        handler.server = mock.Mock()
        handler.server.modules.read.return_value = dict(MODULE_DEFAULTS)
        handler.server.modules.write.return_value = {**MODULE_DEFAULTS, "pomodoro": True}
        handler.server.custom.read.return_value = {"enabled": False}
        handler._deny_nonlocal = mock.Mock(return_value=False)
        handler._read_admin_action = mock.Mock(return_value={"id": "pomodoro", "enabled": True})
        handler._send_json = mock.Mock()

        BridgeHandler.do_POST(handler)

        handler.server.modules.write.assert_called_once_with({**MODULE_DEFAULTS, "pomodoro": True})
        status, body = handler._send_json.call_args.args
        self.assertEqual(status, HTTPStatus.OK)
        self.assertIs(body["modules"]["pomodoro"], True)


if __name__ == "__main__":
    unittest.main()