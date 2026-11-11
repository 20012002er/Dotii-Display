"""TRAE 工作状态采集.

通过 TRAE 官方 hooks 事件推送聚合各会话的任务状态。只消费
``session_id`` / ``cwd`` / ``hook_event_name`` / ``notification_type``
这几个状态字段，不解析、不存储消息内容。权限请求转发（PreToolUse
hook → Dotii 屏上批准）复用 `claudecode_client.PermissionBroker`。
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

MAX_SESSIONS = 16
SESSION_TTL_SECONDS = 24 * 3600.0
SNAPSHOT_SESSIONS = 4
PROJECT_MAX_BYTES = 47
EVENT_URL_PATH = "/api/v1/admin/trae/event"
PERMISSION_URL_PATH = "/api/v1/admin/trae/permission"

# hook 事件 → 会话状态（与 codex.task.status 同一状态集合，供固件复用）
EVENT_STATUS = {
    "UserPromptSubmit": "working",
    "PreToolUse": "working",
    "PostToolUse": "working",
    "Stop": "completed",
    "SessionStart": "idle",
}

# TRAE Notification 类型 → 状态映射
NOTIFICATION_STATUS = {
    "idle_prompt": "completed",
    "permission_prompt": "waiting_user",
    "document_review": "waiting_user",
    "ask_user_question": "waiting_user",
    "browser_interaction": "waiting_user",
}

STATUS_TEXT = {
    "working": "工作中",
    "waiting_user": "等待用户",
    "completed": "已完成",
    "failed": "失败",
    "idle": "暂无任务",
    "offline": "离线",
}

# TRAE 支持的事件（无 SessionEnd，无 StopFailure）
HOOK_EVENTS = (
    "SessionStart", "UserPromptSubmit",
    "PostToolUse", "Stop", "Notification",
)


def hook_command(port: int) -> str:
    """裸 curl 命令：macOS/Linux 的 shell 与 Windows 的 cmd 都能直接执行，
    stdin 由 TRAE 直接送入 curl，不依赖 sh 外壳或单引号语法。
    事件上报是尽力而为——管理中心离线（如重启间隙）时静默退出 0，
    不在 TRAE 界面上产生 hook 错误提示。"""
    return (
        f"curl -s -m 2 -X POST http://127.0.0.1:{port}{EVENT_URL_PATH} "
        f"-H \"Content-Type: application/json\" --data-binary @- || exit 0"
    )


def permission_hook_command(port: int) -> str:
    """PreToolUse 决策 hook：同裸 curl 形态，但服务端会 hold 响应
    等待 Dotii 上的批准（上限 30s），curl 超时留 5s 余量。管理中心
    不可达时静默退出 0——空输出让 TRAE 照常弹本机权限提示。"""
    return (
        f"curl -s -m 35 -X POST http://127.0.0.1:{port}{PERMISSION_URL_PATH} "
        f"-H \"Content-Type: application/json\" --data-binary @- || exit 0"
    )


def _project_name(cwd: Any) -> str:
    text = str(cwd or "").rstrip("/\\")
    name = text.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] if text else ""
    return name.encode("utf-8")[:PROJECT_MAX_BYTES].decode("utf-8", errors="ignore")


class TraeMonitor:
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
        if status is None and event == "Notification":
            notification_type = str(payload.get("notification_type") or "")
            status = NOTIFICATION_STATUS.get(notification_type)
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
            "source": "trae_hooks",
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
    """识别我们注入的 matcher 组：组的 hooks 里含事件上报或权限决策命令。"""
    if not isinstance(entry, dict):
        return False
    commands = [str(hook.get("command", "")) for hook in entry.get("hooks", []) if isinstance(hook, dict)]
    return any((EVENT_URL_PATH in command or PERMISSION_URL_PATH in command) for command in commands)


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
            if EVENT_URL_PATH in command or PERMISSION_URL_PATH in command:
                return command
    return None


def update_hooks(path: Path, port: int, action: str = "install") -> dict[str, Any]:
    """在 TRAE hooks.json 中安装/移除事件上报 hooks（幂等，保留用户已有配置）。

    TRAE 的 hooks.json 采用嵌套结构：version → hooks → 事件 → matcher 组列表。
    我们为每个事件追加一个独立 matcher 组。首次写入前备份为
    ``<hooks.json>.dotii-backup``（已存在时不覆盖，保留最早原始状态）；
    任何格式异常都抛出 ValueError 且不改动文件。
    """
    if action not in {"install", "remove"}:
        raise ValueError("action 必须是 install 或 remove")
    try:
        raw = path.read_text(encoding="utf-8") if path.is_file() else "{}"
        settings = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取 TRAE hooks 文件：{error.__class__.__name__}") from error
    if not isinstance(settings, dict):
        raise ValueError("TRAE hooks 文件格式不受支持")

    # TRAE hooks.json 顶层有 version 字段
    if "version" not in settings:
        settings["version"] = 1
    hooks = settings.get("hooks") if "hooks" in settings else {}
    if not isinstance(hooks, dict):
        raise ValueError("TRAE hooks 文件的 hooks 配置格式不受支持")

    command = hook_command(port)
    permission_command = permission_hook_command(port)
    # 普通事件上报组 + PreToolUse 权限决策组一起安装/移除。
    install_plan = [(event, command) for event in HOOK_EVENTS]
    install_plan.append(("PreToolUse", permission_command))
    changed: list[str] = []
    for event, event_command in install_plan:
        entries = hooks.get(event, [])
        if not isinstance(entries, list):
            raise ValueError(f"hooks.{event} 格式不受支持")
        ours = [entry for entry in entries if _is_our_entry(entry)]
        if action == "install":
            if len(ours) == 1 and _our_command(ours[0]) == event_command:
                continue
            remaining = [entry for entry in entries if not _is_our_entry(entry)]
            hooks[event] = remaining + [{"matcher": "", "hooks": [{"type": "command", "command": event_command, "timeout": 35 if event == "PreToolUse" else 30}]}]
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
