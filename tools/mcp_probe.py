#!/usr/bin/env python3
"""麦当劳 MCP 探针：完成 initialize 握手并抓取 tools/list。

开发期工具，只在 tools/ 下使用；src/ 不依赖它。
Token 从项目根目录 config.txt 读取，绝不写入输出文件或标准输出。

用法:
    python tools/mcp_probe.py                 # 打印工具清单摘要
    python tools/mcp_probe.py --json          # 打印完整原始 JSON
    python tools/mcp_probe.py --save          # 另存 tools/out/mcp-tools.json
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config.txt")
OUT_DIR = os.path.join(ROOT, "tools", "out")
OUT_FILE = os.path.join(OUT_DIR, "mcp-tools.json")

URL = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-06-18"


def load_token() -> str:
    """从 config.txt 读取 token。支持 {"token": "..."} 或裸字符串。"""
    if not os.path.exists(CONFIG):
        sys.exit(f"找不到 {CONFIG}，请先在其中写入 MCP token。")
    raw = open(CONFIG, encoding="utf-8-sig").read().strip()
    try:
        token = json.loads(raw)["token"]
    except (ValueError, KeyError, TypeError):
        token = raw
    if not token:
        sys.exit("config.txt 里没有 token。")
    return token


def post(token: str, payload: dict, session_id: str | None = None) -> tuple[dict | None, str | None]:
    """发一条 JSON-RPC 请求，返回 (解析后的报文, session_id)。"""
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    req = urllib.request.Request(URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            new_session = resp.headers.get("Mcp-Session-Id") or session_id
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        sys.exit(f"HTTP {e.code} {e.reason}\n{detail}")

    # 可能返回 SSE (data: {...}) 或纯 JSON
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line.startswith("{"):
            return json.loads(line), new_session
    return None, new_session


def main() -> None:
    token = load_token()

    init, sid = post(token, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mcd-probe", "version": "1.0"},
        },
    })

    server = (init or {}).get("result", {}).get("serverInfo", {})
    print(f"[握手] OK  server={server.get('name')} {server.get('version')}")
    print(f"[会话] Mcp-Session-Id={'有' if sid else '无'}")

    # initialized 通知（无 id，服务端可能返回 202/空体）
    try:
        post(token, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    except SystemExit as e:
        print(f"[提示] initialized 通知返回异常，继续尝试 tools/list: {e}", file=sys.stderr)

    reply, _ = post(token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, sid)
    tools = (reply or {}).get("result", {}).get("tools", [])
    if not tools:
        sys.exit(f"未取到工具列表，原始返回：{json.dumps(reply, ensure_ascii=False)[:600]}")

    print(f"[工具] 共 {len(tools)} 个\n")
    if "--json" in sys.argv:
        print(json.dumps(reply, ensure_ascii=False, indent=2))
    else:
        for i, t in enumerate(tools, 1):
            desc = (t.get("description") or "").replace("\n", " ").strip()
            print(f"{i:>2}. {t['name']}")
            print(f"    {desc[:160]}")

    if "--save" in sys.argv:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(OUT_FILE, "w", encoding="utf-8") as f:
            json.dump({"serverInfo": server, "tools": tools}, f, ensure_ascii=False, indent=2)
        print(f"\n[已保存] {OUT_FILE}")


if __name__ == "__main__":
    main()
