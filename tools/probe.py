#!/usr/bin/env python3
"""地址探针（开发期工具）—— 实时打 MCP，回答「我这个点位周边麦当劳有多密」。

城市模式走离线预采数据；但用户给的是地址/地标/门店名时，锚点不在预采集合里，
必须实时查。本脚本做且只做这件事，然后把结果放进预采的锚点参照分布里定位水位。

用法:
    python tools/probe.py 南京 珠江路435号
    python tools/probe.py 苏州 观前街
    python tools/probe.py 无锡 江南大学 --json

⚠️ city 是 MCP 的必填参数。用户没说城市时不要瞎猜，先问。
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import db  # noqa: E402
import mcdindex as M  # noqa: E402
from mcd_client import McdClient  # noqa: E402

DATA = os.path.join(ROOT, "data", "observations.json")


def load_reference() -> tuple[list[float], dict]:
    """锚点参照分布优先取本地 SQLite 工作库，没有则退回 JSON 快照。"""
    cities = db.load_observations()
    return M.anchor_reference(cities), cities


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 2:
        sys.exit(__doc__)
    raw_city, keyword = args[0], args[1]

    ref, cities = load_reference()
    city = M.resolve_city(raw_city, cities) or raw_city
    if city not in cities:
        print(f"[提示] 「{raw_city}」不在已采样城市组内，只给点位密度水位。")

    client = McdClient()
    hits = client.call("query-nearby-stores", {
        "beType": 1, "searchType": 2, "city": city, "keyword": keyword,
    })
    if not isinstance(hits, list):
        hits = []
    if not hits:
        sys.exit(f"锚点「{raw_city} {keyword}」附近没有查到门店。"
                 f"换个更具体的地址，或确认城市名是否正确。")

    dists = [h.get("distance") for h in hits if isinstance(h.get("distance"), (int, float))]
    d5 = max(dists) if len(dists) >= 3 else None
    reading = M.anchor_reading(d5, ref, found=len(hits))

    out = {
        "anchor": {"city": city, "keyword": keyword},
        "nearby": [
            {"storeCode": h.get("storeCode"), "name": h.get("storeName"),
             "address": h.get("address"), "distance": h.get("distance"),
             "open": h.get("businessStartTime"), "close": h.get("businessEndTime"),
             "reservable": h.get("reservation")}
            for h in hits
        ],
        "d5": d5,
        "reading": reading,
        "cityIndex": None,
    }

    if city in cities:
        res = M.build_index(cities)
        row = res["cities"][city]
        out["cityIndex"] = {
            "score": row["score"], "rank": row["rank"], "rankOf": row["rankOf"],
            "subScores": row["subScores"],
        }

    if "--json" in sys.argv:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    print(f"\n锚点：{city} · {keyword}")
    print("=" * 62)
    print(f"{'距离':>8}  {'门店':<26} 地址")
    print("-" * 62)
    for h in out["nearby"]:
        d = h["distance"]
        print(f"{(str(round(d)) + ' m') if isinstance(d, (int, float)) else '—':>8}  "
              f"{(h['name'] or '')[:26]:<26} {(h['address'] or '')[:28]}")

    print("=" * 62)
    if reading["percentile"] is not None:
        print(f"第 5 家门店距离 {reading['d5']} m  →  门店密度水位 {reading['percentile']} 分位")
        print(f"参照分布：{reading['referenceSize']} 个锚点，中位 {reading['median']} m")
        print(f"判定：{reading['verdict']}")
    else:
        print(reading["verdict"])

    if out["cityIndex"]:
        ci = out["cityIndex"]
        print(f"\n所属城市：{city} MCDIndex {ci['score']}（同组第 {ci['rank']}/{ci['rankOf']}）")
        print(f"  门店密度 {ci['subScores']['density']} · "
              f"核心区覆盖 {ci['subScores']['reach']} · "
              f"得来速 {ci['subScores']['driveThru']}")
    else:
        print(f"\n注意：{city} 不在已采样的城市组内，只给点位密度水位，不给城市指数。")

    print("\n说明：门店数为同构采样下界，不代表官方门店总数；指数只做同组相对比较。")


if __name__ == "__main__":
    main()
