"""macOS Spotify 唱片台状态、封面配色与播放控制。"""
from __future__ import annotations

import colorsys
import hashlib
import io
import math
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from PIL import Image

PLAYING_POLL_INTERVAL_SECONDS = 1.0
IDLE_POLL_INTERVAL_SECONDS = 5.0
MAX_STALE_SECONDS = 8.0
COMMAND_TIMEOUT_SECONDS = 2.0
COVER_DOWNLOAD_TIMEOUT_SECONDS = 5.0
MAX_COVER_BYTES = 2 * 1024 * 1024
FIELD_SEPARATOR = "\x1f"
DEFAULT_PALETTE = (0x10141C, 0x2F66D0, 0x2E3B55)
CONTROL_SCRIPTS = {
    "playpause": 'tell application "Spotify" to playpause',
    "previous": 'tell application "Spotify" to previous track',
    "next": 'tell application "Spotify" to next track',
}

APPLE_SCRIPT = """
tell application "Spotify"
    set separator to ASCII character 31
    set stateText to player state as text
    set positionText to player position as text
    set trackName to ""
    set trackArtist to ""
    set trackAlbum to ""
    set artworkUrl to ""
    set spotifyUrl to ""
    set durationText to ""
    try
        set currentTrack to current track
        if currentTrack is not missing value then
            set trackName to name of currentTrack
            set trackArtist to artist of currentTrack
            set trackAlbum to album of currentTrack
            set artworkUrl to artwork url of currentTrack
            set spotifyUrl to spotify url of currentTrack
            set durationText to duration of currentTrack as text
        end if
    end try
    return stateText & separator & positionText & separator & trackName & separator & trackArtist & separator & trackAlbum & separator & artworkUrl & separator & spotifyUrl & separator & durationText
end tell
"""


def _error_text(error: Exception) -> str:
    return str(error).strip()[:200] or error.__class__.__name__


class TurntableCommandError(ValueError):
    """可向设备直接返回的唱片台控制错误。"""


def _rgb_int(color: tuple[float, float, float]) -> int:
    channels = [max(0, min(255, round(channel))) for channel in color]
    return (channels[0] << 16) | (channels[1] << 8) | channels[2]


def _rgb_channels(color: int) -> tuple[int, int, int]:
    return (color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF


def _luminance(color: int) -> float:
    linear = []
    for channel in _rgb_channels(color):
        value = channel / 255.0
        linear.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _cap_luminance(color: int, maximum: float) -> int:
    if _luminance(color) <= maximum:
        return color
    channels = _rgb_channels(color)
    low, high = 0.0, 1.0
    result = 0
    for _ in range(16):
        factor = (low + high) / 2.0
        candidate = _rgb_int(tuple(channel * factor for channel in channels))
        if _luminance(candidate) <= maximum:
            low = factor
            result = candidate
        else:
            high = factor
    return result


def _hls_color(color: int, lightness: float, saturation: float, hue_shift: float = 0.0) -> int:
    hue, _, _ = colorsys.rgb_to_hls(*(channel / 255.0 for channel in _rgb_channels(color)))
    rgb = colorsys.hls_to_rgb((hue + hue_shift) % 1.0, lightness, saturation)
    return _rgb_int(tuple(channel * 255.0 for channel in rgb))


def _extract_palette(data: bytes) -> tuple[int, int, int]:
    with Image.open(io.BytesIO(data)) as probe:
        probe.verify()
    with Image.open(io.BytesIO(data)) as opened:
        image = opened.convert("RGB")
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        image.thumbnail((48, 48), resampling)
        quantization = getattr(Image, "Quantize", Image).MEDIANCUT
        quantized = image.quantize(colors=12, method=quantization)
        raw_palette = quantized.getpalette() or []
        entries = quantized.getcolors(maxcolors=48 * 48 + 1) or []

    colors = []
    for count, index in entries:
        offset = index * 3
        if offset + 3 <= len(raw_palette):
            color = _rgb_int(tuple(raw_palette[offset:offset + 3]))
            colors.append((count, color))
    if not colors:
        raise ValueError("封面没有可用颜色")

    background_source = min(colors, key=lambda item: _luminance(item[1]))[1]
    background = _hls_color(background_source, 0.08, 0.35)
    background = _cap_luminance(background, 0.055)

    def accent_score(item: tuple[int, int]) -> float:
        count, color = item
        _, _, saturation = colorsys.rgb_to_hls(*(channel / 255.0 for channel in _rgb_channels(color)))
        return count * (0.25 + saturation)

    accent_source = max(colors, key=accent_score)[1]
    _, source_lightness, source_saturation = colorsys.rgb_to_hls(
        *(channel / 255.0 for channel in _rgb_channels(accent_source))
    )
    accent = _hls_color(
        accent_source,
        min(0.42, max(0.30, source_lightness)),
        max(0.55, source_saturation),
    )
    accent = _cap_luminance(accent, 0.16)
    secondary = _hls_color(accent, 0.24, 0.42, hue_shift=0.08)
    secondary = _cap_luminance(secondary, 0.10)
    return background, accent, secondary


class TurntableService:
    def __init__(
        self,
        cache_dir: str | Path | None = None,
        *,
        command_runner: Callable[..., Any] | None = None,
        url_opener: Callable[..., Any] | None = None,
        monotonic: Callable[[], float] | None = None,
        epoch_clock: Callable[[], float] | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir is not None else (
            Path.home() / "Library" / "Caches" / "Dotii" / "turntable"
        )
        self._command_runner = command_runner if command_runner is not None else subprocess.run
        self._url_opener = url_opener if url_opener is not None else urllib.request.urlopen
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self._epoch_clock = epoch_clock if epoch_clock is not None else time.time
        self._state_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._spotify_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._enabled = False
        self._spotify_running = False
        self._playback_state = "not_running"
        self._track_revision = 0
        self._title = ""
        self._artist = ""
        self._album = ""
        self._progress_ms = 0
        self._duration_ms = 0
        self._palette_revision = 0
        self._palette = DEFAULT_PALETTE
        self._palette_source = "fallback"
        self._sampled_at_epoch = 0
        self._last_success_monotonic: float | None = None
        self._error = ""
        self._palette_cache: dict[int, tuple[tuple[int, int, int], str]] = {}
        self._started = False
        self._stopped = False
        self._thread = threading.Thread(target=self._run, name="turntable-poller", daemon=True)

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started or self._stopped:
                return
            self._started = True
            self._thread.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stopped = True
            started = self._started
        self._stop_event.set()
        self._wake_event.set()
        if started and threading.current_thread() is not self._thread:
            self._thread.join(timeout=3)

    def set_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        with self._state_lock:
            self._enabled = enabled
            if not enabled:
                self._reset_locked()
        self._wake_event.set()

    def command(self, action: Any, position_ms: Any = None) -> dict[str, Any]:
        if not isinstance(action, str) or action not in {*CONTROL_SCRIPTS, "seek"}:
            raise TurntableCommandError("不支持的唱片台操作")
        if action == "seek":
            if type(position_ms) is not int:
                raise TurntableCommandError("seek 需要整数 position_ms")
            if position_ms < 0:
                raise TurntableCommandError("position_ms 不能小于 0")

        with self._spotify_lock:
            with self._state_lock:
                enabled = self._enabled
            if not enabled:
                raise TurntableCommandError("请先启用唱片台模块")
            try:
                if not self._spotify_is_running():
                    raise TurntableCommandError("Spotify 未运行，请先打开 Spotify")
                if action == "seek":
                    duration_ms = self._parse_player(self._read_player())["duration_ms"]
                    if duration_ms > 0 and position_ms > duration_ms:
                        raise TurntableCommandError(f"position_ms 不能超过当前曲目时长 {duration_ms}")
                    seconds = f"{position_ms // 1000}.{position_ms % 1000:03d}"
                    script = f'tell application "Spotify" to set player position to {seconds}'
                else:
                    script = CONTROL_SCRIPTS[action]
                self._control_player(script)
                self._sample_once()
                return self.snapshot()
            except TurntableCommandError:
                raise
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                raise TurntableCommandError(f"Spotify 控制失败：{_error_text(error)}") from error
            finally:
                self._wake_event.set()

    def _reset_locked(self) -> None:
        self._spotify_running = False
        self._playback_state = "not_running"
        self._track_revision = 0
        self._title = ""
        self._artist = ""
        self._album = ""
        self._progress_ms = 0
        self._duration_ms = 0
        self._palette_revision = 0
        self._palette = DEFAULT_PALETTE
        self._palette_source = "fallback"
        self._sampled_at_epoch = 0
        self._last_success_monotonic = None
        self._error = ""

    def _is_collecting(self) -> bool:
        with self._state_lock:
            return self._enabled and not self._stop_event.is_set()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.clear()
            if not self._is_collecting():
                self._wake_event.wait()
                continue
            self._sample_once()
            with self._state_lock:
                playback_state = self._playback_state
            delay = PLAYING_POLL_INTERVAL_SECONDS if playback_state == "playing" else IDLE_POLL_INTERVAL_SECONDS
            self._wake_event.wait(delay)

    def _command(self, arguments: list[str]) -> Any:
        return self._command_runner(
            arguments,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )

    def _spotify_is_running(self) -> bool:
        result = self._command(["pgrep", "-x", "Spotify"])
        if result.returncode == 1:
            return False
        if result.returncode != 0:
            detail = str(result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"pgrep 失败 ({result.returncode}): {detail}")
        return True

    def _read_player(self) -> str:
        result = self._command(["osascript", "-e", APPLE_SCRIPT])
        if result.returncode != 0:
            detail = str(result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"AppleScript 失败 ({result.returncode}): {detail}")
        return str(result.stdout or "")

    def _control_player(self, script: str) -> None:
        result = self._command(["osascript", "-e", script])
        if result.returncode != 0:
            detail = str(result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"AppleScript 失败 ({result.returncode}): {detail}")

    @staticmethod
    def _number(value: str) -> float:
        normalized = value.strip()
        if not normalized or normalized == "missing value":
            return 0.0
        if "," in normalized and "." not in normalized:
            normalized = normalized.replace(",", ".")
        number = float(normalized)
        if not math.isfinite(number) or number < 0:
            raise ValueError("Spotify 数值无效")
        return number

    @classmethod
    def _parse_player(cls, output: str) -> dict[str, Any]:
        fields = output.rstrip("\r\n").split(FIELD_SEPARATOR)
        if len(fields) != 8:
            raise ValueError("AppleScript 返回格式无效")
        state, position, title, artist, album, artwork_url, spotify_url, duration = fields
        if not any((title, artist, album, artwork_url, spotify_url)):
            return {
                "playback_state": "stopped", "title": "", "artist": "", "album": "",
                "progress_ms": 0, "duration_ms": 0, "track_revision": 0,
                "artwork_url": "",
            }
        duration_ms = round(cls._number(duration))
        progress_ms = round(cls._number(position) * 1000)
        if duration_ms:
            progress_ms = min(progress_ms, duration_ms)
        normalized_state = state.strip().lower()
        playback_state = normalized_state if normalized_state in ("playing", "paused") else "stopped"
        identity = spotify_url.strip() or FIELD_SEPARATOR.join(
            (title, artist, album, str(duration_ms))
        )
        revision = int.from_bytes(hashlib.sha256(identity.encode("utf-8")).digest()[:4], "big") or 1
        return {
            "playback_state": playback_state,
            "title": title,
            "artist": artist,
            "album": album,
            "progress_ms": progress_ms,
            "duration_ms": duration_ms,
            "track_revision": revision,
            "artwork_url": artwork_url.strip(),
        }

    def _sample_once(self) -> None:
        with self._spotify_lock:
            self._sample_once_locked()

    def _sample_once_locked(self) -> None:
        if not self._is_collecting():
            return
        running = False
        try:
            running = self._spotify_is_running()
            if not self._is_collecting():
                return
            sampled_at = self._monotonic()
            if not running:
                with self._state_lock:
                    if not self._enabled or self._stop_event.is_set():
                        return
                    self._reset_locked()
                    self._sampled_at_epoch = int(self._epoch_clock())
                    self._last_success_monotonic = sampled_at
                return

            player = self._parse_player(self._read_player())
            if not self._is_collecting():
                return
            revision = player.pop("track_revision")
            artwork_url = player.pop("artwork_url")
            with self._state_lock:
                palette_revision = self._palette_revision
                palette = self._palette
                palette_source = self._palette_source
            if revision != palette_revision:
                if revision == 0:
                    palette, palette_source = DEFAULT_PALETTE, "fallback"
                else:
                    palette, palette_source = self._palette_for_track(revision, artwork_url)
                if not self._is_collecting():
                    return
            sampled_at = self._monotonic()
            with self._state_lock:
                if not self._enabled or self._stop_event.is_set():
                    return
                self._spotify_running = True
                self._playback_state = player["playback_state"]
                self._track_revision = revision
                self._title = player["title"]
                self._artist = player["artist"]
                self._album = player["album"]
                self._progress_ms = player["progress_ms"]
                self._duration_ms = player["duration_ms"]
                self._palette_revision = revision
                self._palette = palette
                self._palette_source = palette_source
                self._sampled_at_epoch = int(self._epoch_clock())
                self._last_success_monotonic = sampled_at
                self._error = ""
        except Exception as error:
            with self._state_lock:
                if not self._enabled or self._stop_event.is_set():
                    return
                self._spotify_running = running
                self._playback_state = "error"
                self._error = _error_text(error)

    def _cache_path(self, revision: int) -> Path:
        key = hashlib.sha256(f"track:{revision}".encode("ascii")).hexdigest()
        return self.cache_dir / f"{key}.cover"

    def _read_cached_cover(self, path: Path) -> bytes | None:
        try:
            if not 0 < path.stat().st_size <= MAX_COVER_BYTES:
                return None
            return path.read_bytes()
        except OSError:
            return None

    def _download_cover(self, artwork_url: str) -> bytes:
        parsed = urllib.parse.urlsplit(artwork_url)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            raise ValueError("封面 URL 仅支持 HTTP/HTTPS")
        request = urllib.request.Request(artwork_url, headers={"User-Agent": "Dotii-Turntable/1.0"})
        with self._url_opener(request, timeout=COVER_DOWNLOAD_TIMEOUT_SECONDS) as response:
            geturl = getattr(response, "geturl", None)
            if geturl is not None:
                final_scheme = urllib.parse.urlsplit(geturl()).scheme.lower()
                if final_scheme not in ("http", "https"):
                    raise ValueError("封面重定向协议无效")
            data = response.read(MAX_COVER_BYTES + 1)
        if not data or len(data) > MAX_COVER_BYTES:
            raise ValueError("封面为空或超过 2MB")
        return data

    def _save_cover(self, path: Path, data: bytes) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(data)
            temporary.replace(path)
        except OSError:
            pass

    def _palette_for_track(self, revision: int, artwork_url: str) -> tuple[tuple[int, int, int], str]:
        cached = self._palette_cache.get(revision)
        if cached is not None:
            return cached
        path = self._cache_path(revision)
        data = self._read_cached_cover(path)
        downloaded = False
        if data is None and self._is_collecting():
            try:
                data = self._download_cover(artwork_url)
                downloaded = True
            except Exception:
                data = None
        result: tuple[tuple[int, int, int], str] = (DEFAULT_PALETTE, "fallback")
        if data is not None:
            try:
                result = (_extract_palette(data), "cover")
                if downloaded:
                    self._save_cover(path, data)
            except Exception:
                result = (DEFAULT_PALETTE, "fallback")
        if self._is_collecting():
            self._palette_cache[revision] = result
        return result

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            enabled = self._enabled
            spotify_running = self._spotify_running
            playback_state = self._playback_state
            last_success = self._last_success_monotonic
            error = self._error
            values = {
                "enabled": enabled,
                "connected": False,
                "spotify_running": spotify_running,
                "playback_state": playback_state,
                "track_revision": self._track_revision,
                "title": self._title,
                "artist": self._artist,
                "album": self._album,
                "progress_ms": self._progress_ms,
                "duration_ms": self._duration_ms,
                "palette": list(self._palette),
                "palette_source": self._palette_source,
                "sampled_at_epoch": self._sampled_at_epoch,
                "stale": False,
                "error": error,
            }
        stale = last_success is None or self._monotonic() - last_success > MAX_STALE_SECONDS
        values["stale"] = stale
        values["connected"] = bool(
            enabled and spotify_running and playback_state != "error" and not stale and not error
        )
        return values
