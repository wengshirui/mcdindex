#!/usr/bin/env python3
"""只读探针：提取麦当劳 MCP 各工具返回值的【结构】，不输出任何具体业务值。

目的：验证某个玩法是否有真实数据支撑（字段齐不齐、菜单多不多），
而不是把用户账号数据打印出来。个人字段一律只显示字段名与类型。

用法:
    python tools/probe_shapes.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config.txt")
URL = "https://mcp.mcd.cn"


def load_token() -> str:
    raw = open(CONFIG, encoding="utf-8-sig").read().strip()
    try:
        return json.loads(raw)["token"]
    except (ValueError, KeyError, TypeError):
        return raw


TOKEN = load_token()


def rpc(payload: dict, sid: str | None = None):
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {TOKEN}",
            **({"Mcp-Session-Id": sid} if sid else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return {"__http_error__": f"{e.code} {e.read().decode('utf-8', 'replace')[:200]}"}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line.startswith("{"):
            return json.loads(line)
    return None


def call(name: str, args: dict | None = None):
    sid = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "probe-shapes", "version": "1.0"}}})
    r = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": name, "arguments": args or {}}})
    if not r:
        return None
    if "error" in r:
        return {"__rpc_error__": r["error"]}
    res = r.get("result", {})
    if res.get("isError"):
        return {"__tool_error__": str(res)[:300]}
    out = []
    for c in res.get("content", []):
        if c.get("type") == "text":
            try:
                out.append(json.loads(c["text"]))
            except ValueError:
                out.append(c["text"])
    return out[0] if len(out) == 1 else out


def shape(v, depth=0, max_depth=4):
    """把任意 JSON 值压缩成『结构描述』，丢弃所有具体业务值。"""
    pad = "  " * depth
    if isinstance(v, dict):
        if depth >= max_depth:
            return f"{{{len(v)} keys}}"
        lines = []
        for k, val in v.items():
            if isinstance(val, (dict, list)):
                lines.append(f"{pad}{k}:")
                lines.append(shape(val, depth + 1, max_depth))
            else:
                kind = type(val).__name__
                if isinstance(val, bool):
                    lines.append(f"{pad}{k}: bool")
                elif val is None:
                    lines.append(f"{pad}{k}: null")
                else:
                    lines.append(f"{pad}{k}: {kind}")
        return "\n".join(lines)
    if isinstance(v, list):
        if not v:
            return f"{pad}[空数组]"
        return f"{pad}[len={len(v)}] 首元素:\n" + shape(v[0], depth + 1, max_depth)
    return f"{pad}{type(v).__name__}"


CASES: list[tuple[str, str, dict]] = [
    ("now-time-info", "服务器时间", {}),
    ("list-nutrition-foods", "营养数据（餐品覆盖度）", {}),
    ("mall-points-products", "商城商品（当卡池/商店）", {}),
    ("query-lottery-info", "积分抽奖（当扭蛋）", {}),
    ("campaign-calendar", "活动日历", {}),
    ("query-my-account", "积分账户【个人】", {}),
    ("order-list", "历史订单【个人】— 玩法核心依赖", {}),
    ("query-my-coupons", "我的券【个人】", {"page": "1", "pageSize": "5"}),
    ("query-nearby-stores", "附近门店（当地图节点）", {"beType": 1, "searchType": 2, "city": "北京"}),
]


def main() -> None:
    store = None
    for name, label, args in CASES:
        print("=" * 74)
        print(f"{name}  —— {label}")
        got = call(name, args)
        if got is None:
            print("  (无返回)")
            continue
        if isinstance(got, dict) and any(str(k).startswith("__") for k in got):
            print(f"  ⚠️ {got}")
            continue
        print(shape(got, 1))
        if name == "query-nearby-stores" and isinstance(got, dict):
            for node in (got.get("data") or got.get("stores") or []):
                if isinstance(node, dict):
                    store = {k: node.get(k) for k in ("storeCode", "beCode") if node.get(k)}
                    break

    if store:
        print("=" * 74)
        print("query-meals  —— 菜单规模（当卡池）")
        got = call("query-meals", {**store, "orderType": 1, "beType": 1})
        if isinstance(got, dict) and any(str(k).startswith("__") for k in got):
            print(f"  ⚠️ {got}")
        else:
            print(shape(got, 1))


if __name__ == "__main__":
    sys.exit(main())
