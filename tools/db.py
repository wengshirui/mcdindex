#!/usr/bin/env python3
"""MCDIndex 本地数据仓（SQLite，开发期工具）。

为什么要它
--------------------------------------------------------------------------
采集一轮全国要 3 万多次 MCP 调用、约 20 分钟。原来的做法是每 600 次
把整份 JSON 重写一遍：中断即全废（无法续采），且每次分析都要把几十 MB
的 JSON 全读进内存再重算。两者都是纯浪费。

改成本地 SQLite 之后：
  · **可续采**：每个 (城市, 关键词, 轮次) 探针的成败都落库，重跑只补缺的。
  · **可查**：错误码分布、各城锚点解析率这类问题直接用 SQL 问，不用重采。
  · **分析提速**：聚合交给 SQLite，`src/mcdindex.py` 仍然只吃内存里的字典，
    保持"交付物零 I/O"的约定不变。

分工（重要）
--------------------------------------------------------------------------
  tools/out/mcdindex.db     工作库，**gitignore**，属开发期资产
  data/observations.json   由库导出的快照，**随仓库分发**，离线演示页依赖它

用法：
    import db
    conn = db.connect()
    run = db.start_run(conn, scope="全国地级及以上行政区")
    db.upsert_probe(conn, run, city, kw, 1, "ok", None, 5, 1234.0)
    ...
    db.to_observations(conn)     # -> {城市: 该城观测}，直接喂 mcdindex.build_index
    db.export_json(conn, "data/observations.json")
"""

from __future__ import annotations

import json
import os
import sqlite3
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "tools", "out", "mcdindex.db")
JSON_PATH = os.path.join(ROOT, "data", "observations.json")

# 可重试的瞬时错误：续采时应当重打
TRANSIENT_CODES = {"429", "500", "502", "503", "504", "508"}
# 语义性失败：重打也是白打。实测两类 ——
#   600047 当前位置附近没有找到合适的餐厅（锚点有解析结果，但周边无门店 → 稀疏信号）
#   600014 没有查询到该地址（锚点文本压根没解析出来）
#   600058 参数不合法（如 searchType=2 缺 city 或 keyword）
PERMANENT_HINT = {"600014", "600047", "600058"}
# 这几类"失败"其实等价于"该锚点采不到门店"，统计密度时按 0 家处理
SPARSE_CODES = {"600014", "600047"}

PASS_STORE = 1    # query-nearby-stores beType=1 门店供给
PASS_DRIVE = 5    # query-nearby-stores beType=5 得来速

SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
  run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  scope       TEXT,
  city_count  INTEGER,
  calls       INTEGER DEFAULT 0,
  errors      INTEGER DEFAULT 0,
  note        TEXT
);

-- 每一次探针调用，无论成败。既做续采依据，也做错误分析的事实表。
CREATE TABLE IF NOT EXISTS probe (
  city       TEXT NOT NULL,
  keyword    TEXT NOT NULL,
  pass       INTEGER NOT NULL,
  status     TEXT NOT NULL,          -- ok / empty / error / unknown(partial 导入，待重采)
  error_code TEXT,
  errmsg     TEXT,                   -- 错误原文，用于把错误码回译成人话
  found      INTEGER,                -- 返回门店数（0~5）
  d5         REAL,                   -- 该锚点第 5 家距离（米）
  hits       TEXT,                   -- 仅城市名锚点：命中明细 JSON [{code,name,distance}]
  run_id     INTEGER,
  called_at  TEXT,
  PRIMARY KEY (city, keyword, pass)
);

CREATE TABLE IF NOT EXISTS store (
  city       TEXT NOT NULL,
  store_code TEXT NOT NULL,
  name       TEXT,
  address    TEXT,
  open_at    TEXT,
  close_at   TEXT,
  open_now   TEXT,
  reservable TEXT,
  first_run  INTEGER,
  last_run   INTEGER,
  PRIMARY KEY (city, store_code)
);

CREATE TABLE IF NOT EXISTS drive_thru (
  city       TEXT NOT NULL,
  store_code TEXT NOT NULL,
  run_id     INTEGER,
  PRIMARY KEY (city, store_code)
);

CREATE TABLE IF NOT EXISTS price_sample (
  city       TEXT NOT NULL,
  store_code TEXT NOT NULL,
  store_name TEXT,
  run_id     INTEGER,
  PRIMARY KEY (store_code)
);

CREATE TABLE IF NOT EXISTS price_item (
  store_code TEXT NOT NULL,
  item_code  TEXT NOT NULL,
  name       TEXT,
  original   REAL,
  current    REAL,
  run_id     INTEGER,
  PRIMARY KEY (store_code, item_code)
);

CREATE INDEX IF NOT EXISTS ix_probe_city  ON probe(city, pass);
CREATE INDEX IF NOT EXISTS ix_probe_stat  ON probe(status, error_code);
CREATE INDEX IF NOT EXISTS ix_store_city  ON store(city);
CREATE INDEX IF NOT EXISTS ix_price_store ON price_item(store_code);
"""


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # WAL + NORMAL：批量写入下快很多，且崩溃不至于写坏库
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _migrate(conn)
    return conn


# 列名 -> 建列语句。CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，
# 所以新增字段必须在这里登记，否则老库一写就 "no column named ..."。
MIGRATIONS = {
    "probe": {"errmsg": "TEXT"},
    "store": {},
    "drive_thru": {},
}


def _migrate(conn) -> None:
    for table, cols in MIGRATIONS.items():
        if not cols:
            continue
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()


def start_run(conn, scope: str, city_count: int = None, note: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO run(started_at, scope, city_count, note) VALUES (?,?,?,?)",
        (time.strftime("%Y-%m-%d %H:%M:%S"), scope, city_count, note or None),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn, run_id: int, calls: int, errors: int) -> None:
    conn.execute("UPDATE run SET finished_at=?, calls=?, errors=? WHERE run_id=?",
                 (time.strftime("%Y-%m-%d %H:%M:%S"), calls, errors, run_id))
    conn.commit()


# --------------------------------------------------------------------------
# 写入
# --------------------------------------------------------------------------

def upsert_probes(conn, run_id: int, rows: list[tuple]) -> None:
    """rows: [(city, keyword, pass, status, error_code, found, d5, hits_json), ...]"""
    if not rows:
        return
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    conn.executemany(
        "INSERT OR REPLACE INTO probe"
        "(city,keyword,pass,status,error_code,found,d5,hits,run_id,called_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(*r, run_id, now) for r in rows],
    )
    conn.commit()


# --------------------------------------------------------------------------
# 批量写入：高并发采集下每次 commit 才是开销，所以攒够一批再提交
# --------------------------------------------------------------------------

_SQL_PROBE = ("INSERT OR REPLACE INTO probe"
              "(city,keyword,pass,status,error_code,errmsg,found,d5,hits,run_id,called_at)"
              " VALUES (?,?,?,?,?,?,?,?,?,?,?)")
_SQL_STORE = (
    "INSERT INTO store(city,store_code,name,address,open_at,close_at,open_now,"
    "reservable,first_run,last_run) VALUES (?,?,?,?,?,?,?,?,?,?)"
    " ON CONFLICT(city,store_code) DO UPDATE SET"
    "  name=COALESCE(excluded.name,name),"
    "  address=COALESCE(excluded.address,address),"
    "  open_at=COALESCE(excluded.open_at,open_at),"
    "  close_at=COALESCE(excluded.close_at,close_at),"
    "  open_now=excluded.open_now,"
    "  reservable=COALESCE(excluded.reservable,reservable),"
    "  last_run=excluded.last_run")
_SQL_DT = "INSERT OR IGNORE INTO drive_thru(city,store_code,run_id) VALUES (?,?,?)"


def probe_row(city, keyword, pass_, status, error_code, errmsg, found, d5, hits,
              run_id, ts):
    return (city, keyword, pass_, status, error_code, errmsg, found, d5, hits,
            run_id, ts)


def store_rows(city: str, result: list[dict], run_id: int) -> list[tuple]:
    """把 MCP 的 query-nearby-stores 返回直接转成 store 表行。"""
    return [(city, s.get("storeCode"), s.get("storeName"), s.get("address"),
             s.get("businessStartTime"), s.get("businessEndTime"),
             s.get("businessStatus"), s.get("reservation"), run_id, run_id)
            for s in result if s.get("storeCode")]


def store_rows_from_dict(city: str, stores: dict, run_id: int) -> list[tuple]:
    """把 partial JSON 里的 {code: {...}} 转成 store 表行（导入用）。"""
    return [(city, code, v.get("name"), v.get("address"), v.get("open"), v.get("close"),
             v.get("openNow"), v.get("reservable"), run_id, run_id)
            for code, v in stores.items() if code]


def dt_rows(city: str, codes, run_id: int) -> list[tuple]:
    return [(city, c, run_id) for c in codes if c]


def flush(conn, probes=(), stores=(), drive_thru=()) -> None:
    if probes:
        conn.executemany(_SQL_PROBE, probes)
    if stores:
        conn.executemany(_SQL_STORE, stores)
    if drive_thru:
        conn.executemany(_SQL_DT, drive_thru)
    conn.commit()


def upsert_stores(conn, run_id: int, city: str, stores: list[dict]) -> None:
    """stores: [{storeCode,name,address,open,close,openNow,reservable}, ...]"""
    rows = [(city, s.get("storeCode"), s.get("storeName") or s.get("name"),
             s.get("address"), s.get("businessStartTime") or s.get("open"),
             s.get("businessEndTime") or s.get("close"),
             s.get("businessStatus") or s.get("openNow"),
             s.get("reservation") or s.get("reservable"), run_id, run_id)
            for s in stores if s.get("storeCode")]
    if not rows:
        return
    conn.executemany(
        "INSERT INTO store(city,store_code,name,address,open_at,close_at,open_now,"
        "reservable,first_run,last_run) VALUES (?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(city,store_code) DO UPDATE SET"
        "  name=COALESCE(excluded.name,name),"
        "  address=COALESCE(excluded.address,address),"
        "  open_at=COALESCE(excluded.open_at,open_at),"
        "  close_at=COALESCE(excluded.close_at,close_at),"
        "  open_now=excluded.open_now,"
        "  reservable=COALESCE(excluded.reservable,reservable),"
        "  last_run=excluded.last_run",
        rows,
    )
    conn.commit()


def upsert_drive_thru(conn, run_id: int, city: str, codes: list[str]) -> None:
    if not codes:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO drive_thru(city,store_code,run_id) VALUES (?,?,?)",
        [(city, c, run_id) for c in codes if c],
    )
    conn.commit()


def upsert_prices(conn, run_id: int, city: str, store_code: str, store_name: str,
                 items: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO price_sample(city,store_code,store_name,run_id)"
        " VALUES (?,?,?,?)", (city, store_code, store_name, run_id))
    rows = []
    for code, rec in items.items():
        rec = rec or {}
        rows.append((store_code, code, rec.get("name"), rec.get("original"),
                     rec.get("current"), run_id))
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO price_item(store_code,item_code,name,original,"
            "current,run_id) VALUES (?,?,?,?,?,?)", rows)
    conn.commit()


# --------------------------------------------------------------------------
# 续采
# --------------------------------------------------------------------------

def pending_keys(conn, pass_: int, retry_transient: bool = True,
                 retry_permanent: bool = False) -> set[tuple[str, str]]:
    """返回**已经拿到终态、不必重打**的 (city, keyword) 集合。

      成功(ok/empty)                → 一律终态
      永久失败(600014/600047/600058) → 默认终态（那是"这个锚点采不到门店"，不是故障）
      瞬时失败(429/508/5xx)          → 默认**不算**终态，会被重打（那是真丢过数据）
      未知错误码                     → 保守当作终态，不重复烧调用
      unknown(partial 导入，语义不明) → 不算终态，会被重打
    """
    done: set[tuple[str, str]] = set()
    for r in conn.execute(
            "SELECT city, keyword, status, error_code FROM probe WHERE pass=?", (pass_,)):
        st, code = r["status"], (r["error_code"] or "")
        key = (r["city"], r["keyword"])
        if st in ("ok", "empty"):
            done.add(key)
        elif st == "error":
            if code in PERMANENT_HINT:
                if not retry_permanent:
                    done.add(key)
            elif code in TRANSIENT_CODES:
                if not retry_transient:
                    done.add(key)
            else:
                done.add(key)
        # st == "unknown" 落到这里：不加入 done，续采时重打
    return done


def done_cities(conn, pass_: int = PASS_STORE) -> set[str]:
    return {r["city"] for r in conn.execute(
        "SELECT DISTINCT city FROM probe WHERE pass=?", (pass_,))}


# --------------------------------------------------------------------------
# 读出：还原成 mcdindex.py 认识的观测结构
# --------------------------------------------------------------------------

def to_observations(conn, cities: list[str] | None = None) -> dict:
    """组装 {城市: 该城观测}，字段与 tools/collect.py 的 JSON 产物一致。"""
    obs: dict[str, dict] = {}

    def slot(c: str) -> dict:
        if c not in obs:
            obs[c] = {"city": c, "keywordCount": 0, "sampledStoreCount": 0, "stores": {},
                      "driveThruCount": 0, "driveThruMatched": 0, "driveThruBase": 0,
                      "driveThruCodes": [],
                      "coreProbe": None, "anchorProbes": {}, "priceSamples": []}
        return obs[c]

    if cities:
        for c in cities:
            slot(c)

    # 探针：锚点分布 + 城市名锚点的命中明细 + 关键词计数
    for r in conn.execute(
            "SELECT city, keyword, status, found, d5, hits FROM probe WHERE pass=?",
            (PASS_STORE,)):
        o = slot(r["city"])
        o["keywordCount"] += 1
        if r["d5"] is not None and (r["found"] or 0) >= 3:
            o["anchorProbes"][r["keyword"]] = {"d5": r["d5"], "n": r["found"]}
        if r["keyword"] == r["city"] and r["hits"]:
            try:
                hits = json.loads(r["hits"])
            except ValueError:
                hits = []
            if hits:
                o["coreProbe"] = {"keyword": r["keyword"], "hits": hits}

    for r in conn.execute("SELECT city, store_code, name, address, open_at, close_at,"
                          " open_now, reservable FROM store"):
        o = slot(r["city"])
        o["stores"][r["store_code"]] = {
            "name": r["name"], "address": r["address"],
            "open": r["open_at"], "close": r["close_at"],
            "openNow": r["open_now"], "reservable": r["reservable"],
        }

    for r in conn.execute("SELECT city, store_code FROM drive_thru ORDER BY store_code"):
        o = slot(r["city"])
        o["driveThruCodes"].append(r["store_code"])

    samples: dict[str, dict] = {}
    for r in conn.execute("SELECT store_code, item_code, name, original, current"
                          " FROM price_item ORDER BY store_code, item_code"):
        s = samples.setdefault(r["store_code"], {})
        s[r["item_code"]] = {"name": r["name"], "original": r["original"],
                             "current": r["current"]}
    for r in conn.execute("SELECT city, store_code, store_name FROM price_sample"):
        o = slot(r["city"])
        items = samples.get(r["store_code"]) or {}
        if items:
            o["priceSamples"].append({
                "storeCode": r["store_code"], "storeName": r["store_name"],
                "itemCount": len(items), "items": items,
            })

    for o in obs.values():
        o["sampledStoreCount"] = len(o["stores"])
        # ---- 得来速占比的口径（重要，踩过坑）----
        # beType=1（全部门店）与 beType=5（得来速）是**两轮独立采样**，用的是同一套锚点、
        # 同一个"最近 5 家"上限。实测两边交集很小：得来速多在城市外围，而最近 5 家
        # 通常落在市中心 —— 南京采到 10 家得来速，其中只有 1 家出现在门店采样里。
        # 所以「得来速数 ÷ 门店采样数」这个比值是坏的：分子根本不是分母的子集。
        # 正确做法是让分子分母都取自同一套锚点能触达的门店并集：
        #     占比 = |得来速门店| / |门店采样 ∪ 得来速门店|
        store_set = set(o["stores"])
        dt_set = set(o["driveThruCodes"])
        union = store_set | dt_set
        o["driveThruCount"] = len(dt_set)
        o["driveThruMatched"] = len(dt_set & store_set)
        o["driveThruBase"] = len(union)
        o["driveThruCodes"] = sorted(dt_set)
        if o["coreProbe"] is None:
            probe = o["anchorProbes"].get(o["city"])
            if probe:
                o["coreProbe"] = {"keyword": o["city"],
                                  "hits": [{"distance": probe["d5"]}]}
    return obs


def meta(conn, scope: str = None) -> dict:
    """给导出的 JSON 装配 meta 段。"""
    # 调用数要**跨轮累加**：续采意味着数据是好几轮攒出来的，
    # 只取某一轮的最大值会严重低估实际消耗的配额。
    calls = conn.execute("SELECT COALESCE(SUM(calls),0) c FROM run").fetchone()["c"]
    errs = conn.execute("SELECT COALESCE(SUM(errors),0) e FROM run").fetchone()["e"]
    n_runs = conn.execute("SELECT COUNT(*) c FROM run WHERE calls > 0").fetchone()["c"]
    run = conn.execute("SELECT * FROM run ORDER BY run_id DESC LIMIT 1").fetchone()
    n_city = conn.execute("SELECT COUNT(DISTINCT city) c FROM store").fetchone()["c"]
    # 两个"城市数"必须分开叫：采到门店的 300 城 ≠ 参加排名的 337 城。
    # 混用一个 "cities" 曾让快照与工作库算出不同的组内百分位（见 export_json 注释）。
    n_group = conn.execute("SELECT COUNT(DISTINCT city) c FROM probe").fetchone()["c"]
    return {
        "collectedAt": (run["finished_at"] or run["started_at"]) if run else None,
        "mcpCalls": calls,
        "errors": errs,
        "runs": n_runs,
        "citiesInGroup": n_group,
        "citiesWithStores": n_city,
        "scope": scope or (run["scope"] if run else None),
        "method": {
            "stores": "query-nearby-stores searchType=2 beType=1，同构关键词轮询去重",
            "driveThru": "query-nearby-stores searchType=2 beType=5（与门店采样同一套关键词）",
            "price": "query-meals orderType=1 beType=1，取 originalPrice 挂牌价",
            "anchorProbes": "同一次门店采样顺带记录每个关键词锚点的第 5 家距离（额外调用为 0）",
            "storage": "原始观测落 SQLite（tools/out/mcdindex.db），本文件为导出快照",
        },
        "caveats": [
            "sampledStoreCount 是采样下界，不是官方门店总数；仅同构采样下城市间相对可比",
            "门店密度越高，采样越接近饱和不足；各省实测覆盖率见 README",
            "价格用 originalPrice；currentPrice 含限时活动价与卡券绑定，不可跨城比",
            "菜单随时段变化，同一批次内采完才有可比性",
            "latitude/longitude 实测恒为 0.0，无坐标可用；distance 为相对锚点距离",
            "anchorProbes 的锚点由关键词文本解析而来，不同关键词的锚点性质不同，仅作参照分布",
            "直辖市的下级是区/县，本数据集中其「城市」粒度为整个直辖市",
            "得来速占比取自 beType=1 与 beType=5 两轮独立采样的并集，"
            "不用「得来速数 ÷ 门店采样数」（分子不是分母子集）",
        ],
    }


def common_keywords() -> dict:
    cars = ("人民路 解放路 中山路 建设路 文化路 新华路 长江路 北京路 光明路 团结路 "
            "胜利路 青年路 和平路 友谊路 幸福路 迎宾大道 世纪大道").split()
    poi = ("万达 吾悦 广场 大厦 中心 百货 商场 购物中心 超市 医院 学校 大学 公园 车站 "
           "火车站 高铁站 大学城 地铁 步行街 商业街 写字楼 酒店 开发区 工业园 老城区 "
           "新城区 高新区").split()
    return {"roads": cars, "poi": poi}


def export_json(conn, path: str = JSON_PATH, scope: str = None,
                drop_sparse: bool = False) -> dict:
    """把工作库导出成随仓库分发的 JSON 快照。

    🔴 `drop_sparse` 必须保持 False。这里曾写成
    `if o["sampledStoreCount"]`，把"18,100 个关键词下一家门店都没采到"的城市
    过滤掉了 —— 快照剩 300 城，而工作库路径是 337 城。

    后果不是少几行数据，而是**评分被悄悄改掉**：指数的分项是组内百分位，
    比较组规模本身就是分制的一部分。实测两条路径算出的结果确有差异
    （佛山 96.3 → 96.2，苏州与大连名次互换），也就是说
    **随仓库分发的快照复现不出页面上公布的名次**。

    稀疏城市的"密度 0"是采样下界，不是"确实没有门店"，它本来就该留在组里
    （否则会跟"确实没店"混为一谈）。所以默认全量导出。
    """
    cities = to_observations(conn)
    if drop_sparse:
        cities = {c: o for c, o in cities.items() if o["sampledStoreCount"]}
    payload = {
        "meta": meta(conn, scope),
        "commonKeywords": common_keywords(),
        "cities": cities,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload["meta"]


# --------------------------------------------------------------------------
# 从旧的 partial JSON 抢救数据
# --------------------------------------------------------------------------

def import_partial(conn, path: str, run_id: int, keywords: dict[str, list[str]],
                   complete: set[str]) -> dict:
    """把中断那轮写下的 collect.partial.json 灌进库。

    partial 里只有**聚合结果**（门店 / 锚点 / 价格），没有逐次探针的成败，
    所以错误明细无法补录。但能精确知道它跑到哪 —— 任务按 spec 顺序展开，
    `complete` 是调用方（掌握 keywords_for 口径的那一方）算出的
    "关键词已全部采过的城市"集合，`keywords` 是每城的关键词清单。
    据此把这些探针标成终态，续采就不必白重打。
    """
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    cs = d.get("cities") or {}

    n_store = n_probe = n_price = 0
    for city, o in cs.items():
        rows_store = store_rows_from_dict(city, o.get("stores") or {}, run_id)
        rows_dt = dt_rows(city, o.get("driveThruCodes") or [], run_id)
        rows_probe = []

        anchors = o.get("anchorProbes") or {}
        core = o.get("coreProbe") or {}
        hits_json = json.dumps(core.get("hits") or [], ensure_ascii=False) if core else None
        # 跑完的城市：清单里每个关键词都有终态（成功进 anchors，其余记 unknown）
        # 跑到一半的城市：只有 anchors 里那些能确认
        kws = keywords.get(city, []) if city in complete else list(anchors)
        for kw in kws:
            a = anchors.get(kw)
            if a:
                rows_probe.append(probe_row(city, kw, PASS_STORE, "ok", None, None,
                                            a.get("n"), a.get("d5"),
                                            hits_json if kw == city else None, run_id, None))
            elif city in complete:
                # partial 不含逐次成败，无法区分"返回不足 3 家"与"调用失败"。
                # 记成 unknown 而不是 empty —— 续采时会重打一遍，
                # 避免把"可能被瞬时错误吞掉的"观测永久固化下来。
                rows_probe.append(probe_row(city, kw, PASS_STORE, "unknown", None, None,
                                            0, None, None, run_id, None))

        flush(conn, rows_probe, rows_store, rows_dt)
        n_store += len(rows_store)
        n_probe += len(rows_probe)

        for p in o.get("priceSamples") or []:
            upsert_prices(conn, run_id, city, p["storeCode"], p.get("storeName"),
                          p.get("items") or {})
            n_price += 1

    conn.execute("UPDATE run SET note=COALESCE(note,'')||? WHERE run_id=?",
                 (f" [由 partial 导入: 门店{n_store} 探针{n_probe} 价格{n_price};"
                  f" 错误明细不可补录]", run_id))
    conn.commit()
    return {"stores": n_store, "probes": n_probe, "prices": n_price,
            "completeCities": len(complete)}


def has_data(conn) -> bool:
    return conn.execute("SELECT COUNT(*) FROM store").fetchone()[0] > 0


def load_observations():
    """读观测：优先用本地 SQLite 工作库，没有就退回 data/observations.json 快照。

    这样分析脚本不必关心数据最近是"刚采完"还是"只有随仓库分发的快照"。
    """
    if os.path.exists(DB_PATH):
        conn = connect()
        if has_data(conn):
            return to_observations(conn)
        conn.close()
    if os.path.exists(JSON_PATH):
        with open(JSON_PATH, encoding="utf-8") as f:
            return json.load(f)["cities"]
    raise SystemExit("既没有本地 SQLite 工作库，也没有 data/observations.json 快照。"
                     "先跑 python tools/collect.py")


def load_meta() -> dict:
    if os.path.exists(DB_PATH):
        conn = connect()
        if has_data(conn):
            return meta(conn)
        conn.close()
    if os.path.exists(JSON_PATH):
        with open(JSON_PATH, encoding="utf-8") as f:
            return json.load(f).get("meta") or {}
    return {}


def stats(conn) -> dict:
    q = lambda sql: conn.execute(sql).fetchone()[0]
    return {
        "run": q("SELECT COUNT(*) FROM run"),
        "cities_with_stores": q("SELECT COUNT(DISTINCT city) FROM store"),
        "stores": q("SELECT COUNT(*) FROM store"),
        "drive_thru": q("SELECT COUNT(*) FROM drive_thru"),
        "probes_store": q(f"SELECT COUNT(*) FROM probe WHERE pass={PASS_STORE}"),
        "probes_drive": q(f"SELECT COUNT(*) FROM probe WHERE pass={PASS_DRIVE}"),
        "probes_error": q("SELECT COUNT(*) FROM probe WHERE status='error'"),
        "price_samples": q("SELECT COUNT(*) FROM price_sample"),
        "price_items": q("SELECT COUNT(*) FROM price_item"),
        "anchor_probes": q("SELECT COUNT(*) FROM probe WHERE d5 IS NOT NULL"),
    }


def error_breakdown(conn, limit: int = 15) -> list[tuple]:
    return conn.execute(
        "SELECT error_code, status, COUNT(*) n FROM probe WHERE status='error'"
        " GROUP BY error_code ORDER BY n DESC LIMIT ?", (limit,)).fetchall()


if __name__ == "__main__":
    import sys
    c = connect()
    print(json.dumps(stats(c), ensure_ascii=False, indent=2))
    bd = error_breakdown(c)
    if bd:
        print("\n错误码分布（probe 表）:")
        for r in bd:
            print(f"  {r['error_code'] or '(无码)':>10}  {r['n']:>7}")
    if "--export" in sys.argv:
        print("\n[导出]", json.dumps(export_json(c), ensure_ascii=False)[:200])
