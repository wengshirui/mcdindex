"""MCDIndex —— 城市麦当劳指数计算引擎。

设计前提（全部来自实测，不是假设）
--------------------------------------------------------------------------
1. MCP 的 `query-nearby-stores` 单次固定上限 5 家、无分页；`latitude/longitude`
   恒返回 0.0，入参也没有坐标字段 → **拿不到门店绝对总数，也拿不到坐标**。
2. 唯一可行的替代是"同构关键词采样"：所有城市用同一套关键词模板轮询去重。
   结果仍是**采样下界**，偏差随门店密度增大而放大（实测南京覆盖 ~59%、无锡 ~100%）。
3. => 所以本引擎**只做组内相对比较，不做绝对量推断**。所有分项一律转成
   「同级别城市内的百分位」，同构采样带来的偏差在组内被抵消。

选维度时踩过的坑（重要）
--------------------------------------------------------------------------
第一版曾把「价格」和「营业时长」计入合成，实测发现二者在江苏省内**几乎没有区分度**：
    · 59 个共有 SKU 的挂牌价篮子，13 城极差仅 1.66%；
    · 营业时长中位数全部落在 15.0~16.25 小时。
给这种近乎无差异的量做百分位，等于把测量噪声放大成 0~100 的排名差异 —— 假精度。
因此引入 `reliability` 收缩：某维度相对极差低于 `MIN_SPREAD` 时，得分自动向 50 塌缩。
价格与营业时长降级为"读数"（readings），只展示、不计分。

扩到全国 337 城后复测（2026-10-12）：
    · 价格篮子 117 个共有 SKU，极差 0.285×（¥3117 ~ ¥4207）→ 仍低于门槛，继续只作读数；
    · 营业时长 11.0 ~ 24.0 h，极差 0.765× → **这次达标了**，但仍不计分 ——
      24 小时店与城市规模高度共线，计分等于把"城市大"这个因子重复计一次。
    这个复测本身就是护栏有效的证据：换到更大尺度，该被拦的仍然被拦住。

最终计入合成的三个维度（全国尺度实测相对极差）
--------------------------------------------------------------------------
  密度 density     同构采样门店数            0 ~ 216 家（43.2×）
  覆盖 reach       城市核心锚点第 5 家距离   848 ~ 9775 m（2.35×，越小越好，抗采样偏差）
  得来速 driveThru 得来速门店占比            0% ~ 100%（1.0×）

模块约定：纯计算，零 I/O、零第三方依赖、无全局状态。
"""

from __future__ import annotations

from statistics import median
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "DEFAULT_WEIGHTS",
    "MIN_SPREAD",
    "parse_hhmm",
    "open_minutes",
    "store_hours",
    "city_price_map",
    "common_basket",
    "basket_total",
    "city_readings",
    "relative_spread",
    "reliability",
    "percentile",
    "score_dimension",
    "anchor_reference",
    "anchor_reading",
    "resolve_city",
    "build_index",
    "explain",
]


DEFAULT_WEIGHTS: dict[str, float] = {
    "density": 0.55,
    "reach": 0.25,
    "driveThru": 0.20,
}

# 相对极差低于该阈值即视为"该维度没有区分度"，得分向 50 塌缩以拒绝假精度
MIN_SPREAD = 0.30

# 归类为"长时段营业"的阈值（分钟）
LONG_HOURS_MIN = 16 * 60


# --------------------------------------------------------------------------
# 时间与门店属性
# --------------------------------------------------------------------------

def parse_hhmm(value: Any) -> int | None:
    """'07:30' -> 450（分钟）。无法解析返回 None。"""
    if not isinstance(value, str) or ":" not in value:
        return None
    hh, _, mm = value.partition(":")
    try:
        h, m = int(hh), int(mm)
    except ValueError:
        return None
    if not (0 <= h < 24 and 0 <= m < 60):
        return None
    return h * 60 + m


def open_minutes(open_at: Any, close_at: Any) -> int | None:
    """营业时长（分钟），正确处理跨零点。"""
    start, end = parse_hhmm(open_at), parse_hhmm(close_at)
    if start is None or end is None:
        return None
    span = end - start
    if span <= 0:
        span += 24 * 60
    return span


def store_hours(store: Mapping[str, Any]) -> int | None:
    return open_minutes(store.get("open"), store.get("close"))


# --------------------------------------------------------------------------
# 价格：把"整店菜单"压成"城市级 SKU 价格表"
# --------------------------------------------------------------------------

def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def city_price_map(city_obs: Mapping[str, Any]) -> dict[str, float]:
    """取该城各价格样本门店的 **originalPrice 中位数**，得到城市级 SKU 价格表。

    只用 originalPrice（挂牌价）。currentPrice 含限时活动价与卡券绑定，不可跨城比。
    """
    per_sku: dict[str, list[float]] = {}
    for sample in city_obs.get("priceSamples") or []:
        for code, item in (sample.get("items") or {}).items():
            price = _to_float((item or {}).get("original"))
            if price is not None and price > 0:
                per_sku.setdefault(code, []).append(price)
    return {code: median(vals) for code, vals in per_sku.items()}


def common_basket(
    price_maps: Mapping[str, Mapping[str, float]],
    coverage: float = 1.0,
) -> list[str]:
    """选出在至少 `coverage` 比例城市里都有报价的 SKU，作为比价篮子。"""
    if not price_maps:
        return []
    need = max(1, int(len(price_maps) * coverage + 0.9999))
    tally: dict[str, int] = {}
    for code_map in price_maps.values():
        for code in code_map:
            tally[code] = tally.get(code, 0) + 1
    return sorted(c for c, n in tally.items() if n >= need)


def basket_total(price_map: Mapping[str, float], basket: Iterable[str]) -> float | None:
    """篮子内 SKU 挂牌价合计；缺项超过两成则判为不可比，返回 None。"""
    codes = list(basket)
    if not codes:
        return None
    chosen = [price_map[c] for c in codes if c in price_map]
    if len(chosen) < len(codes) * 0.8:
        return None
    return round(sum(chosen), 2)


# --------------------------------------------------------------------------
# 读数（不计入合成，只展示）
# --------------------------------------------------------------------------

def city_readings(city_obs: Mapping[str, Any]) -> dict[str, Any]:
    """门店侧的辅助读数：营业时长、长时段占比、可预约占比、菜单规模、得来速数。"""
    stores = list((city_obs.get("stores") or {}).values())
    durations = [d for d in (store_hours(s) for s in stores) if d]
    return {
        "sampledStores": len(stores),
        "hoursMedian": round(median(durations) / 60, 1) if durations else None,
        "longHoursShare": round(sum(1 for d in durations if d >= LONG_HOURS_MIN) / len(durations), 3)
                          if durations else None,
        "reservableShare": round(sum(1 for s in stores if s.get("reservable")) / len(stores), 3)
                           if stores else None,
        "driveThruCount": int(city_obs.get("driveThruCount") or 0),
        "driveThruBase": int(city_obs.get("driveThruBase") or len(stores)),
        "driveThruMatched": int(city_obs.get("driveThruMatched") or 0),
        "menuSku": sum(int(s.get("itemCount") or 0)
                       for s in (city_obs.get("priceSamples") or [])),
    }


def _core_reach(city_obs: Mapping[str, Any]) -> float | None:
    """城市核心锚点第 5 家门店的距离（米）。越小说明核心区门店越密。

    锚点由 MCP 对城市名做文本解析得到，位置不完全可控 —— 这是本维度已知的局限，
    但它**不依赖采样饱和度**，因此比门店计数更抗偏差。
    """
    hits = (city_obs.get("coreProbe") or {}).get("hits") or []
    dists = [h.get("distance") for h in hits if isinstance(h.get("distance"), (int, float))]
    if len(dists) < 3:
        return None
    return float(max(dists))


# --------------------------------------------------------------------------
# 区分度诊断与打分
# --------------------------------------------------------------------------

def relative_spread(population: Sequence[float]) -> float:
    """相对极差 = (max-min)/median。用于判断一个维度是否值得计分。

    中位数为 0 时改用以最大值为分母。否则「半数城市取 0、另一端最高到 47%」这种
    明显有区分度的分布会被算成 0，进而被护栏误判成"无区分度"而塌缩到 50 ——
    得来速占比在全国尺度上正是这一形态（只有 120 个城市有得来速记录）。
    """
    vals = [v for v in population if v is not None]
    if len(vals) < 2:
        return 0.0
    low, high = min(vals), max(vals)
    if high == low:
        return 0.0
    denom = median(vals)
    if not denom:
        denom = max(abs(v) for v in vals)
    if not denom:
        return 0.0
    return round((high - low) / abs(denom), 4)


def reliability(spread: float, min_spread: float = MIN_SPREAD) -> float:
    """把相对极差折算成 0~1 的置信权重。区分度不足则向 0 收缩。"""
    if min_spread <= 0:
        return 1.0
    return round(min(1.0, max(0.0, spread / min_spread)), 3)


def percentile(value: float, population: Sequence[float]) -> float:
    """value 在 population 中的百分位（0~100）。并列取平均秩 —— 非参数，抗偏态。"""
    vals = [v for v in population if v is not None]
    if not vals or value is None:
        return 50.0
    if len(vals) == 1:
        return 50.0
    below = sum(1 for v in vals if v < value)
    equal = sum(1 for v in vals if v == value)
    avg_rank = below + (equal - 1) / 2.0
    return round(avg_rank / (len(vals) - 1) * 100, 1)


def score_dimension(
    value: float | None,
    population: Sequence[float],
    higher_is_better: bool = True,
    min_spread: float = MIN_SPREAD,
) -> tuple[float | None, dict[str, Any]]:
    """把一个原始量转成 0~100 分项，并返回该维度的区分度诊断。

    护栏：相对极差 < min_spread 时，分数向 50 收缩，避免把噪声当差异。
    """
    vals = [v for v in population if v is not None]
    spread = relative_spread(vals)
    rel = reliability(spread, min_spread)
    diag = {
        "spread": spread,
        "reliability": rel,
        "min": round(min(vals), 3) if vals else None,
        "max": round(max(vals), 3) if vals else None,
        "discriminating": rel >= 0.999,
    }
    if value is None:
        return None, diag
    pct = percentile(value, vals)
    if not higher_is_better:
        pct = 100.0 - pct
    return round(50.0 + (pct - 50.0) * rel, 1), diag


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------

def build_index(
    observations: Mapping[str, Any],
    weights: Mapping[str, float] | None = None,
    basket_coverage: float = 1.0,
) -> dict[str, Any]:
    """把观测集算成"同级别城市内的麦当劳指数"。

    observations: {城市名: 该城观测}，结构见 tools/collect.py 产物。
    """
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)
    total_w = sum(w.values()) or 1.0
    w = {k: v / total_w for k, v in w.items()}

    names = list(observations)

    # --- 原始量 ---
    density = {c: float(observations[c].get("sampledStoreCount") or 0) for c in names}
    reach = {c: _core_reach(observations[c]) for c in names}
    dt_share = {}
    for c in names:
        obs_c = observations[c]
        num = float(obs_c.get("driveThruCount") or 0)
        # 门店采样（beType=1）与得来速采样（beType=5）是**两轮独立调用**，用的是同一套
        # 锚点、同一个"最近 5 家"上限。实测二者交集很小（得来速多在城市外围，最近 5 家
        # 多在市中心），所以「得来速数 ÷ 门店采样数」的分子并不是分母的子集。
        # 分母改用两轮的并集：同一套锚点下"能触达的全部门店"。
        base = obs_c.get("driveThruBase") or obs_c.get("sampledStoreCount") or 0
        dt_share[c] = (num / float(base) * 100) if base else None

    # --- 分项打分 + 诊断 ---
    dens_score = {}
    reach_score = {}
    dt_score = {}
    diag: dict[str, Any] = {}

    d_pop = list(density.values())
    for c in names:
        dens_score[c], diag["density"] = score_dimension(density[c], d_pop, True)

    r_pop = [v for v in reach.values() if v is not None]
    for c in names:
        reach_score[c], diag["reach"] = score_dimension(reach[c], r_pop, higher_is_better=False)

    dt_pop = [v for v in dt_share.values() if v is not None]
    for c in names:
        dt_score[c], diag["driveThru"] = score_dimension(dt_share[c], dt_pop, True)

    # --- 读数（展示用，不计分）---
    price_maps = {c: city_price_map(observations[c]) for c in names}
    basket = common_basket(price_maps, coverage=basket_coverage)
    if not basket and basket_coverage > 0.5:
        # 城市一多，"全组都有报价"的 SKU 会归零 —— 全国 300 个城市里没有任何一个
        # SKU 是通用的。价格只是展示用读数，放宽到 50% 覆盖率仍能得到可比的篮子。
        basket = common_basket(price_maps, coverage=0.5)
    price_raw = {c: basket_total(price_maps[c], basket) for c in names}
    readings = {c: city_readings(observations[c]) for c in names}
    price_diag = {
        "spread": relative_spread([v for v in price_raw.values() if v is not None]),
        "min": min((v for v in price_raw.values() if v is not None), default=None),
        "max": max((v for v in price_raw.values() if v is not None), default=None),
    }
    hours_diag = {
        "spread": relative_spread([r["hoursMedian"] for r in readings.values() if r["hoursMedian"]]),
        "min": min((r["hoursMedian"] for r in readings.values() if r["hoursMedian"]), default=None),
        "max": max((r["hoursMedian"] for r in readings.values() if r["hoursMedian"]), default=None),
    }

    # --- 合成 ---
    cities: dict[str, Any] = {}
    for c in names:
        sub = {"density": dens_score[c], "reach": reach_score[c], "driveThru": dt_score[c]}
        avail = {k: v for k, v in sub.items() if v is not None}
        if avail:
            wsum = sum(w[k] for k in avail)
            score = round(sum(sub[k] * w[k] for k in avail) / wsum, 1)
        else:
            score = 50.0
        cities[c] = {
            "city": c,
            "score": score,
            "subScores": sub,
            "raw": {
                **readings[c],
                "coreReach5": reach[c],
                "driveThruShare": round(dt_share[c], 1) if dt_share[c] is not None else None,
                "basketTotal": price_raw[c],
                "pricedSku": len(price_maps[c]),
            },
        }

    # --- 组内排名（这就是"同级别比较"）---
    ranked = sorted(cities.values(), key=lambda r: (-r["score"], r["city"]))
    for i, row in enumerate(ranked, 1):
        cities[row["city"]]["rank"] = i
        cities[row["city"]]["rankOf"] = len(ranked)

    return {
        "cities": cities,
        "order": [r["city"] for r in ranked],
        "basket": basket,
        "basketSize": len(basket),
        "weights": w,
        "scoredDimensions": list(DEFAULT_WEIGHTS),
        "diagnostics": diag,
        "readingsDiagnostics": {"price": price_diag, "hours": hours_diag},
        "meta": {
            "groupSize": len(names),
            "definition": "组内相对指数：分项一律取同级别城市内的百分位，不推断门店绝对总数",
            "minSpread": MIN_SPREAD,
        },
    }


# --------------------------------------------------------------------------
# 地址探针：把「某个点位第 5 家门店的距离」放进参照分布
# --------------------------------------------------------------------------

def anchor_reference(observations: Mapping[str, Any]) -> list[float]:
    """汇总所有城市、所有关键词锚点的第 5 家距离，作为地址模式的参照分布。

    `tools/collect.py` 在门店采样时顺带记录每个关键词锚点的 d5（额外调用为 0）。
    全国 337 城共约 1.8 万次探针，其中查到 ≥3 家门店的约 5,800 个进入参照分布。
    """
    out: list[float] = []
    for city_obs in observations.values():
        for probe in (city_obs.get("anchorProbes") or {}).values():
            d5 = (probe or {}).get("d5")
            if isinstance(d5, (int, float)) and d5 > 0:
                out.append(float(d5))
    return sorted(out)


def resolve_city(query: str, cities: Iterable[str]) -> str | None:
    """把用户输入的城市名归一化到观测集的键。

    依次尝试：完全一致 → 补/去「市」 → 唯一子串匹配。
    匹配到多个时返回 None（宁可让调用方去问，也不要瞎猜）。
    """
    if not query:
        return None
    q = query.strip()
    names = list(cities)
    if q in names:
        return q
    for cand in (q + "市", q.rstrip("市")):
        if cand in names:
            return cand
    hits = [n for n in names if q in n or n.rstrip("市") in q]
    return hits[0] if len(hits) == 1 else None


def anchor_reading(
    d5: float | None,
    reference: Sequence[float],
    found: int | None = None,
) -> dict[str, Any]:
    """给一个点位的「5 家门店半径」，返回它相对参照分布的水位。

    d5 越小说明该点位周边门店越密。返回的 percentile 越大 = 越密。
    `found` 是该锚点实际查到的门店数：不足 5 家本身就是"稀疏"的证据，
    此时不给分位（样本不够），但要说清楚它到底有多稀疏。
    """
    ref = [v for v in reference if v and v > 0]
    base = {
        "d5": None if d5 is None else round(float(d5), 1),
        "percentile": None,
        "referenceSize": len(ref),
        "median": round(median(ref), 1) if ref else None,
        "found": found,
    }

    # 门店不足 5 家：锚点周边的门店密度低于探针能力下限，属于稀疏区
    if found is not None and found < 5:
        if found == 0:
            return {**base, "percentile": 0.0,
                    "verdict": "该锚点附近未查到门店（可能过偏，或关键词没被解析成有效位置）"}
        return {**base, "verdict":
                f"该锚点范围内只查到 {found} 家门店（不足 5 家），属于门店稀疏区"}

    if d5 is None or d5 <= 0 or not ref:
        return {**base, "verdict": "数据不足，无法给出水位"}

    denser = sum(1 for v in ref if v > d5)   # 比它远的锚点有多少个
    pct = round(denser / len(ref) * 100, 1)
    if pct >= 75:
        verdict = "明显高于同组中位，属于门店密集区"
    elif pct >= 55:
        verdict = "略高于同组中位"
    elif pct >= 45:
        verdict = "与同组中位相当"
    elif pct >= 25:
        verdict = "略低于同组中位"
    else:
        verdict = "明显低于同组中位，属于门店稀疏区"
    return {**base, "percentile": pct, "verdict": verdict}


def explain(result: Mapping[str, Any], city: str) -> str:
    """给单城生成一句可读结论。"""
    row = (result.get("cities") or {}).get(city)
    if not row:
        return f"暂无 {city} 的观测数据。"
    r = row["raw"]
    label = {"density": "门店密度", "reach": "核心区覆盖", "driveThru": "得来速"}
    top = sorted(((k, v) for k, v in row["subScores"].items() if v is not None),
                 key=lambda kv: -kv[1])
    strong = "、".join(label[k] for k, _ in top[:1])
    weak = "、".join(label[k] for k, _ in top[-1:])
    parts = [f"{city} MCDIndex {row['score']}，同组 {row['rankOf']} 城中第 {row['rank']} 名"]
    parts.append(f"采样门店 {r['sampledStores']} 家（同构采样下界）")
    if r["driveThruCount"]:
        parts.append(f"得来速 {r['driveThruCount']} 家（占 {r['driveThruShare']}%）")
    if r["coreReach5"]:
        parts.append(f"市中心 {round(r['coreReach5'])} 米内已有 5 家")
    size = result.get("basketSize")
    if r["basketTotal"] is not None and size:
        hit = r.get("pricedSku") or 0
        parts.append(f"{size} 项共有 SKU 篮子挂牌价合计 ¥{r['basketTotal']}"
                     f"（本城在售 {hit} 项）")
    return "，".join(parts) + f"。优势在{strong}，短板在{weak}。"
