#!/usr/bin/env python3
"""全国城市清单构建（开发期工具）。

从行政区划数据生成「城市 → 区县列表」，供采集器做同构关键词采样。

数据源：`china-division` 的 pca-code.json（省 / 市 / 区三级）。
首次运行会联网拉取并缓存到 `tools/out/pca-code.json`，之后离线可用。

层级处理约定（重要，别改）：
  - 直辖市（北京/天津/上海/重庆）：数据里其下级是「市辖区」，故城市名取省名，
    区县取「市辖区」的下级。
  - 「省直辖县级行政区划 / 自治区直辖县级行政区划」节点的下级是**县级**单位，
    与地级市不同级，**整节点跳过** —— 否则会把县级市混进"同级别比较"。
  - 港澳台不在本数据集内（与赛事面向中国大陆开发者的范围一致）。

用法：
    from cities import load_national_cities
    cities = load_national_cities()      # {"南京市": ["玄武区", ...], ...}
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "tools", "out", "pca-code.json")
SOURCE = "https://unpkg.com/china-division/dist/pca-code.json"

# 需要跳过的"下级是县级单位"的中间节点
SKIP_MARKERS = ("省直辖县级", "自治区直辖县级")

MUNICIPALITIES = {"北京市", "天津市", "上海市", "重庆市"}


def _download() -> list:
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    req = urllib.request.Request(SOURCE, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return data


def _load_raw() -> list:
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    return _download()


def load_national_cities(municipality_districts: bool = True) -> dict[str, list[str]]:
    """返回 {城市名: [区县名...]}。城市名为 MCP 可直接使用的写法（含「市/自治州/地区/盟」后缀）。"""
    out: dict[str, list[str]] = {}
    for prov in _load_raw():
        pname = prov.get("name") or ""
        for city in prov.get("children") or []:
            cname = city.get("name") or ""
            districts = [d.get("name") for d in (city.get("children") or []) if d.get("name")]

            if any(m in cname for m in SKIP_MARKERS):
                continue

            # 直辖市：下级是「市辖区」占位，改用省名做城市名
            if pname in MUNICIPALITIES and cname in ("市辖区", "县"):
                if municipality_districts:
                    out.setdefault(pname, [])
                    out[pname].extend(districts)
                continue

            if cname:
                out.setdefault(cname, [])
                out[cname].extend(districts)

    # 去重并保序
    return {c: list(dict.fromkeys(ds)) for c, ds in out.items()}


def load_city_province() -> dict[str, str]:
    """返回 {城市名: 省级行政区名}。

    专供**省级口径校验**使用：把 337 个地级市的采样门店数按省加总，
    与窄门餐眼公布的省级门店数对照，就能量出每个省的采样覆盖率。
    直辖市（北京/天津/上海/重庆）的省级名与城市名相同。
    """
    out: dict[str, str] = {}
    for prov in _load_raw():
        pname = prov.get("name") or ""
        for city in prov.get("children") or []:
            cname = city.get("name") or ""
            if any(m in cname for m in SKIP_MARKERS):
                continue
            if pname in MUNICIPALITIES and cname in ("市辖区", "县"):
                out[pname] = pname
                continue
            if cname:
                out.setdefault(cname, pname)
    return out


def city_level(name: str, province: str | None = None) -> str:
    """粗分行政级别，仅用于展示分组，不参与打分。"""
    if name in MUNICIPALITIES:
        return "直辖市"
    SUBS = {"哈尔滨市", "长春市", "沈阳市", "大连市", "济南市", "青岛市", "南京市",
            "杭州市", "宁波市", "厦门市", "武汉市", "广州市", "深圳市", "成都市", "西安市"}
    if name in SUBS:
        return "副省级市"
    if name.endswith(("自治州", "地区", "盟")):
        return "自治州/地区/盟"
    return "地级市"


def main() -> None:
    cities = load_national_cities()
    total_districts = sum(len(v) for v in cities.values())
    print(f"城市数 {len(cities)}，区县合计 {total_districts}，"
          f"平均每城 {total_districts / len(cities):.1f} 个区县")
    from collections import Counter
    lv = Counter()
    for c in cities:
        lv[city_level(c)] += 1
    for k, v in lv.most_common():
        print(f"  {k}: {v}")
    print("\n样例：")
    for c in list(cities)[:3] + ["南京市", "苏州市", "北京市", "重庆市"]:
        if c in cities:
            print(f"  {c} ({len(cities[c])}) {' '.join(cities[c][:8])}")
    if "--save" in sys.argv:
        out = os.path.join(ROOT, "tools", "out", "cities.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(cities, f, ensure_ascii=False, indent=1)
        print(f"\n[已保存] {out}")


if __name__ == "__main__":
    main()
