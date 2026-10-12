#!/usr/bin/env python3
"""MCDIndex 观测采集器（开发期工具）。

观测**直接落 SQLite**（`tools/out/mcdindex.db`），全部跑完再导出
`data/observations.json` 作为随仓库分发的快照。相比"每 N 条重写整份 JSON"：

  · **中断可续采** —— 每个 (城市, 关键词, 轮次) 探针的成败都落库；
    重跑时 `--resume` 只补缺失与瞬时失败的那些，已采的绝不重打。
  · **错误可归因** —— 每次失败的错误码都留着，一条 SQL 就能看分布
    （`SELECT error_code, COUNT(*) FROM probe WHERE status='error' GROUP BY 1`）。
  · **分析不必重算** —— 聚合交给 SQLite；`src/mcdindex.py` 仍只吃内存字典，
    交付物零 I/O 的约定不变。

采集三路可同构对比的原始观测：

  1. 门店供给 | query-nearby-stores (beType=1) 同构关键词采样
  2. 可得性   | 同一次采样顺带记录营业起止时间 / 是否可预约；
               另跑一轮 beType=5 标记得来速门店。
  3. 价格水平 | query-meals (orderType=1) —— 返回整店菜单含
               name / originalPrice / currentPrice。

关键口径（踩过的坑，别改）：
  - query-nearby-stores 单次固定上限 5 家、无分页 → 结果是**采样下界**，不是门店总数。
    只有"同构"才让城市间可比。
  - 价格必须用 **originalPrice（挂牌价）**。currentPrice 含限时活动价/早餐卡专享价，
    跨城抖动大、且常与 withOrder 绑定的卡券强相关，不可比。
  - 菜单**随时段变化**（早餐/正餐菜单不同）→ 同一批次内跑完。

用法:
    python tools/collect.py --national --resume --workers 30   # 全国（可反复续跑）
    python tools/collect.py                                    # 内置江苏 13 市
    python tools/collect.py 南京市 苏州市                        # 指定城市
    python tools/collect.py --national --limit 5                # 冒烟
    python tools/collect.py --status                            # 只看库里现有多少
    python tools/collect.py --import-partial tools/out/collect.partial.json \\
                            --done-tasks 9648                   # 抢救中断那轮的数据
"""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402
from cities import load_national_cities  # noqa: E402
from mcd_client import McdClient  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "observations.json")
PARTIAL = os.path.join(ROOT, "tools", "out", "collect.partial.json")

# 每城最多取多少个区县做关键词。
# 上限的作用是**均衡采样预算**：直辖市/大市有 20~38 个区县，
# 若全用上，其关键词数会是小城市的 1.5~2 倍，等于给大市更多采样预算。
MAX_DISTRICTS = 10

DISTRICTS: dict[str, list[str]] = {
    "南京市": "玄武区 秦淮区 建邺区 鼓楼区 浦口区 栖霞区 雨花台区 江宁区 六合区 溧水区 高淳区".split(),
    "无锡市": "锡山区 惠山区 滨湖区 梁溪区 新吴区 江阴市 宜兴市".split(),
    "徐州市": "云龙区 鼓楼区 贾汪区 泉山区 铜山区 丰县 沛县 睢宁县 新沂市 邳州市".split(),
    "常州市": "天宁区 钟楼区 新北区 武进区 金坛区 溧阳市".split(),
    "苏州市": "虎丘区 吴中区 相城区 姑苏区 吴江区 昆山市 常熟市 张家港市 太仓市".split(),
    "南通市": "崇川区 通州区 海门区 启东市 如皋市 海安市 如东县".split(),
    "连云港市": "连云区 海州区 赣榆区 东海县 灌云县 灌南县".split(),
    "淮安市": "清江浦区 淮安区 淮阴区 洪泽区 涟水县 盱眙县 金湖县".split(),
    "盐城市": "亭湖区 盐都区 大丰区 响水县 滨海县 阜宁县 射阳县 建湖县 东台市".split(),
    "扬州市": "广陵区 邗江区 江都区 宝应县 仪征市 高邮市".split(),
    "镇江市": "京口区 润州区 丹徒区 丹阳市 扬中市 句容市".split(),
    "泰州市": "海陵区 高港区 姜堰区 兴化市 靖江市 泰兴市".split(),
    "宿迁市": "宿城区 宿豫区 沭阳县 泗阳县 泗洪县".split(),
}

COMMON_ROADS = "人民路 解放路 中山路 建设路 文化路 新华路 长江路 北京路 光明路 团结路 胜利路 青年路 和平路 友谊路 幸福路 迎宾大道 世纪大道".split()
COMMON_POI = "万达 吾悦 广场 大厦 中心 百货 商场 购物中心 超市 医院 学校 大学 公园 车站 火车站 高铁站 大学城 地铁 步行街 商业街 写字楼 酒店 开发区 工业园 老城区 新城区 高新区".split()

PRICE_SAMPLES_PER_CITY = 3
FLUSH_EVERY = 400          # 攒多少条探针结果提交一次事务


def keywords_for(city: str, districts: list[str] | None = None) -> list[str]:
    ds = districts if districts is not None else DISTRICTS.get(city, [])
    return [city, "麦当劳", *ds[:MAX_DISTRICTS], *COMMON_ROADS, *COMMON_POI]


def build_spec(argv: list[str]) -> tuple[dict[str, list[str]], str]:
    """决定要采哪些城市、每城用哪些区县做关键词，并给出 scope 名。"""
    if "--national" in argv:
        spec = load_national_cities()
        scope = "全国地级及以上行政区"
    elif [a for a in argv if a in DISTRICTS]:
        spec = {c: DISTRICTS[c] for c in argv if c in DISTRICTS}
        scope = "江苏 13 市"
    else:
        spec = dict(DISTRICTS)
        scope = "江苏 13 市"

    if "--limit" in argv:      # 冒烟测试 / 限速探测用
        n = int(argv[argv.index("--limit") + 1])
        spec = dict(list(spec.items())[:n])
        scope += f"（前 {n} 城）"
    return spec, scope


def classify(data, err_code) -> str:
    """把一次探针的结果归成 ok / empty / error 三态。

    注意 600047「当前位置附近没有找到合适的餐厅」与 600014「没有查询到该地址」
    不是"调用失败"，而是"这个锚点周边采不到门店"—— 本质是稀疏观测，记 empty，
    这样它既不会被续采反复重打，也不会在错误统计里污染真实故障率。
    """
    if data is None:
        return "empty" if err_code in db.SPARSE_CODES else "error"
    if isinstance(data, list):
        return "ok" if data else "empty"
    if isinstance(data, dict):
        for k in ("list", "stores", "meals"):
            if isinstance(data.get(k), (list, dict)):
                return "ok" if data[k] else "empty"
    return "empty"


def store_hits(arr: list) -> tuple[int, float | None, str | None]:
    """从门店数组里取 (门店数, 第5家距离, 命中明细 JSON)。"""
    if not arr:
        return 0, None, None
    dists = [x.get("distance") for x in arr if isinstance(x.get("distance"), (int, float))]
    d5 = max(dists) if dists else None
    hits = json.dumps([{"code": x.get("storeCode"), "name": x.get("storeName"),
                        "distance": x.get("distance")} for x in arr], ensure_ascii=False)
    return len(arr), d5, hits


def print_status(conn) -> None:
    st = db.stats(conn)
    print(json.dumps(st, ensure_ascii=False, indent=2))
    run = conn.execute("SELECT run_id, started_at, finished_at, scope, calls, errors, note"
                       " FROM run ORDER BY run_id DESC LIMIT 5").fetchall()
    if run:
        print("\n最近运行:")
        for r in run:
            print(f"  #{r['run_id']} {r['started_at']} → {r['finished_at'] or '(未结束)'}"
                  f"  {r['scope']}  calls={r['calls']} errors={r['errors']}")
            if r["note"]:
                print(f"      note: {r['note'][:160]}")


def main() -> None:
    argv = sys.argv[1:]
    workers = int(argv[argv.index("--workers") + 1]) if "--workers" in argv else 6
    do_prices = "--no-prices" not in argv
    do_drivethru = "--no-drivethru" not in argv
    resume = "--resume" in argv or "--import-partial" in argv
    retry_all = "--retry-all" in argv
    export = "--no-export" not in argv

    conn = db.connect()

    if "--status" in argv:
        print_status(conn)
        return

    spec, scope = build_spec(argv)
    targets = list(spec)

    note = ""
    if "--import-partial" in argv:
        path = argv[argv.index("--import-partial") + 1]
        done = int(argv[argv.index("--done-tasks") + 1]) if "--done-tasks" in argv else 0
        # 关键词清单与"任务跑到哪"的换算都归调用方，db.py 不重复这套口径
        keywords = {c: keywords_for(c, spec.get(c)) for c in targets}
        acc, complete = 0, set()
        for c in targets:
            acc += len(keywords[c])
            if acc <= done:
                complete.add(c)
        run_id = db.start_run(conn, scope + "（续采）", len(targets),
                              f"继承中断轮 partial，已完成关键词 {done} 个，"
                              f"判定跑完 {len(complete)} 城")
        info = db.import_partial(conn, path, run_id, keywords, complete)
        note = (f"[已导入 partial] 门店 {info['stores']} / 探针 {info['probes']} / "
                f"价格 {info['prices']} / 判定已跑完 {info['completeCities']} 城")
        print(note, flush=True)
        stat = db.stats(conn)
        print(f"库内现有：门店 {stat['stores']}（{stat['cities_with_stores']} 城）、"
              f"门店探针 {stat['probes_store']}、锚点 {stat['anchor_probes']}\n", flush=True)
        if "--import-only" in argv:
            skip = db.pending_keys(conn, db.PASS_STORE, retry_permanent=retry_all)
            pending = sum(1 for c in targets for kw in keywords[c]
                          if (c, kw) not in skip)
            print(f"[仅导入] 门店轮待补探针 {pending} 个，未发起任何 MCP 调用。")
            return
    else:
        run_id = db.start_run(conn, scope, len(targets), note)

    client = McdClient()
    print(f"目标城市 {len(targets)} 个，并发 {workers}"
          f"{'，续采模式' if resume else ''}\n", flush=True)

    # ---------------- 1. 门店供给采样（beType=1） ----------------
    tasks = [(c, kw) for c in targets for kw in keywords_for(c, spec.get(c))]
    if resume:
        skip = db.pending_keys(conn, db.PASS_STORE, retry_permanent=retry_all)
        before = len(tasks)
        tasks = [t for t in tasks if (t[0], t[1]) not in skip]
        print(f"[续采] 已完成 {before - len(tasks)} 个关键词探针，"
              f"本次只补 {len(tasks)} 个\n", flush=True)

    print(f"[1/3] 门店供给采样：{len(tasks)} 次查询 …", flush=True)
    probes, srows, drows = [], [], []
    done = 0

    def job1(t):
        city, kw = t
        data, code, msg = client.call_ex("query-nearby-stores", {
            "beType": db.PASS_STORE, "searchType": 2, "city": city, "keyword": kw})
        return city, kw, data, code, msg

    def flush_all():
        db.flush(conn, probes, srows, drows)
        probes.clear(); srows.clear(); drows.clear()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for city, kw, data, code, errmsg in pool.map(job1, tasks):
            done += 1
            status = classify(data, code)
            arr = data if isinstance(data, list) else []
            found, d5, hits = store_hits(arr) if status == "ok" else (0, None, None)
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            probes.append(db.probe_row(city, kw, db.PASS_STORE, status, code, errmsg,
                                       found, d5, hits if kw == city else None, run_id, ts))
            srows.extend(db.store_rows(city, arr, run_id))
            if len(probes) >= FLUSH_EVERY:
                flush_all()
                if done % 2000 < FLUSH_EVERY:
                    n = conn.execute("SELECT COUNT(*) FROM store").fetchone()[0]
                    print(f"      {done}/{len(tasks)}  门店 {n}  调用 {client.calls}"
                          f"  错误 {len(client.errors)}", flush=True)
    flush_all()
    n_store = conn.execute("SELECT COUNT(*) FROM store").fetchone()[0]
    print(f"      完成 {done}/{len(tasks)}  门店 {n_store}  调用 {client.calls}"
          f"  错误 {len(client.errors)}", flush=True)

    # ---------------- 2. 得来速采样（beType=5） ----------------
    if do_drivethru:
        # 必须与门店采样用**同一套关键词**，否则"得来速占比"的分子分母口径不一致
        dtasks = [(c, kw) for c in targets for kw in keywords_for(c, spec.get(c))]
        if resume:
            skip = db.pending_keys(conn, db.PASS_DRIVE, retry_permanent=retry_all)
            dtasks = [t for t in dtasks if (t[0], t[1]) not in skip]
        print(f"\n[2/3] 得来速采样：{len(dtasks)} 次查询 …", flush=True)
        d2 = 0

        def job5(t):
            city, kw = t
            data, code, msg = client.call_ex("query-nearby-stores", {
                "beType": db.PASS_DRIVE, "searchType": 2, "city": city, "keyword": kw})
            return city, kw, data, code, msg

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for city, kw, data, code, errmsg in pool.map(job5, dtasks):
                d2 += 1
                status = classify(data, code)
                arr = data if isinstance(data, list) else []
                found, d5, hits = store_hits(arr) if status == "ok" else (0, None, None)
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                probes.append(db.probe_row(city, kw, db.PASS_DRIVE, status, code, errmsg,
                                           found, d5, None, run_id, ts))
                drows.extend(db.dt_rows(city, [s.get("storeCode") for s in arr], run_id))
                if len(probes) >= FLUSH_EVERY:
                    flush_all()
                    if d2 % 2000 < FLUSH_EVERY:
                        n = conn.execute("SELECT COUNT(*) FROM drive_thru").fetchone()[0]
                        print(f"      {d2}/{len(dtasks)}  得来速 {n}  调用 {client.calls}"
                              f"  错误 {len(client.errors)}", flush=True)
        flush_all()
        n_dt = conn.execute("SELECT COUNT(*) FROM drive_thru").fetchone()[0]
        print(f"      完成 {d2}/{len(dtasks)}  得来速 {n_dt}", flush=True)

    # ---------------- 3. 价格采样 ----------------
    if do_prices:
        rows = conn.execute(
            "SELECT city, store_code, name FROM store WHERE city IN (%s)"
            % ",".join("?" * len(targets)), targets).fetchall()
        by_city: dict[str, list] = {}
        for r in rows:
            by_city.setdefault(r["city"], []).append((r["store_code"], r["name"]))

        ptasks = []
        for c in targets:
            codes = sorted(by_city.get(c, []))
            if not codes:
                continue
            idx = sorted({0, len(codes) // 2, len(codes) - 1})
            ptasks.extend((c, *codes[i]) for i in idx[:PRICE_SAMPLES_PER_CITY])

        have = {r["store_code"] for r in conn.execute("SELECT store_code FROM price_sample")}
        if resume:
            ptasks = [(c, sc, nm) for c, sc, nm in ptasks if sc not in have]

        print(f"\n[3/3] 价格采样：{len(ptasks)} 家门店 …", flush=True)

        def jobp(t):
            city, sc, nm = t
            data, code, msg = client.call_ex("query-meals", {
                "storeCode": sc, "orderType": 1, "beType": 1})
            return city, sc, nm, data, code

        n_price = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for city, sc, nm, data, code in pool.map(jobp, ptasks):
                meals = (data or {}).get("meals") if isinstance(data, dict) else None
                if not isinstance(meals, dict) or not meals:
                    continue
                items = {}
                for mc, rec in meals.items():
                    if not isinstance(rec, dict):
                        continue
                    items[mc] = {"name": rec.get("name"),
                                 "original": rec.get("originalPrice"),
                                 "current": rec.get("currentPrice")}
                if items:
                    db.upsert_prices(conn, run_id, city, sc, nm, items)
                    n_price += 1
        print(f"      完成 {n_price} 家门店的菜单", flush=True)

    db.finish_run(conn, run_id, client.calls, len(client.errors))

    # ---------------- 收尾 ----------------
    print("\n" + "=" * 72)
    st = db.stats(conn)
    print(f"库内合计：门店 {st['stores']}（{st['cities_with_stores']} 城）"
          f" 得来速 {st['drive_thru']} 锚点 {st['anchor_probes']}"
          f" 价格样本 {st['price_samples']}（{st['price_items']} 个 SKU）")
    if client.error_codes:
        print("\n本次错误码分布：")
        for code, n in sorted(client.error_codes.items(), key=lambda kv: -kv[1])[:12]:
            print(f"  {code:>10}  {n:>6}")
    bd = db.error_breakdown(conn)
    if bd:
        print("\n库内探针错误码累计：")
        for r in bd:
            print(f"  {r['error_code'] or '(无码)':>10}  {r['n']:>6}")
    print("=" * 72)

    if export:
        m = db.export_json(conn, OUT, scope)
        print(f"[已导出] {OUT}  城市 {m['cities']} / 调用 {m['mcpCalls']}")


if __name__ == "__main__":
    main()
