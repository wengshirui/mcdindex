#!/usr/bin/env python3
"""麦当劳 MCP 客户端（开发期工具）。

职责：握手 + tools/call，带 429 退避重试。只暴露"拿业务数据"这一件事。
Token 从 config.txt 读取，绝不写入任何输出。

用法：
    from mcd_client import McdClient
    c = McdClient()
    data = c.call("query-meals", {"storeCode": "1990524", "orderType": 1, "beType": 1})
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config.txt")
URL = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-06-18"

# 业务失败的报文形如："query-nearby-stores: 600014 没有查询到该地址，请检查配送地址是否正确"
_CODE_RE = re.compile(r":\s*(\d{4,6})\s")

# 需要重试的**业务**错误码。注意 508 是 JSON 体里的业务码（"太火爆了"），
# 不是 HTTP 状态码 —— 只在 `_post` 里重试 HTTP 429/5xx 是抓不到它的，
# 实测 30 并发下 508 能占到全部失败的 20%，白白丢数据。
RETRY_CODES = {"429", "500", "502", "503", "504", "508"}


class McpError(RuntimeError):
    """MCP 调用失败。`code` 是业务错误码（如 600014 / 508），拿不到时为 None。

    续采要靠它区分"永久失败"和"瞬时失败"：`600014 没有查询到该地址` 重打也是白打，
    而 `508 太火爆了` 必须重试。
    """

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def load_token() -> str:
    if not os.path.exists(CONFIG):
        raise McpError(f"找不到 {CONFIG}，请先写入 MCP token。")
    raw = open(CONFIG, encoding="utf-8-sig").read().strip()
    try:
        token = json.loads(raw)["token"]
    except (ValueError, KeyError, TypeError):
        token = raw
    if not token:
        raise McpError("config.txt 里没有 token。")
    return token


class McdClient:
    """无状态 MCP 客户端。服务端不返回 Mcp-Session-Id，每次调用独立。"""

    def __init__(self, token: str | None = None, max_retry: int = 5):
        self.token = token or load_token()
        self.max_retry = max_retry
        self.calls = 0
        self.errors: list[str] = []
        self.error_codes: dict[str, int] = {}
        self._handshake()

    # ---------- 底层 ----------

    def _post(self, payload: dict) -> dict:
        body = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
        }
        req = urllib.request.Request(URL, data=body, headers=headers, method="POST")

        delay = 1.0
        last = None
        for attempt in range(self.max_retry):
            try:
                with urllib.request.urlopen(req, timeout=40) as resp:
                    text = resp.read().decode("utf-8", "replace")
                for line in text.splitlines():
                    line = line.strip()
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if line.startswith("{"):
                        return json.loads(line)
                return {}
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (429, 500, 502, 503, 504) and attempt < self.max_retry - 1:
                    time.sleep(delay + random.random() * 0.4)
                    delay *= 1.8
                    continue
                detail = e.read().decode("utf-8", "replace")[:300]
                raise McpError(f"HTTP {e.code} {e.reason} {detail}", str(e.code)) from e
            except (urllib.error.URLError, TimeoutError) as e:
                last = e
                if attempt < self.max_retry - 1:
                    time.sleep(delay + random.random() * 0.4)
                    delay *= 1.8
                    continue
                raise McpError(f"网络错误：{e}") from e
        raise McpError(f"重试 {self.max_retry} 次仍失败：{last}")

    def _handshake(self) -> None:
        self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mcdindex-tools", "version": "1.0"},
            },
        })
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except McpError:
            pass

    # ---------- 上层 ----------

    def call(self, name: str, args: dict | None = None) -> dict:
        """调一个工具，返回内层 data。业务失败抛 McpError。"""
        reply = self._post({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": name, "arguments": args or {}},
        })
        self.calls += 1
        if "error" in reply:
            raise McpError(f"{name}: {reply['error']}")
        result = reply.get("result") or {}
        content = result.get("content") or []
        if not content:
            raise McpError(f"{name}: 空 content")
        text = content[0].get("text", "")
        start = text.find('{"success"')
        if start < 0:
            raise McpError(f"{name}: 无法定位响应体")
        obj = json.loads(text[start:text.rfind("}") + 1])
        if not obj.get("success"):
            code = obj.get("code")
            raise McpError(f"{name}: {code} {obj.get('message')}",
                           None if code is None else str(code))
        return obj.get("data")

    def try_call(self, name: str, args: dict | None = None):
        """调一个工具，失败返回 None 并记进 self.errors（用于批量采集容错）。"""
        try:
            return self.call(name, args)
        except McpError as e:
            self.errors.append(str(e))
            return None

    def call_ex(self, name: str, args: dict | None = None):
        """同 try_call，但把失败原因结构化返回：`(data, error_code, error_message)`。

        采集器需要它 —— 只有知道每次探针**为什么**失败，才能区分
        「这个锚点不存在」与「服务端限流」，进而决定续采时补哪些。

        瞬时的业务错误码在这里就地重试（带退避），不必等下一轮续采：
        508「太火爆了」在高并发下很常见，但它恰恰是**能救回来的**失败。
        """
        delay = 0.8
        for attempt in range(self.max_retry):
            try:
                data = self.call(name, args)
                return data, None, None
            except McpError as e:
                code = e.code or self._guess_code(str(e))
                if code in RETRY_CODES and attempt < self.max_retry - 1:
                    time.sleep(delay + random.random() * 0.4)
                    delay *= 1.8
                    continue
                self.errors.append(str(e))
                self.error_codes[code or "(无码)"] = self.error_codes.get(code or "(无码)", 0) + 1
                return None, code, str(e)

    @staticmethod
    def _guess_code(message: str) -> str | None:
        m = _CODE_RE.search(message)
        return m.group(1) if m else None
