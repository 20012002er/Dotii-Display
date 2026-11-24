"""Qoder 工作状态与用量采集.

状态：通过 Qoder IDE 官方 hooks 事件推送聚合各会话的任务状态。只消费
``session_id`` / ``cwd`` / ``hook_event_name`` 这几个状态字段，
不解析、不存储消息内容。Qoder IDE 仅支持 UserPromptSubmit /
PreToolUse / PostToolUse / PostToolUseFailure / Stop 五种事件，
无 Notification，故不做权限转发与等待用户状态。

用量：用个人访问令牌（PAT）走 cosy 协议交换 jobToken，再读
``/sash/api/v2/me/usage`` 额度接口。PAT 只保存在本机运行目录，
不进入设备快照或日志。
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import logging
import threading
import time
import urllib.parse
from email.utils import formatdate
from pathlib import Path
from typing import Any, Callable

MAX_SESSIONS = 16
SESSION_TTL_SECONDS = 24 * 3600.0
SNAPSHOT_SESSIONS = 4
PROJECT_MAX_BYTES = 47
EVENT_URL_PATH = "/api/v1/admin/qoder/event"

# hook 事件 → 会话状态（与 codex.task.status 同一状态集合，供固件复用）。
# PostToolUseFailure 是单个工具失败，Agent 仍在正常工作，不算任务失败。
EVENT_STATUS = {
    "UserPromptSubmit": "working",
    "PreToolUse": "working",
    "PostToolUse": "working",
    "PostToolUseFailure": "working",
    "Stop": "completed",
}

STATUS_TEXT = {
    "working": "工作中",
    "completed": "已完成",
    "idle": "暂无任务",
    "offline": "离线",
}

# Qoder IDE 支持的全部事件（无 SessionStart/SessionEnd/Notification）
HOOK_EVENTS = (
    "UserPromptSubmit", "PreToolUse",
    "PostToolUse", "PostToolUseFailure", "Stop",
)


def hook_command(port: int) -> str:
    """裸 curl 命令：macOS/Linux 的 shell 与 Windows 的 cmd 都能直接执行，
    stdin 由 Qoder 直接送入 curl，不依赖 sh 外壳或单引号语法。
    事件上报是尽力而为——管理中心离线（如重启间隙）时静默退出 0，
    不在 Qoder 界面上产生 hook 错误提示。"""
    return (
        f"curl -s -m 2 -X POST http://127.0.0.1:{port}{EVENT_URL_PATH} "
        f"-H \"Content-Type: application/json\" --data-binary @- || exit 0"
    )


def _project_name(cwd: Any) -> str:
    text = str(cwd or "").rstrip("/\\")
    name = text.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] if text else ""
    return name.encode("utf-8")[:PROJECT_MAX_BYTES].decode("utf-8", errors="ignore")


class QoderMonitor:
    """内存聚合器：事件驱动，无后台线程。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}

    def record_event(self, payload: dict[str, Any], now: float | None = None) -> str | None:
        """记录一条 hook 事件；返回映射后的状态，未映射事件返回 None。"""
        now = time.time() if now is None else now
        if not isinstance(payload, dict):
            return None
        event = str(payload.get("hook_event_name") or "")
        session_id = str(payload.get("session_id") or "")
        if not session_id or len(session_id) > 128:
            return None
        status = EVENT_STATUS.get(event)
        if status is None:
            return None
        session = {"status": status, "project": _project_name(payload.get("cwd")), "updated_at": now}
        with self._lock:
            self._sessions[session_id] = session
            while len(self._sessions) > MAX_SESSIONS:
                oldest = min(self._sessions, key=lambda key: self._sessions[key]["updated_at"])
                del self._sessions[oldest]
        return status

    def set_enabled(self, enabled: bool) -> None:
        if not enabled:
            with self._lock:
                self._sessions.clear()

    def snapshot(self, module_enabled: bool = True, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        with self._lock:
            sessions = [dict(value) for value in self._sessions.values()]
        sessions = [item for item in sessions if now - item["updated_at"] <= SESSION_TTL_SECONDS]
        sessions.sort(key=lambda item: item["updated_at"], reverse=True)
        primary = sessions[0] if sessions else None
        if not module_enabled:
            service_state, status, project = "disabled", "idle", ""
        elif primary is None:
            service_state, status, project = "idle", "idle", ""
        else:
            service_state, status, project = "online", primary["status"], primary["project"]
        return {
            "module_enabled": module_enabled,
            "service_state": service_state,
            "configured": True,
            "connected": service_state == "online",
            "source": "qoder_hooks",
            "status": status,
            "status_text": STATUS_TEXT.get(status, "离线"),
            "project": project,
            "session_count": len(sessions),
            "sessions": [
                {
                    "status": item["status"],
                    "status_text": STATUS_TEXT.get(item["status"], "离线"),
                    "project": item["project"],
                    "updated_at_epoch": int(item["updated_at"]),
                }
                for item in sessions[:SNAPSHOT_SESSIONS]
            ],
            "updated_at_epoch": int(primary["updated_at"]) if primary else 0,
        }


def _is_our_entry(entry: Any) -> bool:
    """识别我们注入的 matcher 组：组的 hooks 里含事件上报命令。"""
    if not isinstance(entry, dict):
        return False
    commands = [str(hook.get("command", "")) for hook in entry.get("hooks", []) if isinstance(hook, dict)]
    return any(EVENT_URL_PATH in command for command in commands)


def hooks_installed(path: Path) -> bool:
    """检查设置文件中是否仍有我们的上报条目（只读，异常一律视为未安装）。"""
    try:
        settings = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(settings, dict):
        return False
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        if any(_is_our_entry(entry) for entry in entries):
            return True
    return False


def _our_command(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None
    for hook in entry.get("hooks", []):
        if isinstance(hook, dict):
            command = str(hook.get("command", ""))
            if EVENT_URL_PATH in command:
                return command
    return None


def update_hooks(path: Path, port: int, action: str = "install") -> dict[str, Any]:
    """在 Qoder settings.json 中安装/移除事件上报 hooks（幂等，保留用户已有配置）。

    Qoder 的 settings.json 是扁平结构：hooks → 事件 → matcher 组列表，
    同文件还有 enabledPlugins 等无关配置，必须原样保留。我们为每个事件
    追加一个独立 matcher 组。首次写入前备份为 ``<settings.json>.dotii-backup``
    （已存在时不覆盖，保留最早原始状态）；任何格式异常都抛出 ValueError
    且不改动文件。修改后需重启 Qoder IDE 生效（hooks 不支持热加载）。
    """
    if action not in {"install", "remove"}:
        raise ValueError("action 必须是 install 或 remove")
    try:
        raw = path.read_text(encoding="utf-8") if path.is_file() else "{}"
        settings = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取 Qoder settings 文件：{error.__class__.__name__}") from error
    if not isinstance(settings, dict):
        raise ValueError("Qoder settings 文件格式不受支持")

    hooks = settings.get("hooks") if "hooks" in settings else {}
    if not isinstance(hooks, dict):
        raise ValueError("Qoder settings 文件的 hooks 配置格式不受支持")

    command = hook_command(port)
    changed: list[str] = []
    for event in HOOK_EVENTS:
        entries = hooks.get(event, [])
        if not isinstance(entries, list):
            raise ValueError(f"hooks.{event} 格式不受支持")
        ours = [entry for entry in entries if _is_our_entry(entry)]
        if action == "install":
            if len(ours) == 1 and _our_command(ours[0]) == command:
                continue
            remaining = [entry for entry in entries if not _is_our_entry(entry)]
            hooks[event] = remaining + [{"matcher": "", "hooks": [{"type": "command", "command": command, "timeout": 30}]}]
            changed.append(event)
        else:
            if not ours:
                continue
            remaining = [entry for entry in entries if not _is_our_entry(entry)]
            if remaining:
                hooks[event] = remaining
            else:
                hooks.pop(event, None)
            changed.append(event)
    if not changed:
        return {"ok": True, "action": action, "changed": [], "settings_path": str(path)}
    settings["hooks"] = hooks
    backup = path.with_name(path.name + ".dotii-backup")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and not backup.is_file():
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    temporary = path.with_name(path.name + ".dotii-tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return {"ok": True, "action": action, "changed": changed, "settings_path": str(path)}


# ---- 用量仪表盘：PAT → cosy jobToken → sash 额度接口（CN 区域） ----

USAGE_POLL_INTERVAL_SECONDS = 120.0
USAGE_MAX_BACKOFF_SECONDS = 600.0
USAGE_REQUEST_TIMEOUT_SECONDS = 10.0
USAGE_IDLE_WAIT_SECONDS = 5.0
USAGE_MAX_STALE_SECONDS = 1800.0
USAGE_SLEEP_RESUME_JUMP_SECONDS = 30.0
USAGE_MAX_RESPONSE_BYTES = 65536
USAGE_GATEWAY_HOST = "gateway.qoder.com.cn"
USAGE_OPENAPI_HOST = "openapi.qoder.com.cn"
JOB_TOKEN_PATH = "/algo/api/v3/user/jobToken?Encode=1"
USAGE_PATH = "/sash/api/v2/me/usage"
TOKEN_REFRESH_MARGIN_SECONDS = 300.0

# cosy 协议常量（社区逆向 + 官方产物核对，与 qoder2api 逐字节一致）。
COSY_SECRET_B64 = "d2FyLCB3YXIgbmV2ZXIgY2hhbmdlcw=="
COSY_CUSTOM_ALPHABET = "_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!"
_COSY_STD_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_COSY_S2C = {_COSY_STD_ALPHABET[i]: COSY_CUSTOM_ALPHABET[i] for i in range(64)}
_COSY_S2C["="] = "$"


def cosy_encode(plaintext: bytes) -> str:
    """cosy 自定义 base64：标准 base64 后按 n/3 三块重排，再映射自定义字母表。"""
    std = base64.b64encode(plaintext).decode()
    n = len(std)
    a = n // 3
    rearranged = std[n - a:] + std[a:n - a] + std[:a]
    return "".join(_COSY_S2C[ch] for ch in rearranged)


def cosy_signature(date: str) -> str:
    """旧式请求签名：md5("cosy&<secret>&<rfc1123date>")。"""
    return hashlib.md5(f"cosy&{COSY_SECRET_B64}&{date}".encode()).hexdigest()


def _fingerprint(pat: str) -> dict[str, str]:
    """以 PAT 为种子派生稳定设备指纹（无 uid 可用时的回退路径）。"""
    seed = f"cred:{pat}"
    machine_id = hashlib.md5(f"machine:{seed}".encode()).hexdigest()
    machine_type = hashlib.md5(f"machinetype:{seed}".encode()).hexdigest()[:18]
    machine_token = base64.urlsafe_b64encode(
        hashlib.sha512(f"machinetoken:{seed}".encode()).digest()
    ).decode().rstrip("=")[:43]
    return {"machineId": machine_id, "machineType": machine_type, "machineToken": machine_token}


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse_usage(payload: Any) -> dict[str, Any]:
    """解析额度响应：优先套餐额度 userQuota，无套餐时回落组织资源包。

    percent 统一按 已用/总额 计算并夹取到 0..100，不依赖服务端
    percentage 字段（不同额度桶语义可能不一致）。附加额度包 addOnQuota
    是独立桶，不计入主仪表盘。
    """
    result: dict[str, Any] = {
        "available": False, "used": 0, "total": 0, "percent": 0,
        "plan": "", "exceeded": False, "bucket": "", "error": "",
    }
    if not isinstance(payload, dict):
        result["error"] = "用量响应格式不受支持"
        return result
    source = payload.get("qoderUsage")
    if not isinstance(source, dict):
        source = payload
    result["plan"] = str(source.get("userType") or "")[:15]
    result["exceeded"] = source.get("isQuotaExceeded") is True
    bucket = source.get("userQuota")
    kind = "plan"
    if not isinstance(bucket, dict) or not _number(bucket.get("total")) or bucket["total"] <= 0:
        org = source.get("orgResourcePackage")
        if isinstance(org, dict) and org.get("available") is not False and _number(org.get("cap")) and org["cap"] > 0:
            bucket = {"total": org["cap"], "used": org.get("used")}
            kind = "org"
        else:
            bucket = None
    if bucket is None:
        result["error"] = "接口未提供额度数据"
        return result
    total = int(bucket["total"])
    used = max(0, int(bucket.get("used") or 0))
    result.update({
        "available": True,
        "used": used,
        "total": total,
        "percent": min(100, max(0, int(used * 100 / total))),
        "bucket": kind,
    })
    return result


def _usage_text(value: Any, maximum: int, field: str) -> str:
    text = str(value or "").strip()
    if len(text.encode("utf-8")) > maximum:
        raise ValueError(f"{field} 太长")
    return text


class QoderUsageConfigStore:
    DEFAULTS = {"personal_token": "", "display_enabled": False}

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
            "has_personal_token": bool(config["personal_token"]),
            "configured": bool(config["personal_token"]),
            "display_enabled": bool(config["display_enabled"]),
        }

    def validate(self, candidate: dict[str, Any], preserve_secret: bool = False) -> dict[str, Any]:
        current = self.read()
        secret = candidate.get("personal_token", "" if not preserve_secret else current["personal_token"])
        if preserve_secret and secret == "":
            secret = current["personal_token"]
        secret = _usage_text(secret, 200, "PAT")
        if secret and any(character.isspace() for character in secret):
            raise ValueError("PAT 不能包含空白字符")
        display = candidate.get("display_enabled", current["display_enabled"])
        if not isinstance(display, bool):
            raise ValueError("display_enabled 必须是布尔值")
        return {"personal_token": secret, "display_enabled": display}

    def write(self, candidate: dict[str, Any], preserve_secret: bool = False) -> dict[str, Any]:
        validated = self.validate(candidate, preserve_secret=preserve_secret)
        payload = json.dumps(validated, ensure_ascii=False, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with self._lock:
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(self.path)
        return self.public()


class QoderUsageError(Exception):
    """单次用量请求失败；message 不包含 PAT。kind: network/auth/parse/http。"""

    def __init__(self, message: str, kind: str = "unknown") -> None:
        super().__init__(message)
        self.kind = kind


class QoderUsageService:
    def __init__(self, config: QoderUsageConfigStore, module_enabled: Callable[[], bool]) -> None:
        self.config_store = config
        self.module_enabled = module_enabled
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._usage: dict[str, Any] = {}
        self._last_success = 0.0
        self._last_error = ""
        self._token = ""
        self._token_pat = ""
        self._token_expire = 0.0
        self._thread = threading.Thread(target=self._run, name="qoder-usage-poller", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=3)

    def reconfigure(self) -> None:
        with self._lock:
            self._usage = {}
            self._last_success = 0.0
            self._last_error = ""
            self._token = ""
            self._token_pat = ""
            self._token_expire = 0.0
        self._wake.set()

    def set_enabled(self, enabled: bool) -> None:
        self.reconfigure()

    def _enabled(self) -> bool:
        try:
            return bool(self.module_enabled()) and bool(self.config_store.read()["display_enabled"])
        except Exception:
            return False

    def _http_json(self, method: str, url: str, headers: dict[str, str], body: str | None = None) -> dict[str, Any]:
        """单次 HTTPS JSON 请求；失败抛 QoderUsageError（按性质分类）。"""
        parts = urllib.parse.urlsplit(url)
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        connection = http.client.HTTPSConnection(parts.hostname, timeout=USAGE_REQUEST_TIMEOUT_SECONDS)
        try:
            connection.request(method, target, body=body.encode("utf-8") if body is not None else None, headers=headers)
            response = connection.getresponse()
            raw = response.read(USAGE_MAX_RESPONSE_BYTES + 1)
            status = response.status
        except (OSError, http.client.HTTPException) as error:
            raise QoderUsageError(f"无法连接 {parts.hostname}", "network") from error
        finally:
            connection.close()
        detail = raw[:200].decode("utf-8", errors="ignore").strip()
        if status in (401, 403):
            raise QoderUsageError(f"PAT 认证失败：{detail or f'HTTP {status}'}"[:120], "auth")
        if status != 200:
            raise QoderUsageError(f"接口返回 HTTP {status}", "http")
        if len(raw) > USAGE_MAX_RESPONSE_BYTES:
            raise QoderUsageError("用量响应过大", "parse")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise QoderUsageError("响应不是有效 JSON", "parse") from error
        if not isinstance(data, dict):
            raise QoderUsageError("响应格式不受支持", "parse")
        return data

    def _exchange_token(self, pat: str) -> tuple[str, float]:
        """PAT → cosy 编码 jobToken 交换，返回 (jt- 令牌, 到期墙钟秒)。"""
        fingerprint = _fingerprint(pat)
        date = formatdate(usegmt=True)
        inner = json.dumps({
            "personalToken": pat, "securityOauthToken": "", "refreshToken": "",
            "needRefresh": False, "authInfo": {},
        })
        body = cosy_encode(json.dumps({"payload": inner, "encodeVersion": "1"}).encode())
        data = self._http_json("POST", f"https://{USAGE_GATEWAY_HOST}{JOB_TOKEN_PATH}", {
            "cosy-machinetoken": fingerprint["machineToken"],
            "cosy-machinetype": fingerprint["machineType"],
            "cosy-machineid": fingerprint["machineId"],
            "login-version": "v2",
            "appcode": "cosy",
            "accept": "application/json",
            "accept-encoding": "identity",
            "cosy-version": "0.1.43",
            "cosy-clienttype": "5",
            "date": date,
            "signature": cosy_signature(date),
            "content-type": "application/json",
            "user-agent": "Go-http-client/2.0",
        }, body=body)
        token = str(data.get("securityOauthToken") or "")
        if not token:
            raise QoderUsageError("jobToken 交换响应缺少令牌", "parse")
        expire = data.get("expireTime")
        expire_epoch = float(expire) / 1000.0 if _number(expire) and expire > 1e12 else float(expire) if _number(expire) else 0.0
        if expire_epoch <= 0:
            expire_epoch = time.time() + 1800.0
        return token, expire_epoch

    def _request_usage(self, token: str) -> dict[str, Any]:
        data = self._http_json("GET", f"https://{USAGE_OPENAPI_HOST}{USAGE_PATH}", {
            "accept": "application/json",
            "Authorization": f"Bearer {token}",
            "cosy-clienttype": "10",
            "user-agent": "Qoder",
            "accept-language": "zh-CN",
        })
        return parse_usage(data)

    def _fetch(self) -> dict[str, Any]:
        pat = str(self.config_store.read().get("personal_token") or "")
        if not pat:
            raise QoderUsageError("尚未配置 Qoder PAT")
        with self._lock:
            cached = (
                self._token_pat == pat and self._token
                and self._token_expire - time.time() > TOKEN_REFRESH_MARGIN_SECONDS
            )
            token = self._token if cached else ""
        if not token:
            token, expire = self._exchange_token(pat)
            with self._lock:
                self._token = token
                self._token_pat = pat
                self._token_expire = expire
        return self._request_usage(token)

    def probe(self) -> dict[str, Any]:
        """手动只读检测：一次交换 + 一次用量读，分项报告网络/认证/解析。"""
        result: dict[str, Any] = {
            "ok": False,
            "checked_at_epoch": int(time.time()),
            "network": {"ok": False, "detail": "尚未检测"},
            "auth": {"ok": False, "detail": "尚未检测"},
            "parse": {"ok": False, "detail": "尚未检测"},
            "available": False, "used": 0, "total": 0, "percent": 0, "plan": "--",
            "detail": "尚未完成检测",
        }
        pat = str(self.config_store.read().get("personal_token") or "")
        if not pat:
            result["network"] = {"ok": False, "detail": "尚未配置 PAT"}
            result["detail"] = "请先保存 Qoder PAT"
            return result
        try:
            token, _expire = self._exchange_token(pat)
        except QoderUsageError as error:
            result["network"] = {"ok": error.kind != "network", "detail": str(error)[:63]}
            result["auth"] = {"ok": False, "detail": str(error)[:63]}
            result["detail"] = str(error)[:63]
            return result
        result["network"] = {"ok": True, "detail": "连接正常"}
        result["auth"] = {"ok": True, "detail": "令牌交换成功"}
        try:
            usage = self._request_usage(token)
        except QoderUsageError as error:
            result["parse"] = {"ok": False, "detail": str(error)[:63]}
            result["detail"] = str(error)[:63]
            return result
        result["available"] = usage["available"]
        result["used"] = usage["used"]
        result["total"] = usage["total"]
        result["percent"] = usage["percent"]
        result["plan"] = usage["plan"] or "--"
        if not usage["available"]:
            result["parse"] = {"ok": False, "detail": usage["error"][:63]}
            result["detail"] = usage["error"][:63]
            return result
        result["parse"] = {"ok": True, "detail": "解析成功"}
        result["ok"] = True
        result["detail"] = "Qoder 用量读取正常"
        return result

    def _wait_backoff(self, backoff: float) -> bool:
        """退避分片等待；系统唤醒（墙钟远超单调流逝）时立即重试。"""
        mono_start = time.monotonic()
        wall_start = time.time()
        deadline = mono_start + backoff
        while not self._stop.is_set() and not self._wake.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._stop.wait(min(5.0, remaining))
            if time.time() - wall_start - (time.monotonic() - mono_start) > USAGE_SLEEP_RESUME_JUMP_SECONDS:
                return True
        self._wake.clear()
        return False

    def _run(self) -> None:
        backoff = USAGE_POLL_INTERVAL_SECONDS
        logged_error = ""
        while not self._stop.is_set():
            config = self.config_store.public()
            if not self._enabled() or not config["configured"]:
                with self._lock:
                    self._usage = {}
                    self._last_success = 0.0
                    self._last_error = ""
                backoff = USAGE_POLL_INTERVAL_SECONDS
                logged_error = ""
                self._wake.wait(USAGE_IDLE_WAIT_SECONDS)
                self._wake.clear()
                continue
            try:
                usage = self._fetch()
                with self._lock:
                    self._usage = usage
                    self._last_error = usage.get("error", "")
                    if not self._last_error:
                        self._last_success = time.time()
                backoff = USAGE_POLL_INTERVAL_SECONDS
                error_text = usage.get("error", "")
                if error_text and error_text != logged_error:
                    logging.warning("Qoder 用量读取异常：%s", error_text)
                logged_error = error_text
            except QoderUsageError as error:
                with self._lock:
                    self._last_error = str(error)[:63]
                backoff = min(backoff * 2, USAGE_MAX_BACKOFF_SECONDS)
                if str(error) != logged_error:
                    logging.warning("Qoder 用量请求失败：%s（%.0f 秒后重试）", error, backoff)
                logged_error = str(error)
            if self._wait_backoff(backoff):
                backoff = USAGE_POLL_INTERVAL_SECONDS

    def snapshot(self) -> dict[str, Any]:
        config = self.config_store.public()
        enabled = self._enabled()
        with self._lock:
            usage = dict(self._usage)
            last_success = self._last_success
            error = self._last_error
        stale = not last_success or time.time() - last_success > USAGE_MAX_STALE_SECONDS
        has_data = bool(usage.get("available"))
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
            "display_enabled": config["display_enabled"],
            "service_state": service_state,
            "connected": connected,
            "source": "qoder_usage_api",
            "available": has_data,
            "used": int(usage.get("used", 0)),
            "total": int(usage.get("total", 0)),
            "percent": int(usage.get("percent", 0)),
            "plan": str(usage.get("plan", ""))[:15],
            "exceeded": bool(usage.get("exceeded", False)),
            "updated_at_epoch": int(last_success),
            "last_error": error,
        }