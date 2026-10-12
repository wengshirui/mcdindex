#!/usr/bin/env python3
"""全国 MCDIndex 分析：排名 + 两组外部基准对照（开发期工具，不是交付物）。

回答一个问题：**全国前 20 名，符不符合公开门店数 / GDP 排名？**

三件事：
  1. 跑 MCDIndex，输出全国前 20。
  2. 省级口径校验 —— 把 337 个市的采样门店数按省加总，与窄门餐眼
     2025-12 的省级实数对照，量出各省的**采样覆盖率**。这是唯一
     能真正验证"同构采样"是否靠谱的外部基准（省级公开数据可信，
     市级公开数据基本没有）。
  3. 与 2025 城市 GDP 排名做秩相关，并给出**覆盖率校正后**的排名，
     看前 20 是否稳定。

用法：
    python tools/national_analysis.py            # 全部
    python tools/national_analysis.py --top 30
"""

from __future__ import annotations

import json
import os
import sys
from statistics import median

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402
import mcdindex as M  # noqa: E402
from cities import load_city_province, load_national_cities  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OBS = os.path.join(ROOT, "data", "observations.json")

# --------------------------------------------------------------------------
# 外部基准 1：窄门餐眼《麦当劳 2025 品牌年终报告》，数据截至 2025-12
#   全国 8373 家，覆盖 34 省 328 市。下表为省级存量门店数。
#   可信度高：与"广东省 >1800 家""江苏/浙江 500~700 家""排名第 10 的天津近 250 家"
#   等多篇独立报道一致。
# --------------------------------------------------------------------------
PUBLIC_PROVINCE_STORES = {
    "广东省": 1859, "江苏省": 638, "浙江省": 620, "北京市": 615, "上海市": 583,
    "福建省": 422, "山东省": 300, "湖北省": 293, "四川省": 281, "天津市": 246,
    "辽宁省": 219, "河北省": 196, "安徽省": 141, "陕西省": 120, "河南省": 105,
    "山西省": 95, "黑龙江省": 85, "海南省": 85, "内蒙古自治区": 77, "云南省": 76,
    "吉林省": 75, "江西省": 69, "重庆市": 66, "贵州省": 48, "甘肃省": 31,
    "新疆维吾尔自治区": 30, "西藏自治区": 12, "宁夏回族自治区": 3, "青海省": 1,
}
PUBLIC_NATIONAL_TOTAL = 8373
PUBLIC_CITY_COVERAGE = 328

# --------------------------------------------------------------------------
# 外部基准 2：2025 年城市 GDP（亿元）。来源：各市统计局，经 gotohui 汇总，
# 与人民网/南方+ 报道口径一致。只取前 50，够覆盖任何合理的指数前 20。
# --------------------------------------------------------------------------
PUBLIC_CITY_GDP = {
    "上海市": 56708.71, "北京市": 52073.4, "深圳市": 38731.8, "重庆市": 33757.93,
    "广州市": 32039.46, "苏州市": 27695.09, "成都市": 24763.61, "杭州市": 23010.9,
    "武汉市": 22147.35, "南京市": 19428.78, "宁波市": 18715.68, "天津市": 18539.82,
    "青岛市": 17560.67, "无锡市": 16773.94, "长沙市": 15737.82, "郑州市": 15244.6,
    "福州市": 15112.32, "济南市": 14210.09, "合肥市": 14210.0, "西安市": 13902.67,
    "泉州市": 13778.34, "佛山市": 13157.35, "南通市": 12801.5, "东莞市": 12760.2,
    "烟台市": 11350.98, "常州市": 11158.71, "唐山市": 10450.2, "温州市": 10213.92,
    "大连市": 10002.1, "徐州市": 9957.22, "沈阳市": 9100.33, "厦门市": 8980.37,
    "绍兴市": 8932.13, "石家庄市": 8651.7, "昆明市": 8637.45, "潍坊市": 8587.36,
    "南昌市": 8141.69, "扬州市": 8056.75, "盐城市": 8045.28, "长春市": 8005.59,
    "嘉兴市": 7851.06, "榆林市": 7501.06, "金华市": 7313.47, "泰州市": 7255.27,
    "台州市": 7005.87, "临沂市": 6862.15, "宜昌市": 6464.42, "惠州市": 6363.66,
    "南宁市": 6212.46, "哈尔滨市": 6188.5,
}

# 外部基准 3：公开报道的市级门店数（零散，仅作点对点校验）
PUBLIC_CITY_STORES = {
    "深圳市": (460, "深圳市商务局，截至 2025-07，口径为深圳市域名店数"),
    "北京市": (615, "窄门餐眼 2025-12（直辖市省级数=市级数）"),
    "上海市": (583, "窄门餐眼 2025-12"),
    "天津市": (246, "窄门餐眼 2025-12"),
    "重庆市": (66, "窄门餐眼 2025-12（该值异常偏低，存疑）"),
}


# --------------------------------------------------------------------------
# 秩相关（无 scipy 依赖）
# --------------------------------------------------------------------------

def _ranks(vals: list[float]) -> list[float]:
    """平均秩。"""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return round(num / (dx * dy), 3) if dx and dy else None


# --------------------------------------------------------------------------

def load() -> dict:
    """优先从本地 SQLite 工作库读；没有库时退回 JSON 快照。"""
    return db.load_observations()


def probe_diagnostics(conn, cities: dict) -> None:
    """探针失败归因 —— 这是落 SQLite 之后才问得出的问题。

    关心的是：**采样到底薄在哪**。如果失败集中在某类关键词上，那么
    "门店密度"这个指标就不是均匀地薄，而是系统性地漏掉某些区域。
    """
    roads = set(db.common_keywords()["roads"])
    poi = set(db.common_keywords()["poi"])

    def kind(city: str, kw: str) -> str:
        if kw == city:
            return "城市名"
        if kw == "麦当劳":
            return "品牌词"
        if kw in set(cities.get(city) or []):
            return "区县"
        if kw in roads:
            return "通用道路"
        if kw in poi:
            return "通用POI"
        return "其他"

    agg: dict[str, dict[str, int]] = {}
    errs: dict[str, dict[str, int]] = {}
    for r in conn.execute("SELECT city, keyword, status, error_code FROM probe WHERE pass=1"):
        k = kind(r["city"], r["keyword"])
        a = agg.setdefault(k, {"ok": 0, "empty": 0, "error": 0, "unknown": 0, "n": 0})
        a[r["status"]] = a.get(r["status"], 0) + 1
        a["n"] += 1
        if r["status"] == "error":
            e = errs.setdefault(k, {})
            e[r["error_code"] or "(无码)"] = e.get(r["error_code"] or "(无码)", 0) + 1

    print("\n" + "=" * 96)
    print("探针成败归因（pass=1 门店轮，按关键词类型）")
    print("-" * 96)
    print(f"{'关键词类型':<12}{'探针数':>8}{'采到门店':>10}{'周边无门店':>12}"
          f"{'真故障':>8}{'待重采':>8}{'命中率':>9}")
    for k in ("城市名", "品牌词", "区县", "通用道路", "通用POI", "其他"):
        a = agg.get(k)
        if not a:
            continue
        hit = a["ok"] / a["n"] * 100 if a["n"] else 0
        print(f"{k:<12}{a['n']:>8}{a['ok']:>10}{a['empty']:>12}"
              f"{a['error']:>8}{a['unknown']:>8}{hit:>8.1f}%")
    tot = {s: sum(a.get(s, 0) for a in agg.values()) for s in ("n", "ok", "empty", "error", "unknown")}
    print("-" * 96)
    print(f"{'合计':<12}{tot['n']:>8}{tot['ok']:>10}{tot['empty']:>12}"
          f"{tot['error']:>8}{tot['unknown']:>8}{tot['ok'] / max(1, tot['n']) * 100:>8.1f}%")

    print("\n错误码分布（含原文）：")
    seen = set()
    for r in conn.execute(
            "SELECT error_code, errmsg, COUNT(*) n FROM probe WHERE status='error'"
            " GROUP BY error_code ORDER BY n DESC LIMIT 10"):
        print(f"  {r['error_code'] or '(无码)':>8}  {r['n']:>7}  {(r['errmsg'] or '')[-46:]}")
        seen.add(r["error_code"])
    for k, e in sorted(errs.items(), key=lambda kv: -sum(kv[1].values())):
        if any(c not in seen for c in e):
            print(f"  [{k}] " + "  ".join(f"{c}×{n}" for c, n in sorted(e.items(), key=lambda kv: -kv[1])))
    print("  说明：600047「当前位置附近没有找到合适的餐厅」= 锚点周边确实没门店（稀疏信号，非故障）；")
    print("        600014「没有查询到该地址」= 锚点文本没解析出位置。二者都按 empty 处理，不再重打。")


def probe_density(city_obs: dict) -> dict:
    """区县级锚点探针 —— 一个**不依赖采样饱和度**的密度读数。

    每个区县名都是一个锚点，问"该区县中心第 5 家门店有多远"。
    这个量只要 5 个结果就能测出来，不随城市门店总量被 5 家上限截断，
    因此比"采样门店数"更抗偏差。
      fullShare  该城有多少比例的区县锚点能查到满 5 家（越大越密）
      medianD5   这些锚点第 5 家距离的中位数（米，越小越密）
    """
    probes = city_obs.get("anchorProbes") or {}
    # 只取"区/县/市/旗"结尾的锚点，避开通用道路与 POI 名的跨城频次差异。
    # 注意：开发区/新城区/老城区/高新区这些通用 POI 也是「区」结尾，必须排掉，
    # 否则混进来的就不是"行政中心锚点"，各城锚点性质不一致。
    tails = ("区", "县", "市", "旗")
    generic = {"开发区", "新城区", "老城区", "高新区", "工业园", "大学城", "麦当劳"}
    district = {k: v for k, v in probes.items()
                if k.endswith(tails) and k not in generic}
    if not district:
        return {"n": 0, "fullShare": None, "medianD5": None}
    full = sum(1 for v in district.values() if (v or {}).get("n") == 5)
    d5s = [v["d5"] for v in district.values()
           if isinstance((v or {}).get("d5"), (int, float)) and v.get("n") == 5]
    return {
        "n": len(district),
        "fullShare": round(full / len(district), 3),
        "medianD5": round(median(d5s), 1) if d5s else None,
    }


def main() -> None:
    argv = sys.argv[1:]
    top_n = int(argv[argv.index("--top") + 1]) if "--top" in argv else 20

    cities = load()
    print(f"载入 {len(cities)} 城观测\n")

    # ---------- 0. 省级聚合（排名的覆盖率列要用，所以先算）----------
    prov_of = load_city_province()
    agg: dict[str, int] = {}
    for c, o in cities.items():
        p = prov_of.get(c)
        if p:
            agg[p] = agg.get(p, 0) + int(o.get("sampledStoreCount") or 0)
    rates = {p: agg[p] / pub for p, pub in PUBLIC_PROVINCE_STORES.items() if p in agg}

    # ---------- 1. MCDIndex ----------
    result = M.build_index(cities)
    rows = [result["cities"][c] for c in result["order"]]
    print("=" * 96)
    print(f"{'#':>3} {'城市':<10}{'指数':>7}{'采样门店':>9}{'密度分':>8}"
          f"{'核心区分':>9}{'得来速分':>9}{'得来速%':>8}{'第5家距离':>10}{'省覆盖率':>9}")
    print("-" * 96)
    for i, r in enumerate(rows[:top_n], 1):
        raw, sub = r["raw"], r["subScores"]
        rate = rates.get(prov_of.get(r["city"]))
        print(f"{i:>3} {r['city']:<10}{r['score']:>7}"
              f"{raw['sampledStores']:>9}"
              f"{sub['density'] if sub['density'] is not None else '-':>8}"
              f"{sub['reach'] if sub['reach'] is not None else '-':>9}"
              f"{sub['driveThru'] if sub['driveThru'] is not None else '-':>9}"
              f"{raw['driveThruShare'] if raw['driveThruShare'] is not None else '-':>8}"
              f"{round(raw['coreReach5']) if raw['coreReach5'] else '-':>10}"
              f"{(str(round(rate * 100, 1)) + '%') if rate else '-':>9}")
    print("=" * 96)
    print("维度区分度诊断：")
    for k, d in result["diagnostics"].items():
        print(f"  {k:<10} 相对极差 {d['spread']:<8} 置信 {d['reliability']:<6} "
              f"[{d['min']} ~ {d['max']}]  计分={'是' if d['discriminating'] else '收缩'}")
    print(f"  价格篮子  相对极差 {result['readingsDiagnostics']['price']['spread']} "
          f"[{result['readingsDiagnostics']['price']['min']} ~ "
          f"{result['readingsDiagnostics']['price']['max']}]  （仅读数）")
    print(f"  营业时长  相对极差 {result['readingsDiagnostics']['hours']['spread']} "
          f"[{result['readingsDiagnostics']['hours']['min']} ~ "
          f"{result['readingsDiagnostics']['hours']['max']}]  （仅读数）")

    # ---------- 2. 省级校验 ----------
    print("\n" + "=" * 96)
    print("省级口径校验（我苏采样 vs 窄门餐眼 2025-12 公开实数）")
    print("-" * 96)
    print(f"{'省份':<14}{'采样':>7}{'公开':>7}{'覆盖率':>9}{'缺口':>8}")
    checked = []
    for p, pub in sorted(PUBLIC_PROVINCE_STORES.items(), key=lambda kv: -kv[1]):
        mine = agg.get(p)
        if mine is None:
            print(f"{p:<14}{'—':>7}{pub:>7}{'':>9}{'':>8}  （清单未覆盖）")
            continue
        checked.append((p, mine, pub, mine / pub))
        print(f"{p:<14}{mine:>7}{pub:>7}{mine / pub * 100:>8.1f}%{pub - mine:>8}")
    tot_mine = sum(m for _, m, _, _ in checked)
    tot_pub = sum(p for _, _, p, _ in checked)
    print("-" * 96)
    print(f"{'合计(有公开数)':<14}{tot_mine:>7}{tot_pub:>7}{tot_mine / tot_pub * 100:>8.1f}%"
          f"{tot_pub - tot_mine:>8}")
    all_mine = sum(agg.values())
    print(f"全部 {len(cities)} 城采样合计 {all_mine} 家；"
          f"窄门餐眼全国 {PUBLIC_NATIONAL_TOTAL} 家 → 全国覆盖率 "
          f"{all_mine / PUBLIC_NATIONAL_TOTAL * 100:.1f}%")

    # 覆盖率是否与密度相关？（密集省覆盖率应当更低 —— 这就是偏差的来源）
    if len(checked) >= 5:
        xs = [pub for _, _, pub, _ in checked]
        ys = [r for _, _, _, r in checked]
        print(f"\n省级覆盖率 vs 该省公开门店数的秩相关 = {spearman(xs, ys)}"
              "  （显著为负 = 门店越多的省被低估越多，即已知的采样偏差方向）")

    # ---------- 3. GDP 对照 ----------
    idx_rank = {r["city"]: r["rank"] for r in rows}
    score_of = {r["city"]: r["score"] for r in rows}
    gdp_order = sorted(PUBLIC_CITY_GDP, key=lambda c: -PUBLIC_CITY_GDP[c])
    gdp_rank = {c: i for i, c in enumerate(gdp_order, 1)}

    top20 = [r["city"] for r in rows[:20]]
    print("\n" + "=" * 96)
    print(f"前 {top_n} 名 vs 2025 城市 GDP 排名")
    print("-" * 96)
    print(f"{'MCDIndex#':>9}{'城市':<11}{'指数':>7}{'GDP名':>7}{'GDP(亿)':>11}{'位差':>7}")
    for i, c in enumerate(top20, 1):
        gr = gdp_rank.get(c)
        delta = "" if gr is None else f"{gr - i:+d}"
        gv = f"{PUBLIC_CITY_GDP[c]:.0f}" if c in PUBLIC_CITY_GDP else "未进前50"
        print(f"{i:>9}{c:<11}{score_of[c]:>7}{gr if gr else '—':>7}{gv:>11}{delta:>7}")

    gdp_top20 = set(gdp_order[:20])
    idx_top20 = set(top20)
    print(f"\n两个榜单的交集 {len(idx_top20 & gdp_top20)} 城："
          f"{'、'.join(sorted(idx_top20 & gdp_top20, key=lambda c: idx_rank[c]))}")
    print(f"GDP 前 20 但未进指数前 20（{len(gdp_top20 - idx_top20)} 城）："
          f"{'、'.join(f'{c}(指数{idx_rank.get(c, '未采')})' for c in sorted(gdp_top20 - idx_top20, key=lambda c: gdp_rank[c]))}")
    print(f"指数前 20 但不在 GDP 前 20（{len(idx_top20 - gdp_top20)} 城）："
          f"{'、'.join(sorted(idx_top20 - gdp_top20, key=lambda c: idx_rank[c]))}")

    common = [c for c in gdp_order if c in idx_rank]
    s = spearman([PUBLIC_CITY_GDP[c] for c in common], [score_of[c] for c in common])
    print(f"\nGDP 前 50 中已采到的 {len(common)} 城，GDP 与 MCDIndex 的秩相关 = {s}")
    print("（|ρ| < 0.5 视为弱相关）")

    # ---------- 4. 覆盖率校正后的排名（稳健性检验）----------
    print("\n" + "=" * 96)
    print("稳健性：按省级覆盖率校正门店数后重排（假设省内各市覆盖率相同）")
    print("-" * 96)
    corrected_cities = {}
    for c, o in cities.items():
        p = prov_of.get(c)
        r = rates.get(p)
        o2 = dict(o)
        n = int(o.get("sampledStoreCount") or 0)
        if r and r > 0:
            o2["sampledStoreCount"] = n / r
        corrected_cities[c] = o2
    r2 = M.build_index(corrected_cities)
    corr_top20 = r2["order"][:20]
    print(f"{'校正#':>7}{'城市':<11}{'原#':>6}{'变动':>7}")
    for i, c in enumerate(corr_top20, 1):
        print(f"{i:>7}{c:<11}{idx_rank[c]:>6}{idx_rank[c] - i:>+7}")
    print(f"\n校正前后前 20 名的交集：{len(set(corr_top20) & idx_top20)}/20")
    print(f"校正后新进前 20："
          f"{'、'.join(f'{c}(原{idx_rank[c]})' for c in corr_top20 if c not in idx_top20)}")
    print(f"校正后掉出前 20："
          f"{'、'.join(f'{c}(校正{r2['cities'][c]['rank']})' for c in top20 if c not in corr_top20)}")

    # ---------- 5. 零散市级点校验 ----------
    print("\n" + "=" * 96)
    print("市级点对点校验（公开数零散，仅参考）")
    print("-" * 96)
    for c, (pub, src) in PUBLIC_CITY_STORES.items():
        mine = int(cities.get(c, {}).get("sampledStoreCount") or 0)
        if mine:
            print(f"{c:<8} 采样 {mine:>4} / 公开 {pub:>4} = {mine / pub * 100:>5.1f}%   {src}")
        else:
            print(f"{c:<8} 无采样   / 公开 {pub:>4}          {src}")

    # ---------- 6. 探针密度（抗偏差读数）----------
    print("\n" + "=" * 96)
    print("区县锚点探针读数（抗 5 家上限偏差，仅展示）")
    print("-" * 96)
    print(f"{'城市':<11}{'区县锚点':>8}{'满 5 家占比':>12}{'第5家中位(m)':>13}{'MCDIndex#':>10}")
    probes = {c: probe_density(cities[c]) for c in top20}
    for i, c in enumerate(top20, 1):
        pd = probes[c]
        print(f"{c:<11}{pd['n']:>8}{pd['fullShare']:>12.2f}"
              f"{pd['medianD5'] if pd['medianD5'] else '-':>13}{i:>10}")

    # ---------- 7. 探针失败归因（依赖 SQLite 工作库）----------
    try:
        probe_diagnostics(db.connect(), load_national_cities())
    except Exception as e:                                  # noqa: BLE001
        print(f"\n[跳过探针归因] {type(e).__name__}: {e}")

    out = os.path.join(ROOT, "tools", "out", "national-analysis.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "top20": [{"rank": i, "city": c, "score": score_of[c],
                       "gdpRank": gdp_rank.get(c),
                       "sampled": cities[c]["sampledStoreCount"]} for i, c in enumerate(top20, 1)],
            "provinceCheck": [{"province": p, "mine": m, "public": pub,
                               "rate": round(r, 4)} for p, m, pub, r in checked],
            "gdpSpearman": s,
            "coverageRateSpearman": spearman([p for _, _, p, _ in checked],
                                             [r for _, _, _, r in checked]),
            "correctedTop20": corr_top20,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n[已保存] {out}")


if __name__ == "__main__":
    main()
