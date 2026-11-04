"""Notion 待办事项同步客户端.

通过 Notion API 读取指定数据库中的今日待办事项：
POST https://api.notion.com/v1/databases/{database_id}/query
认证头为 ``Authorization: Bearer <API Key>`` 和 ``Notion-Version: 2022-06-28``。
API Key 只保存在本机运行目录，不进入设备快照或日志。
"""
from __future__ import annotations

import http.client
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

SYNC_INTERVAL_SECONDS = 300.0  # 5 分钟同步一次
MAX_BACKOFF_SECONDS = 600.0
REQUEST_TIMEOUT_SECONDS = 15.0
IDLE_WAIT_SECONDS = 5.0
MAX_STALE_SECONDS = 1800.0
NOTION_API_HOST = "api.notion.com"
NOTION_VERSION = "2022-06-28"
MAX_RESPONSE_BYTES = 1024 * 1024  # 1MB


def _text(value: Any, maximum: int, field: str) -> str:
    text = str(value or "").strip()
    if len(text.encode("utf-8")) > maximum:
        raise ValueError(f"{field} 太长")
    return text


def _extract_rich_text(rich_text: Any) -> str:
    """从 Notion rich_text 数组中提取纯文本。"""
    if not isinstance(rich_text, list):
        return ""
    parts: list[str] = []
    for item in rich_text:
        if isinstance(item, dict) and isinstance(item.get("plain_text"), str):
            parts.append(item["plain_text"])
    return "".join(parts).strip()


def _parse_date(date_value: Any) -> str | None:
    """解析 Notion date 属性，返回 YYYY-MM-DD 格式或 None。"""
    if not isinstance(date_value, dict):
        return None
    start = date_value.get("start")
    if not isinstance(start, str):
        return None
    # 提取日期部分
    return start[:10] if len(start) >= 10 else None


def _is_today(date_str: str | None, now: datetime | None = None) -> bool:
    """检查日期字符串是否是今天。"""
    if not date_str:
        return False
    now = now or datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    return date_str == today_str


def _extract_title(page: dict[str, Any]) -> str:
    """从 Notion page 对象中提取标题。"""
    properties = page.get("properties", {})
    if not isinstance(properties, dict):
        return ""
    # 尝试常见的标题属性名
    for key in ("Name", "Title", "标题", "名称", "Task", "任务"):
        prop = properties.get(key)
        if isinstance(prop, dict):
            if prop.get("type") == "title":
                return _extract_rich_text(prop.get("title", []))
            if prop.get("type") == "rich_text":
                return _extract_rich_text(prop.get("rich_text", []))
    # 兜底：遍历所有属性找第一个 title 类型
    for prop in properties.values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return _extract_rich_text(prop.get("title", []))
    return ""


def _extract_date(page: dict[str, Any]) -> str | None:
    """从 Notion page 对象中提取日期。"""
    properties = page.get("properties", {})
    if not isinstance(properties, dict):
        return None
    # 尝试常见的日期属性名
    for key in ("Date", "日期", "Due", "Due Date", "Deadline"):
        prop = properties.get(key)
        if isinstance(prop, dict) and prop.get("type") == "date":
            return _parse_date(prop.get("date"))
    # 兜底：遍历所有属性找第一个 date 类型
    for prop in properties.values():
        if isinstance(prop, dict) and prop.get("type") == "date":
            return _parse_date(prop.get("date"))
    return None


def _extract_status(page: dict[str, Any]) -> str:
    """从 Notion page 对象中提取状态。"""
    properties = page.get("properties", {})
    if not isinstance(properties, dict):
        return ""
    for key in ("Status", "状态", "Done", "Complete"):
        prop = properties.get(key)
        if isinstance(prop, dict):
            if prop.get("type") == "status":
                status = prop.get("status", {})
                return status.get("name", "") if isinstance(status, dict) else ""
            if prop.get("type") == "checkbox":
                return "已完成" if prop.get("checkbox") else "待完成"
            if prop.get("type") == "select":
                select = prop.get("select", {})
                return select.get("name", "") if isinstance(select, dict) else ""
    return ""


def parse_today_todos(payload: Any, now: datetime | None = None) -> dict[str, Any]:
    """解析 Notion 数据库查询响应，提取今日待办事项。"""
    now = now or datetime.now()
    result: dict[str, Any] = {
        "todos": [],
        "total_count": 0,
        "today_count": 0,
        "error": "",
        "synced_at_epoch": int(time.time()),
    }
    
    if not isinstance(payload, dict):
        result["error"] = "响应格式无效"
        return result
    
    results = payload.get("results", [])
    if not isinstance(results, list):
        result["error"] = "响应数据格式无效"
        return result
    
    result["total_count"] = len(results)
    today_todos: list[dict[str, str]] = []
    
    for page in results:
        if not isinstance(page, dict):
            continue
        
        title = _extract_title(page)
        if not title:
            continue
        
        date_str = _extract_date(page)
        status = _extract_status(page)
        
        # 如果日期是今天且未完成，加入待办列表
        if _is_today(date_str, now) and status not in ("已完成", "Done", "Complete", "done"):
            today_todos.append({
                "title": title[:100],  # 限制标题长度
                "status": status or "待完成",
                "date": date_str or "",
            })
    
    result["todos"] = today_todos[:20]  # 最多返回 20 条
    result["today_count"] = len(today_todos)
    return result


class NotionConfigStore:
    DEFAULTS = {"api_key": "", "database_id": "", "enabled": False}

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
            "has_api_key": bool(config["api_key"]),
            "has_database_id": bool(config["database_id"]),
            "configured": bool(config["api_key"]) and bool(config["database_id"]),
            "enabled": bool(config["enabled"]),
        }

    def validate(self, candidate: dict[str, Any], preserve_secret: bool = False) -> dict[str, Any]:
        current = self.read()
        secret = candidate.get("api_key", "" if not preserve_secret else current["api_key"])
        if preserve_secret and secret == "":
            secret = current["api_key"]
        secret = _text(secret, 200, "API Key")
        if secret and any(character.isspace() for character in secret):
            raise ValueError("API Key 不能包含空白字符")
        
        database_id = _text(candidate.get("database_id", current["database_id"]), 100, "Database ID")
        if database_id and any(character.isspace() for character in database_id):
            raise ValueError("Database ID 不能包含空白字符")
        
        enabled = candidate.get("enabled", current["enabled"])
        if not isinstance(enabled, bool):
            raise ValueError("enabled 必须是布尔值")
        
        return {"api_key": secret, "database_id": database_id, "enabled": enabled}

    def write(self, candidate: dict[str, Any], preserve_secret: bool = False) -> dict[str, Any]:
        validated = self.validate(candidate, preserve_secret=preserve_secret)
        payload = json.dumps(validated, ensure_ascii=False, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with self._lock:
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(self.path)
        return self.public()


class NotionError(Exception):
    """单次请求失败；message 不包含 API Key。"""


class NotionService:
    def __init__(self, config: NotionConfigStore, module_enabled: Callable[[], bool]) -> None:
        self.config_store = config
        self.module_enabled = module_enabled
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._todos: dict[str, Any] = {}
        self._last_success = 0.0
        self._last_error = ""
        self._thread = threading.Thread(target=self._run, name="notion-poller", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=3)

    def reconfigure(self) -> None:
        with self._lock:
            self._todos = {}
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

    def _query_database(self, api_key: str, database_id: str) -> dict[str, Any]:
        """查询 Notion 数据库获取今日待办。"""
        connection = http.client.HTTPSConnection(NOTION_API_HOST, timeout=REQUEST_TIMEOUT_SECONDS)
        try:
            # 不使用过滤器查询，在 Python 端过滤今日待办
            body = json.dumps({"page_size": 50})
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            connection.request("POST", f"/v1/databases/{database_id}/query", body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
            status = response.status
        finally:
            connection.close()
        
        if status in (401, 403):
            raise NotionError("API Key 认证失败，请检查 Notion API Key")
        if status == 404:
            raise NotionError("数据库不存在或无权访问")
        if status != 200:
            raise NotionError(f"Notion 接口返回 HTTP {status}")
        if len(response_body) > MAX_RESPONSE_BYTES:
            raise NotionError("响应数据过大")
        
        try:
            payload = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise NotionError("响应不是有效 JSON") from error
        
        return parse_today_todos(payload)

    def _fetch(self) -> dict[str, Any]:
        config = self.config_store.read()
        api_key = str(config.get("api_key") or "")
        database_id = str(config.get("database_id") or "")
        if not api_key:
            raise NotionError("尚未配置 Notion API Key")
        if not database_id:
            raise NotionError("尚未配置数据库 ID")
        try:
            return self._query_database(api_key, database_id)
        except NotionError:
            raise
        except (OSError, http.client.HTTPException) as error:
            raise NotionError(f"无法连接 Notion 服务") from error

    def probe(self) -> dict[str, Any]:
        """手动只读检测：一次请求，分项报告网络、认证与解析状态。"""
        checked_at = int(time.time())
        result: dict[str, Any] = {
            "ok": False,
            "checked_at_epoch": checked_at,
            "network": {"ok": False, "detail": "尚未检测"},
            "auth": {"ok": False, "detail": "尚未检测"},
            "parse": {"ok": False, "detail": "尚未检测"},
            "today_count": 0,
            "todos": [],
            "detail": "尚未完成检测",
        }
        config = self.config_store.read()
        api_key = str(config.get("api_key") or "")
        database_id = str(config.get("database_id") or "")
        
        if not api_key:
            result["network"] = {"ok": False, "detail": "尚未配置 API Key"}
            result["detail"] = "请先保存 Notion API Key"
            return result
        if not database_id:
            result["network"] = {"ok": False, "detail": "尚未配置数据库 ID"}
            result["detail"] = "请先保存数据库 ID"
            return result
        
        connection = http.client.HTTPSConnection(NOTION_API_HOST, timeout=REQUEST_TIMEOUT_SECONDS)
        try:
            body = json.dumps({"page_size": 10})
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            connection.request("POST", f"/v1/databases/{database_id}/query", body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read(MAX_RESPONSE_BYTES + 1)
            status = response.status
        except (OSError, http.client.HTTPException) as error:
            result["network"] = {"ok": False, "detail": f"无法连接 Notion"}
            result["detail"] = f"网络连接失败：{error.__class__.__name__}"
            return result
        finally:
            connection.close()
        
        result["network"] = {"ok": True, "detail": f"HTTP {status}"}
        if status in (401, 403):
            result["auth"] = {"ok": False, "detail": f"HTTP {status}"}
            result["detail"] = "API Key 认证失败，请检查 Key 是否正确"
            return result
        if status == 404:
            result["auth"] = {"ok": False, "detail": "HTTP 404"}
            result["detail"] = "数据库不存在或无权访问"
            return result
        result["auth"] = {"ok": True, "detail": "认证通过"}
        
        if status != 200:
            result["parse"] = {"ok": False, "detail": f"HTTP {status}"}
            result["detail"] = f"Notion 接口返回 HTTP {status}"
            return result
        
        try:
            payload = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            result["parse"] = {"ok": False, "detail": "响应不是有效 JSON"}
            result["detail"] = "响应无法解析"
            return result
        
        todos = parse_today_todos(payload)
        result["today_count"] = todos.get("today_count", 0)
        result["todos"] = todos.get("todos", [])
        
        if todos.get("error"):
            result["parse"] = {"ok": False, "detail": todos["error"][:63]}
            result["detail"] = todos["error"][:63]
            return result
        
        result["parse"] = {"ok": True, "detail": "解析成功"}
        result["ok"] = True
        result["detail"] = f"今日待办 {result['today_count']} 条"
        return result

    def _wait_backoff(self, backoff: float) -> bool:
        mono_start = time.monotonic()
        wall_start = time.time()
        deadline = mono_start + backoff
        while not self._stop.is_set() and not self._wake.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._stop.wait(min(5.0, remaining))
            if time.time() - wall_start - (time.monotonic() - mono_start) > 30.0:
                return True
        self._wake.clear()
        return False

    def _run(self) -> None:
        backoff = SYNC_INTERVAL_SECONDS
        logged_business_error = ""
        while not self._stop.is_set():
            config = self.config_store.public()
            if not self._enabled() or not config["configured"]:
                with self._lock:
                    self._todos = {}
                    self._last_success = 0.0
                    self._last_error = ""
                backoff = SYNC_INTERVAL_SECONDS
                logged_business_error = ""
                self._wake.wait(IDLE_WAIT_SECONDS)
                self._wake.clear()
                continue
            try:
                todos = self._fetch()
                with self._lock:
                    self._todos = todos
                    self._last_error = todos.get("error", "")
                    if not self._last_error:
                        self._last_success = time.time()
                backoff = SYNC_INTERVAL_SECONDS
                business_error = todos.get("error", "")
                if business_error and business_error != logged_business_error:
                    logging.warning("Notion 同步异常：%s", business_error)
                logged_business_error = business_error
            except NotionError as error:
                with self._lock:
                    self._last_error = str(error)[:63]
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                logging.warning("Notion 同步失败：%s（%.0f 秒后重试）", error, backoff)
            if self._wait_backoff(backoff):
                backoff = SYNC_INTERVAL_SECONDS

    def snapshot(self) -> dict[str, Any]:
        config = self.config_store.public()
        enabled = self._enabled()
        with self._lock:
            todos = dict(self._todos)
            last_success = self._last_success
            error = self._last_error
        
        stale = not last_success or time.time() - last_success > MAX_STALE_SECONDS
        has_data = bool(todos.get("todos"))
        connected = enabled and config["configured"] and not stale
        
        service_state = (
            "disabled" if not enabled
            else "needs_configuration" if not config["configured"]
            else "online" if connected
            else "error" if error
            else "connecting"
        )
        
        return {
            "module_enabled": enabled,
            "configuration_complete": config["configured"],
            "service_state": service_state,
            "configured": config["configured"],
            "connected": connected,
            "source": "notion_api",
            "today_count": int(todos.get("today_count", 0)),
            "todos": todos.get("todos", []),
            "updated_at_epoch": int(last_success),
            "last_error": error,
        }
