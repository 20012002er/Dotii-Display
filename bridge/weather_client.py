"""高德天气采集：为窗外小世界页面提供实时天气.

通过高德开放平台实况天气接口读取指定城市的天气：
GET https://restapi.amap.com/v3/weather/weatherInfo?key=<API Key>&city=<城市>&extensions=base
API Key 只保存在本机运行目录，不进入设备快照或日志。
"""
from __future__ import annotations

import http.client
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

POLL_INTERVAL_SECONDS = 600.0
MAX_BACKOFF_SECONDS = 1800.0
REQUEST_TIMEOUT_SECONDS = 10.0
IDLE_WAIT_SECONDS = 5.0
MAX_STALE_SECONDS = 3600.0
SLEEP_RESUME_JUMP_SECONDS = 30.0
WEATHER_HOST = "restapi.amap.com"
WEATHER_PATH = "/v3/weather/weatherInfo"
MAX_RESPONSE_BYTES = 65536

# 设备端本地绘制所需的天气类别；高德文字描述归一到这七类。
CONDITION_CLEAR = "clear"
CONDITION_CLOUDY = "cloudy"
CONDITION_OVERCAST = "overcast"
CONDITION_RAIN = "rain"
CONDITION_THUNDER = "thunder"
CONDITION_SNOW = "snow"
CONDITION_FOG = "fog"


def _text(value: Any, maximum: int, field: str) -> str:
    text = str(value or "").strip()
    if len(text.encode("utf-8")) > maximum:
        raise ValueError(f"{field} 太长")
    return text


def condition_from_text(weather: str) -> str:
    """高德天气文字 → 设备绘制类别（顺序敏感：先匹配复合天气）。"""
    text = str(weather or "")
    if "雷" in text:
        return CONDITION_THUNDER
    if "雪" in text or "雨夹雪" in text:
        return CONDITION_SNOW
    if "雨" in text:
        return CONDITION_RAIN
    if any(word in text for word in ("雾", "霾", "浮尘", "扬沙", "沙尘")):
        return CONDITION_FOG
    if "阴" in text:
        return CONDITION_OVERCAST
    if "云" in text:
        return CONDITION_CLOUDY
    return CONDITION_CLEAR


def weather_snapshot(payload: Any) -> dict[str, Any]:
    """把高德实况响应规范化为快照字段（纯函数，缺失数据安全降级）。"""
    result: dict[str, Any] = {
        "city": "",
        "weather_text": "",
        "condition": CONDITION_CLEAR,
        "temperature": 0,
        "temperature_available": False,
        "humidity": 0,
        "wind_direction": "",
        "wind_power": "",
        "report_time": "",
        "error": "",
    }
    if not isinstance(payload, dict):
        result["error"] = "响应格式无效"
        return result
    if str(payload.get("status")) != "1":
        message = str(payload.get("info") or "接口返回失败").strip()
        result["error"] = message[:63] or "接口返回失败"
        return result
    lives = payload.get("lives")
    if not isinstance(lives, list) or not lives or not isinstance(lives[0], dict):
        result["error"] = "响应缺少实况天气，请检查城市名称"
        return result
    live = lives[0]
    result["city"] = str(live.get("city") or "").strip()[:31]
    result["weather_text"] = str(live.get("weather") or "").strip()[:31]
    result["condition"] = condition_from_text(result["weather_text"])
    temperature = str(live.get("temperature") or "").strip()
    try:
        result["temperature"] = int(round(float(temperature)))
        result["temperature_available"] = True
    except (TypeError, ValueError):
        pass
    humidity = str(live.get("humidity") or "").strip()
    try:
        result["humidity"] = max(0, min(100, int(round(float(humidity)))))
    except (TypeError, ValueError):
        pass
    result["wind_direction"] = str(live.get("winddirection") or "").strip()[:15]
    result["wind_power"] = str(live.get("windpower") or "").strip()[:15]
    result["report_time"] = str(live.get("reporttime") or "").strip()[:31]
    return result


class WeatherConfigStore:
    DEFAULTS = {"api_key": "", "city": ""}

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        if not path.exists():
            self.write(dict(self.DEFAULTS))

    def read(self) -> dict[str, Any]:
        with self._lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
        if not isinstance(data, dict):
            data = {}
        return {**self.DEFAULTS, **data}

    def public(self) -> dict[str, Any]:
        config = self.read()
        return {
            "city": config["city"],
            "has_api_key": bool(config["api_key"]),
            "configured": bool(config["api_key"]) and bool(config["city"]),
        }

    def validate(self, candidate: dict[str, Any], preserve_secret: bool = False) -> dict[str, Any]:
        current = self.read()
        secret = candidate.get("api_key", "" if not preserve_secret else current["api_key"])
        if preserve_secret and secret == "":
            secret = current["api_key"]
        secret = _text(secret, 200, "API Key")
        if secret and any(character.isspace() for character in secret):
            raise ValueError("API Key 不能包含空白字符")
        city = _text(candidate.get("city", current["city"]), 60, "城市")
        return {"api_key": secret, "city": city}

    def write(self, candidate: dict[str, Any], preserve_secret: bool = False) -> dict[str, Any]:
        validated = self.validate(candidate, preserve_secret=preserve_secret)
        payload = json.dumps(validated, ensure_ascii=False, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with self._lock:
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(self.path)
        return self.public()


class WeatherError(Exception):
    """单次天气请求失败；message 不包含 API Key。"""


class WeatherService:
    def __init__(self, config: WeatherConfigStore, module_enabled: Callable[[], bool]) -> None:
        self.config_store = config
        self.module_enabled = module_enabled
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._weather: dict[str, Any] = {}
        self._last_success = 0.0
        self._last_error = ""
        self._thread = threading.Thread(target=self._run, name="weather-poller", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=3)

    def reconfigure(self) -> None:
        with self._lock:
            self._weather = {}
            self._last_success = 0.0
            self._last_error = ""
        self._wake.set()

    def set_enabled(self, enabled: bool) -> None:
        self.reconfigure()

    def _enabled(self) -> bool:
        try:
            return bool(self.module_enabled())
        except Exception:
            return False

    def _request(self, api_key: str, city: str) -> dict[str, Any]:
        """单次实况天气 GET；失败抛 WeatherError。"""
        query = urlencode({"key": api_key, "city": city, "extensions": "base"})
        connection = http.client.HTTPSConnection(WEATHER_HOST, timeout=REQUEST_TIMEOUT_SECONDS)
        try:
            connection.request("GET", f"{WEATHER_PATH}?{query}", headers={
                "Accept": "application/json",
            })
            response = connection.getresponse()
            body = response.read(MAX_RESPONSE_BYTES + 1)
            status = response.status
        finally:
            connection.close()
        if status in (401, 403):
            raise WeatherError("API Key 认证失败，请检查高德 API Key")
        if status != 200:
            raise WeatherError(f"天气接口返回 HTTP {status}")
        if len(body) > MAX_RESPONSE_BYTES:
            raise WeatherError("天气响应过大")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WeatherError("天气响应不是有效 JSON") from error
        return weather_snapshot(payload)

    def _fetch(self) -> dict[str, Any]:
        config = self.config_store.read()
        api_key = str(config.get("api_key") or "")
        city = str(config.get("city") or "")
        if not api_key:
            raise WeatherError("尚未配置高德 API Key")
        if not city:
            raise WeatherError("尚未配置查询城市")
        try:
            return self._request(api_key, city)
        except WeatherError:
            raise
        except (OSError, http.client.HTTPException) as error:
            raise WeatherError(f"无法连接 {WEATHER_HOST}") from error

    def _wait_backoff(self, backoff: float) -> bool:
        """退避分片等待；reconfigure 的 _wake 与系统唤醒都能立即打断。"""
        mono_start = time.monotonic()
        wall_start = time.time()
        deadline = mono_start + backoff
        while not self._stop.is_set() and not self._wake.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._stop.wait(min(5.0, remaining))
            if time.time() - wall_start - (time.monotonic() - mono_start) > SLEEP_RESUME_JUMP_SECONDS:
                return True
        self._wake.clear()
        return False

    def _run(self) -> None:
        backoff = POLL_INTERVAL_SECONDS
        logged_business_error = ""
        while not self._stop.is_set():
            config = self.config_store.public()
            if not self._enabled() or not config["configured"]:
                with self._lock:
                    self._weather = {}
                    self._last_success = 0.0
                    self._last_error = ""
                backoff = POLL_INTERVAL_SECONDS
                logged_business_error = ""
                self._wake.wait(IDLE_WAIT_SECONDS)
                self._wake.clear()
                continue
            try:
                weather = self._fetch()
                with self._lock:
                    self._weather = weather
                    self._last_error = weather.get("error", "")
                    if not self._last_error:
                        self._last_success = time.time()
                backoff = POLL_INTERVAL_SECONDS
                business_error = weather.get("error", "")
                if business_error and business_error != logged_business_error:
                    logging.warning("高德天气读取异常：%s", business_error)
                logged_business_error = business_error
            except WeatherError as error:
                with self._lock:
                    self._last_error = str(error)[:63]
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                logging.warning("高德天气请求失败：%s（%.0f 秒后重试）", error, backoff)
            if self._wait_backoff(backoff):
                backoff = POLL_INTERVAL_SECONDS

    def snapshot(self) -> dict[str, Any]:
        config = self.config_store.public()
        enabled = self._enabled()
        with self._lock:
            weather = dict(self._weather)
            last_success = self._last_success
            error = self._last_error
        stale = not last_success or time.time() - last_success > MAX_STALE_SECONDS
        has_data = bool(weather.get("weather_text"))
        connected = enabled and config["configured"] and has_data and not stale
        service_state = (
            "disabled" if not enabled
            else "needs_configuration" if not config["configured"]
            else "online" if connected
            else "error" if error
            else "connecting"
        )
        return {
            "module_enabled": enabled,
            "configured": config["configured"],
            "service_state": service_state,
            "connected": connected,
            "source": "amap_api",
            "city": weather.get("city", "") or config["city"],
            "weather_text": weather.get("weather_text", ""),
            "condition": weather.get("condition", CONDITION_CLEAR),
            "temperature": weather.get("temperature", 0),
            "temperature_available": bool(weather.get("temperature_available", False)),
            "humidity": weather.get("humidity", 0),
            "wind_direction": weather.get("wind_direction", ""),
            "wind_power": weather.get("wind_power", ""),
            "report_time": weather.get("report_time", ""),
            "updated_at_epoch": int(last_success) if last_success else 0,
            "error": error,
        }
