from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))

from turntable_client import (  # noqa: E402
    DEFAULT_PALETTE,
    FIELD_SEPARATOR,
    IDLE_POLL_INTERVAL_SECONDS,
    PLAYING_POLL_INTERVAL_SECONDS,
    TurntableService,
)


class FakeClock:
    def __init__(self) -> None:
        self.monotonic_value = 100.0
        self.epoch_value = 1000.0

    def monotonic(self) -> float:
        return self.monotonic_value

    def time(self) -> float:
        return self.epoch_value


def player_output(
    state: str = "playing",
    position: str = "12.5",
    title: str = "Song",
    artist: str = "Artist",
    album: str = "Album",
    artwork_url: str = "",
    spotify_url: str = "spotify:track:first",
    duration: str = "180000",
) -> str:
    return FIELD_SEPARATOR.join(
        (state, position, title, artist, album, artwork_url, spotify_url, duration)
    ) + "\n"


def command_result(returncode: int = 0, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def relative_luminance(color: int) -> float:
    values = []
    for channel in ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF):
        value = channel / 255.0
        values.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
    return 0.2126 * values[0] + 0.7152 * values[1] + 0.0722 * values[2]


class TurntableServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.clock = FakeClock()
        self.runner = mock.Mock()
        self.opener = mock.Mock()
        self.apple_output = player_output()
        self.spotify_running = True

        def run_command(arguments, **_kwargs):
            if arguments[0] == "pgrep":
                return command_result(0 if self.spotify_running else 1, stdout="123\n" if self.spotify_running else "")
            return command_result(0 if not isinstance(self.apple_output, Exception) else 1,
                                  stdout="" if isinstance(self.apple_output, Exception) else self.apple_output,
                                  stderr=str(self.apple_output) if isinstance(self.apple_output, Exception) else "")

        self.runner.side_effect = run_command
        self.service = TurntableService(
            Path(self.temporary.name) / "covers",
            command_runner=self.runner,
            url_opener=self.opener,
            monotonic=self.clock.monotonic,
            epoch_clock=self.clock.time,
        )

    def enable(self) -> None:
        self.service.set_enabled(True)

    def test_disabled_does_not_call_commands_or_download_cover(self) -> None:
        self.apple_output = player_output(artwork_url="https://example.com/cover.jpg")
        self.service._sample_once()
        self.service.set_enabled(False)
        self.service._sample_once()

        snapshot = self.service.snapshot()
        self.assertFalse(snapshot["enabled"])
        self.assertEqual(snapshot["playback_state"], "not_running")
        self.runner.assert_not_called()
        self.opener.assert_not_called()

    def test_not_running_never_invokes_applescript(self) -> None:
        self.enable()
        self.spotify_running = False
        self.service._sample_once()

        snapshot = self.service.snapshot()
        self.assertEqual(snapshot["playback_state"], "not_running")
        self.assertFalse(snapshot["spotify_running"])
        self.assertFalse(snapshot["connected"])
        self.assertEqual(snapshot["track_revision"], 0)
        self.runner.assert_called_once()
        self.assertEqual(self.runner.call_args.args[0], ["pgrep", "-x", "Spotify"])

    def test_playing_parse_and_json_snapshot(self) -> None:
        self.enable()
        self.service._sample_once()

        snapshot = self.service.snapshot()
        self.assertTrue(snapshot["enabled"])
        self.assertTrue(snapshot["connected"])
        self.assertTrue(snapshot["spotify_running"])
        self.assertEqual(snapshot["playback_state"], "playing")
        self.assertEqual(snapshot["title"], "Song")
        self.assertEqual(snapshot["progress_ms"], 12500)
        self.assertEqual(snapshot["duration_ms"], 180000)
        self.assertGreater(snapshot["track_revision"], 0)
        self.assertLessEqual(snapshot["track_revision"], 0xFFFFFFFF)
        self.assertEqual(snapshot["palette_source"], "fallback")
        self.assertEqual(snapshot["sampled_at_epoch"], 1000)
        json.dumps(snapshot, ensure_ascii=False)
        self.assertEqual(self.runner.call_count, 2)
        self.assertEqual(self.runner.call_args.args[0][0], "osascript")

    def test_unicode_and_separator_like_characters_survive(self) -> None:
        self.enable()
        self.apple_output = player_output(
            title="夜の🎵 | 歌", artist="周杰伦 / 越路吹雪", album="仰望✨，Deluxe"
        )
        self.service._sample_once()

        snapshot = self.service.snapshot()
        self.assertEqual(snapshot["title"], "夜の🎵 | 歌")
        self.assertEqual(snapshot["artist"], "周杰伦 / 越路吹雪")
        self.assertEqual(snapshot["album"], "仰望✨，Deluxe")
        self.assertIn("夜の🎵", json.dumps(snapshot, ensure_ascii=False))

    def test_paused_empty_track_and_polling_intervals(self) -> None:
        self.enable()
        self.apple_output = player_output(state="paused", position="0")
        self.service._sample_once()
        self.assertEqual(self.service.snapshot()["playback_state"], "paused")
        self.assertEqual(IDLE_POLL_INTERVAL_SECONDS, 5.0)
        self.assertEqual(PLAYING_POLL_INTERVAL_SECONDS, 1.0)

        self.apple_output = player_output(
            state="stopped", position="missing value", title="", artist="", album="",
            artwork_url="", spotify_url="", duration="",
        )
        self.service._sample_once()
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot["playback_state"], "stopped")
        self.assertEqual(snapshot["track_revision"], 0)
        self.assertEqual(snapshot["progress_ms"], 0)
        self.assertTrue(snapshot["connected"])

    def test_applescript_failure_sets_error_and_recovers(self) -> None:
        self.enable()
        self.apple_output = RuntimeError("not authorized")
        self.service._sample_once()

        failed = self.service.snapshot()
        self.assertEqual(failed["playback_state"], "error")
        self.assertFalse(failed["connected"])
        self.assertTrue(failed["spotify_running"])
        self.assertIn("not authorized", failed["error"])

        self.apple_output = player_output()
        self.service._sample_once()
        recovered = self.service.snapshot()
        self.assertTrue(recovered["connected"])
        self.assertEqual(recovered["error"], "")

    def test_track_revision_depends_on_track_not_progress_or_metadata_refresh(self) -> None:
        self.enable()
        self.service._sample_once()
        first = self.service.snapshot()["track_revision"]

        self.apple_output = player_output(position="40", title="Song (Remastered)")
        self.service._sample_once()
        self.assertEqual(self.service.snapshot()["track_revision"], first)

        self.apple_output = player_output(spotify_url="spotify:track:second")
        self.service._sample_once()
        second = self.service.snapshot()["track_revision"]
        self.assertNotEqual(second, first)

    def test_cover_palette_is_valid_dark_readable_and_cached(self) -> None:
        self.enable()
        image = Image.new("RGB", (96, 96), (18, 24, 40))
        for x in range(48):
            for y in range(96):
                image.putpixel((x, y), (38, 98, 210))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        cover = buffer.getvalue()
        self.opener.side_effect = lambda *_args, **_kwargs: io.BytesIO(cover)
        self.apple_output = player_output(artwork_url="https://example.com/cover.png")

        self.service._sample_once()
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot["palette_source"], "cover")
        self.assertEqual(len(snapshot["palette"]), 3)
        for color in snapshot["palette"]:
            self.assertIs(type(color), int)
            self.assertGreaterEqual(color, 0)
            self.assertLessEqual(color, 0xFFFFFF)
        self.assertLess(relative_luminance(snapshot["palette"][0]), 0.06)
        accent_contrast = 1.05 / (relative_luminance(snapshot["palette"][1]) + 0.05)
        self.assertGreaterEqual(accent_contrast, 4.5)
        self.opener.assert_called_once()
        self.assertEqual(self.opener.call_args.kwargs["timeout"], 5.0)
        self.assertEqual(len(list(self.service.cache_dir.glob("*.cover"))), 1)

        self.service._sample_once()
        self.opener.assert_called_once()

    def test_cover_failure_uses_fallback_without_disconnecting(self) -> None:
        self.enable()
        self.apple_output = player_output(artwork_url="https://example.com/broken.png")
        self.opener.side_effect = TimeoutError("network unavailable")
        self.service._sample_once()

        snapshot = self.service.snapshot()
        self.assertEqual(snapshot["palette"], list(DEFAULT_PALETTE))
        self.assertEqual(snapshot["palette_source"], "fallback")
        self.assertTrue(snapshot["connected"])
        self.assertEqual(snapshot["error"], "")
        self.service._sample_once()
        self.opener.assert_called_once()

    def test_cached_cover_is_used_without_network(self) -> None:
        self.enable()
        image = Image.new("RGB", (48, 48), (30, 80, 180))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        self.opener.side_effect = lambda *_args, **_kwargs: io.BytesIO(buffer.getvalue())
        self.apple_output = player_output(artwork_url="https://example.com/cover.png")
        self.service._sample_once()
        self.assertEqual(self.service.snapshot()["palette_source"], "cover")

        restarted = TurntableService(
            self.service.cache_dir,
            command_runner=self.runner,
            url_opener=self.opener,
            monotonic=self.clock.monotonic,
            epoch_clock=self.clock.time,
        )
        restarted.set_enabled(True)
        restarted._sample_once()
        self.assertEqual(restarted.snapshot()["palette_source"], "cover")
        self.opener.assert_called_once()

    def test_playback_commands_execute_applescript_and_refresh_snapshot(self) -> None:
        self.enable()
        expected = {
            "playpause": "playpause",
            "previous": "previous track",
            "next": "next track",
        }
        for action, statement in expected.items():
            with self.subTest(action=action):
                self.runner.reset_mock()
                snapshot = self.service.command(action, 12345)
                scripts = [
                    call.args[0][2] for call in self.runner.call_args_list
                    if call.args[0][0] == "osascript"
                ]
                self.assertIn(f'tell application "Spotify" to {statement}', scripts)
                self.assertEqual(snapshot["title"], "Song")
                self.assertEqual(snapshot["sampled_at_epoch"], 1000)

    def test_seek_accepts_zero_and_current_track_duration(self) -> None:
        self.enable()
        for position_ms, seconds in ((0, "0.000"), (180000, "180.000")):
            with self.subTest(position_ms=position_ms):
                self.runner.reset_mock()
                snapshot = self.service.command("seek", position_ms)
                scripts = [
                    call.args[0][2] for call in self.runner.call_args_list
                    if call.args[0][0] == "osascript"
                ]
                self.assertIn(
                    f'tell application "Spotify" to set player position to {seconds}', scripts
                )
                self.assertTrue(snapshot["connected"])

    def test_seek_allows_any_nonnegative_position_when_duration_is_unavailable(self) -> None:
        self.enable()
        self.apple_output = player_output(duration="")

        self.service.command("seek", 123456789)

        scripts = [call.args[0] for call in self.runner.call_args_list]
        self.assertIn(
            ["osascript", "-e", 'tell application "Spotify" to set player position to 123456.789'],
            scripts,
        )

    def test_command_requires_enabled_module(self) -> None:
        with self.assertRaisesRegex(ValueError, "请先启用唱片台模块"):
            self.service.command("playpause")
        self.runner.assert_not_called()

    def test_command_requires_running_spotify_without_applescript(self) -> None:
        self.enable()
        self.spotify_running = False

        with self.assertRaisesRegex(ValueError, "Spotify 未运行"):
            self.service.command("next")

        self.runner.assert_called_once_with(
            ["pgrep", "-x", "Spotify"], capture_output=True, text=True,
            timeout=2.0, check=False,
        )

    def test_seek_rejects_invalid_type_negative_and_past_duration(self) -> None:
        self.enable()
        for position_ms in (None, True, 1.0, "1000", -1):
            with self.subTest(position_ms=position_ms):
                self.runner.reset_mock()
                with self.assertRaises(ValueError):
                    self.service.command("seek", position_ms)
                self.runner.assert_not_called()

        with self.assertRaisesRegex(ValueError, "不能超过当前曲目时长"):
            self.service.command("seek", 180001)
        scripts = [call.args[0] for call in self.runner.call_args_list]
        self.assertFalse(any(
            len(arguments) > 2 and "set player position" in arguments[2]
            for arguments in scripts
        ))

    def test_command_rejects_unknown_action(self) -> None:
        self.enable()
        with self.assertRaisesRegex(ValueError, "不支持的唱片台操作"):
            self.service.command("volume_up")
        self.runner.assert_not_called()

    def test_control_applescript_failure_is_clear_and_recoverable(self) -> None:
        self.enable()
        self.apple_output = RuntimeError("not authorized")

        with self.assertRaisesRegex(ValueError, "Spotify 控制失败") as raised:
            self.service.command("playpause")
        self.assertIn("not authorized", str(raised.exception))

        self.apple_output = player_output(state="paused")
        snapshot = self.service.command("next")
        self.assertTrue(snapshot["connected"])
        self.assertEqual(snapshot["playback_state"], "paused")

    def test_repeated_start_stop_and_set_enabled_are_safe(self) -> None:
        self.service.set_enabled(False)
        self.service.start()
        thread = self.service._thread
        self.service.start()
        self.assertIs(self.service._thread, thread)
        self.assertTrue(thread.daemon)
        self.assertTrue(thread.is_alive())

        self.service.stop()
        self.service.stop()
        self.assertFalse(thread.is_alive())
        self.service.start()
        self.assertFalse(thread.is_alive())
        self.service.set_enabled(True)
        self.assertTrue(self.service.snapshot()["enabled"])
        self.runner.assert_not_called()
        self.opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
