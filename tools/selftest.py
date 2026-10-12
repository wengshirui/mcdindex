#!/usr/bin/env python3
"""src/ 纯函数自测。

三态输出：通过 / 失败 / 跳过。**"跳过"不算通过** —— 缺数据就是没有结论。
默认只跑不依赖观测数据的用例；加 --with-data 才跑需要 observations.json 的用例。

用法:
    python tools/selftest.py
    python tools/selftest.py --with-data
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import mcdindex as M  # noqa: E402

OBS = os.path.join(ROOT, "data", "observations.json")

PASS, FAIL, SKIP = [], [], []


def chk(name: str, got, want) -> None:
    (PASS if got == want else FAIL).append((name, got, want))


def near(name: str, got, want, tol=0.05) -> None:
    if got is None:
        FAIL.append((name, got, want))
    elif abs(got - want) <= tol:
        PASS.append((name, got, want))
    else:
        FAIL.append((name, got, want))


def run_pure() -> None:
    # 时间解析
    chk("parse_hhmm 正常", M.parse_hhmm("07:30"), 450)
    chk("parse_hhmm 零点", M.parse_hhmm("00:00"), 0)
    chk("parse_hhmm 非法字符串", M.parse_hhmm("7点半"), None)
    chk("parse_hhmm 非法时区", M.parse_hhmm("25:00"), None)
    chk("parse_hhmm 非字符串", M.parse_hhmm(730), None)

    # 营业时长（含跨零点）
    chk("open_minutes 同日", M.open_minutes("07:00", "22:00"), 900)
    chk("open_minutes 跨零点", M.open_minutes("06:00", "02:00"), 1200)
    chk("open_minutes 24 小时", M.open_minutes("00:00", "00:00"), 1440)
    chk("open_minutes 缺字段", M.open_minutes(None, "22:00"), None)

    # 百分位
    chk("percentile 最小值", M.percentile(1, [1, 2, 3]), 0.0)
    chk("percentile 最大值", M.percentile(3, [1, 2, 3]), 100.0)
    chk("percentile 并列取平均秩", M.percentile(2, [1, 2, 2, 4]), 50.0)
    chk("percentile 单元素退化为中性", M.percentile(5, [5]), 50.0)
    chk("percentile 空总体退化为中性", M.percentile(5, []), 50.0)

    # 区分度与收缩
    near("relative_spread 基本", M.relative_spread([10, 20, 30]), 1.0)
    chk("relative_spread 常量集为 0", M.relative_spread([7, 7, 7]), 0.0)
    chk("relative_spread 单元素为 0", M.relative_spread([7]), 0.0)
    # 中位数为 0 的偏态分布（得来速占比就是）不能算成 0，否则会被误判无区分度
    chk("relative_spread 中位数为 0 时用最大值做分母",
        M.relative_spread([0, 0, 0, 0, 47]), 1.0)
    near("reliability 区分度不足收缩", M.reliability(0.016, 0.30), 0.0533, 0.01)
    chk("reliability 达标封顶 1", M.reliability(3.0, 0.30), 1.0)
    chk("reliability 零阈值为 1", M.reliability(0.001, 0.0), 1.0)

    # 无区分度时必须塌缩到中性，否则就是假精度
    score, diag = M.score_dimension(10, [10, 10, 10])
    chk("score_dimension 无区分度→50", score, 50.0)
    chk("score_dimension 标记未达标", diag["discriminating"], False)
    score2, _ = M.score_dimension(100, [1, 10, 100])
    chk("score_dimension 有区分度→最大值 100", score2, 100.0)
    score3, _ = M.score_dimension(10, [1, 10, 100])
    chk("score_dimension 中位值→50", score3, 50.0)
    score4, _ = M.score_dimension(1, [1, 10, 100], higher_is_better=False)
    chk("score_dimension 反向打分（越小越好）", score4, 100.0)

    # 篮子
    chk("common_basket 全覆盖取交集",
        M.common_basket({"a": {"x": 1, "y": 1}, "b": {"x": 1}}, 1.0), ["x"])
    chk("common_basket 50% 阈值", len(M.common_basket({"a": {"x": 1, "y": 1}, "b": {"x": 1}}, 0.5)), 2)
    chk("basket_total 求和", M.basket_total({"a": 1.5, "b": 2.5}, ["a", "b"]), 4.0)
    chk("basket_total 缺项过多判不可比", M.basket_total({"a": 1}, ["a", "b", "c", "d", "e"]), None)
    chk("basket_total 空篮子", M.basket_total({"a": 1}, []), None)

    # 价格表
    pm = M.city_price_map({"priceSamples": [
        {"items": {"X": {"original": "10", "current": "8"}}},
        {"items": {"X": {"original": "12", "current": "9"}}},
    ]})
    near("city_price_map 取中位数且用挂牌价", pm["X"], 11.0)
    pm2 = M.city_price_map({"priceSamples": [{"items": {"X": {"original": None, "current": "9"}}}]})
    chk("city_price_map 无挂牌价则丢弃", pm2, {})


    # 城市名归一化
    CITIES = ["南京市", "苏州市", "无锡市", "连云港市"]
    chk("resolve_city 完全一致", M.resolve_city("苏州市", CITIES), "苏州市")
    chk("resolve_city 补市字", M.resolve_city("苏州", CITIES), "苏州市")
    chk("resolve_city 去市字", M.resolve_city("南京市", CITIES), "南京市")
    chk("resolve_city 唯一子串", M.resolve_city("连云港", CITIES), "连云港市")
    chk("resolve_city 空输入", M.resolve_city("", CITIES), None)
    chk("resolve_city 无匹配", M.resolve_city("杭州市", CITIES), None)
    chk("resolve_city 歧义不瞎猜", M.resolve_city("市", CITIES), None)

    # 地址锚点水位
    REF = [500.0, 1000.0, 2000.0, 4000.0, 8000.0]
    chk("anchor_reference 汇总", M.anchor_reference(
        {"a": {"anchorProbes": {"x": {"d5": 100}, "y": {"d5": 300}}}}), [100.0, 300.0])
    chk("anchor_reference 丢弃非正值", M.anchor_reference(
        {"a": {"anchorProbes": {"x": {"d5": 0}, "y": {"d5": None}}}}), [])
    r1 = M.anchor_reading(400.0, REF)
    chk("anchor_reading 比全部参照都近→100 分位", r1["percentile"], 100.0)
    r1b = M.anchor_reading(500.0, REF)
    chk("anchor_reading 等于参照最小值→80 分位", r1b["percentile"], 80.0)
    r2 = M.anchor_reading(8000.0, REF)
    chk("anchor_reading 最远→0 分位", r2["percentile"], 0.0)
    r3 = M.anchor_reading(None, REF)
    chk("anchor_reading 无 d5 不给分位", r3["percentile"], None)
    r4 = M.anchor_reading(None, REF, found=1)
    chk("anchor_reading 不足 5 家判稀疏", r4["percentile"], None)
    chk("anchor_reading 稀疏措辞含家数", "1 家" in r4["verdict"], True)
    r5 = M.anchor_reading(None, REF, found=0)
    chk("anchor_reading 零家不算 0 分位", r5["percentile"], 0.0)
    chk("anchor_reading 空参照不崩", M.anchor_reading(500.0, [])["percentile"], None)


def _mini(stores: int, dt: int, base: int | None = None, d5: float = 1000.0) -> dict:
    """造一份最小可用的单城观测，用来隔离测试某个口径。

    核心锚点给 3 个命中 —— `_core_reach` 需要至少 3 个距离才算得出来，
    只给 1 个会让 reach 恒为 None，测试就测了个寂寞。
    """
    o = {
        "sampledStoreCount": stores, "driveThruCount": dt,
        "coreProbe": {"keyword": "x", "hits": [{"distance": d5 / 3}] * 2 + [{"distance": d5}]},
        "stores": {}, "driveThruCodes": [], "priceSamples": [],
    }
    if base is not None:
        o["driveThruBase"] = base
    return o


def run_metrics() -> None:
    """口径用例：这些数字是被"想当然"坑过的，不测就会悄悄错。"""
    # 得来速占比的分母 = beType=1 与 beType=5 两轮的并集，不是单轮门店采样数。
    # 甲市：4 家得来速 / 并集 12 家 = 33.3%；若错用门店数 10 家会得到 40%。
    r = M.build_index({"甲市": _mini(10, 4, 12), "乙市": _mini(10, 2, 10)})
    near("得来速占比以两轮并集为分母", r["cities"]["甲市"]["raw"]["driveThruShare"], 33.3, 0.1)
    chk("并集口径穿透到读数", r["cities"]["甲市"]["raw"]["driveThruBase"], 12)
    # 老数据没有 driveThruBase，必须优雅退化到门店采样数（向后兼容）
    r2 = M.build_index({"丙市": _mini(10, 5)})
    near("缺 driveThruBase 时退化到门店数", r2["cities"]["丙市"]["raw"]["driveThruShare"], 50.0, 0.1)

    # 护栏不变式：任何维度的相对极差低于门槛时，全组该分项必须**恰好**是 50
    for obs, tag in (({"甲市": _mini(10, 4, 12), "乙市": _mini(10, 4, 12)}, "常量集"),
                     ({"甲市": _mini(10, 4, 12), "乙市": _mini(10, 2, 10)}, "有区分度")):
        rr = M.build_index(obs)
        for k, d in rr["diagnostics"].items():
            if d["spread"] < M.MIN_SPREAD:
                vals = {row["subScores"][k] for row in rr["cities"].values()}
                chk(f"{tag}·{k} 无区分度时全组塌缩到 50", vals <= {50.0}, True)


def run_data() -> None:
    if not os.path.exists(OBS):
        SKIP.append(("观测数据用例（缺 observations.json）", None, None))
        return

    raw = json.load(open(OBS, encoding="utf-8"))
    obs = raw["cities"]
    n = len(obs)
    r = M.build_index(obs)

    # 这些断言必须**与数据范围无关** —— 江苏 13 城或全国 300+ 城都应成立，
    # 否则换一批数据就全红，测的就不是逻辑而是"当前恰好采到几个城"。
    chk("观测非空", n > 0, True)
    chk("所有城市都进了结果", len(r["cities"]), n)
    chk(f"排名连续 1..{n}", sorted(x["rank"] for x in r["cities"].values()), list(range(1, n + 1)))
    chk("排名与得分单调一致",
        [r["cities"][c]["score"] for c in r["order"]],
        sorted((x["score"] for x in r["cities"].values()), reverse=True))

    for c, row in r["cities"].items():
        for k, v in row["subScores"].items():
            if v is not None and not (0 <= v <= 100):
                FAIL.append((f"{c}.{k} 分数越界", v, "0~100"))
        # 每城的占比都不能超过 100%，且分子不能大于分母
        dtc, dtb = row["raw"]["driveThruCount"], row["raw"]["driveThruBase"]
        if dtb and dtc > dtb:
            FAIL.append((f"{c} 得来速数超过并集基数", (dtc, dtb), "dt <= base"))
    PASS.append(("所有分项落在 0~100、得来速分子不超分母", None, None))

    # 护栏的有效性：无区分度的维度必须全组恰好 50
    for k, d in r["diagnostics"].items():
        if d["spread"] < M.MIN_SPREAD:
            vals = {row["subScores"][k] for row in r["cities"].values()}
            chk(f"{k} 无区分度→全组 50", vals <= {50.0}, True)
    for k in r["scoredDimensions"]:
        chk(f"{k} 有区分度诊断字段", "discriminating" in r["diagnostics"][k], True)

    # 锚点参照分布必须够厚，否则地址模式的分位没有意义
    ref = M.anchor_reference(obs)
    chk("锚点参照分布样本充足(>100)", len(ref) > 100, True)
    chk("锚点参照分布无非法值", all(v > 0 for v in ref), True)

    # 抽样对照公开数据：无锡同构采样应接近公开值（55~61），不能高得离谱
    if "无锡市" in obs:
        wx = r["cities"]["无锡市"]["raw"]["sampledStores"]
        chk("无锡采样在公开值区间附近", 40 <= wx <= 90, True)


def main() -> None:
    run_pure()
    run_metrics()
    if "--with-data" in sys.argv:
        run_data()

    for n, g, w in PASS:
        print(f"  通过  {n}")
    for n, g, w in SKIP:
        print(f"  跳过  {n}")
    for n, g, w in FAIL:
        print(f"  失败  {n}   got={g!r} want={w!r}")
    print(f"\n  → {len(PASS)} 通过 / {len(FAIL)} 失败 / {len(SKIP)} 跳过")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
