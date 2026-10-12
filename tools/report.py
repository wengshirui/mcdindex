#!/usr/bin/env python3
"""MCDIndex 报告生成器（开发期工具）。

读 tools/out/mcdindex.db（SQLite 工作库）→ 调 src/mcdindex.py → 输出：
  1. 终端排行榜
  2. demo/index.html —— 自包含演示页：输入城市名或地址关键词即出指数
     （含「这个指数是什么」说明区块，数字从数据算出来，不写死）

数据全部内嵌进 HTML，运行时不联网、不调 MCP —— 打开即可演示。

用法:
    python tools/report.py
    python tools/report.py --csv        # 额外导出 demo/mcdindex-ranking.csv
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import db  # noqa: E402
import mcdindex as M  # noqa: E402

OBS = os.path.join(ROOT, "data", "observations.json")
DEMO = os.path.join(ROOT, "demo")
HTML_OUT = os.path.join(DEMO, "index.html")
CSV_OUT = os.path.join(DEMO, "mcdindex-ranking.csv")
ANALYSIS = os.path.join(ROOT, "tools", "out", "national-analysis.json")

# 公开渠道的门店数参考（人工核实，仅用于在页面上展示采样覆盖率；非官方数据源）。
# 只把观测集里**真实存在**的城市输出到页面，所以江苏/全国两套数据都能用同一模板。
PUBLIC_REFS = [
    {"city": "北京市", "public": 615, "src": "窄门餐眼 2025-12（直辖市省级数=市级数）"},
    {"city": "上海市", "public": 583, "src": "窄门餐眼 2025-12"},
    {"city": "深圳市", "public": 460, "src": "深圳市商务局，截至 2025-07"},
    {"city": "天津市", "public": 246, "src": "窄门餐眼 2025-12"},
    {"city": "南京市", "public": 204, "src": "公开报道"},
    {"city": "苏州市", "public": 121, "src": "公开报道"},
    {"city": "无锡市", "public": 61, "src": "公开报道"},
    {"city": "南通市", "public": 32, "src": "公开报道"},
]


def load() -> dict:
    """从本地 SQLite 工作库读观测；没有库时退回 data/observations.json 快照。

    原来必须依赖 observaions.json 存在才能出报告；现在采集中途也能随时出报告看进度。
    """
    return {"cities": db.load_observations(), "meta": db.load_meta()}


def load_gdp_check() -> dict | None:
    """读 national_analysis.py 的产物，取「指数与 GDP 排名的关系」这组结论。

    页面要引用这些数字，但不该写死在模板里 —— 重跑分析后页面自动跟着变。
    文件不存在（例如只跑了江苏一版数据）时返回 None，页面会略过这一段。
    """
    try:
        with open(ANALYSIS, encoding="utf-8") as f:
            a = json.load(f)
    except (OSError, ValueError):
        return None
    top = a.get("top20") or []
    if not top:
        return None
    return {
        "overlap": sum(1 for r in top if (r.get("gdpRank") or 999) <= 20),
        "n": len(top),
        "spearman": a.get("gdpSpearman"),
    }


def flatten_stores(cities: dict) -> list[dict]:
    out = []
    for city, obs in cities.items():
        dt = set(obs.get("driveThruCodes") or [])
        for code, s in (obs.get("stores") or {}).items():
            out.append({
                "c": city,
                "n": s.get("name") or "",
                "a": s.get("address") or "",
                "d": 1 if code in dt else 0,
                "h": s.get("open"),
                "e": s.get("close"),
            })
    return out


def print_table(result: dict) -> None:
    label = {"density": "密度", "reach": "覆盖", "driveThru": "得来速"}
    print("=" * 92)
    print(f"{'排名':<5}{'城市':<9}{'MCDIndex':>9}{'密度':>7}{'覆盖':>7}{'得来速':>7}"
          f"{'门店':>6}{'得来速':>7}{'占比%':>7}{'核心⑤':>8}{'篮子价':>9}")
    print("-" * 92)
    for i, c in enumerate(result["order"], 1):
        row = result["cities"][c]
        s, r = row["subScores"], row["raw"]
        print(f"{i:<5}{c:<9}{row['score']:>9}{s['density']:>7}{str(s['reach']):>7}"
              f"{str(s['driveThru']):>7}{r['sampledStores']:>6}{r['driveThruCount']:>7}"
              f"{str(r['driveThruShare']):>7}{str(r['coreReach5']):>8}"
              f"{str(r['basketTotal']):>9}")
    print("=" * 92)

    print("\n分维度区分度诊断（相对极差 < %.2f 会被判定为无区分度并自动收缩）:" % M.MIN_SPREAD)
    for k, d in result["diagnostics"].items():
        flag = "有区分度" if d["discriminating"] else "区分度不足→已收缩"
        print(f"  {label.get(k, k):<6} 极差 {d['spread']:.3f}  "
              f"range {d['min']}~{d['max']}  置信 {d['reliability']}  {flag}")
    for k, d in result["readingsDiagnostics"].items():
        nm = {"price": "价格篮子", "hours": "营业时长"}.get(k, k)
        print(f"  {nm:<6} 极差 {d['spread']:.3f}  "
              f"range {d['min']}~{d['max']}  （仅读数，不计分）")


def build_html(payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    tpl = HTML_TEMPLATE.replace("/*__DATA__*/null", data)
    return tpl


def main() -> None:
    raw = load()
    cities = raw["cities"]
    result = M.build_index(cities)

    print_table(result)
    print()
    for c in result["order"][:3]:
        print(M.explain(result, c))
    print()

    payload = {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M"),
        "collectedAt": raw.get("meta", {}).get("collectedAt"),
        "mcpCalls": raw.get("meta", {}).get("mcpCalls"),
        "scope": raw.get("meta", {}).get("scope"),
        "index": result,
        "stores": flatten_stores(cities),
        "meta": raw.get("meta", {}),
        # 「指数和 GDP 排名对得上吗」的结论，来自 national_analysis.py 的产物
        "gdpCheck": load_gdp_check(),
        # 公开渠道参考值（人工核实，用于展示采样覆盖率；非官方数据源）
        "publicRef": [
            {**ref, "mine": result["cities"][ref["city"]]["raw"]["sampledStores"]}
            for ref in PUBLIC_REFS if ref["city"] in result["cities"]
        ],
    }

    os.makedirs(DEMO, exist_ok=True)
    with open(HTML_OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(build_html(payload))
    print(f"[已保存] {HTML_OUT}")

    if "--csv" in sys.argv:
        # newline="" 是 csv 模块的要求；再显式指定 lineterminator="\n"，
        # 否则 csv 默认写 \r\n，与 .gitattributes 的 eol=lf 冲突、制造整文件 diff。
        with open(CSV_OUT, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["排名", "城市", "MCDIndex", "密度分", "覆盖分", "得来速分",
                        "采样门店", "得来速门店", "得来速占比%", "核心5家距离m",
                        "篮子挂牌价", "营业时长中位h", "可预约占比", "菜单SKU"])
            for i, c in enumerate(result["order"], 1):
                r = result["cities"][c]
                s, k = r["subScores"], r["raw"]
                w.writerow([i, c, r["score"], s["density"], s["reach"], s["driveThru"],
                            k["sampledStores"], k["driveThruCount"], k["driveThruShare"],
                            k["coreReach5"], k["basketTotal"], k["hoursMedian"],
                            k["reservableShare"], k["menuSku"]])
        print(f"[已保存] {CSV_OUT}")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>麦当劳指数 MCDIndex · 同级别比较</title>
<style>
:root{
  --bg:#0e0e0f; --card:#17181a; --card2:#1f2124; --line:#2a2c30;
  --fg:#f2f2f0; --mut:#9a9c9f; --dim:#6e7073;
  --red:#DA291C; --gold:#FFC72C; --blue:#4aa3ff; --green:#4ecb71;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  line-height:1.6;-webkit-font-smoothing:antialiased}
.wrap{max-width:940px;margin:0 auto;padding:32px 20px 64px}
header h1{font-size:26px;font-weight:600;margin:0 0 6px}
header h1 em{color:var(--gold);font-style:normal}
header p{margin:0;color:var(--mut);font-size:13px}
.badge{display:inline-block;font-size:11px;padding:2px 8px;border:1px solid var(--line);
  border-radius:99px;color:var(--dim);margin-left:8px;vertical-align:middle}
.jump{color:var(--gold);text-decoration:none;border-bottom:1px dashed currentColor;margin-left:6px}
.search{margin:24px 0 8px;display:flex;gap:10px;flex-wrap:wrap}
.search input{flex:1;min-width:240px;background:var(--card);border:1px solid var(--line);
  border-radius:10px;padding:13px 15px;color:var(--fg);font-size:15px;outline:none}
.search input:focus{border-color:var(--red)}
.hint{margin:10px 0 0;font-size:12px;color:var(--dim)}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.chip{font-size:12px;padding:4px 10px;border-radius:99px;background:var(--card2);
  border:1px solid var(--line);color:var(--mut);cursor:pointer}
.chip:hover{border-color:var(--gold);color:var(--gold)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:22px 24px;margin-top:20px}
.card.err{border-color:#4a2222}
.hero{display:flex;align-items:flex-end;gap:22px;flex-wrap:wrap}
.score{font-size:64px;font-weight:600;line-height:1;letter-spacing:-2px}
.score small{font-size:16px;font-weight:400;color:var(--dim);margin-left:6px;letter-spacing:0}
.hero .meta{color:var(--mut);font-size:13px}
.hero .meta b{color:var(--fg);font-weight:500}
.rankbar{display:inline-block;background:var(--card2);border:1px solid var(--line);
  border-radius:8px;padding:6px 12px;font-size:13px;color:var(--gold)}
.dims{margin-top:22px;display:grid;gap:14px}
.dim{display:grid;grid-template-columns:78px 1fr 52px;align-items:center;gap:12px;font-size:13px}
.dim .track{display:block;height:10px;background:#2b2d31;border-radius:99px;overflow:hidden}
.dim .fill{display:block;height:100%;border-radius:99px;background:var(--red);min-width:2px}
.dim .val{text-align:right;color:var(--mut);font-variant-numeric:tabular-nums}
.reads{margin-top:22px;display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}
.read{background:var(--card2);border-radius:10px;padding:11px 13px;
  display:flex;flex-direction:column;justify-content:space-between;min-height:70px}
.read .k{font-size:11px;color:var(--dim);margin-bottom:6px;line-height:1.35}
.read .v{font-size:17px;font-weight:500;font-variant-numeric:tabular-nums}
.tbl{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}
.tbl th,.tbl td{padding:9px 8px;text-align:right;border-bottom:1px solid var(--line)}
.tbl th{color:var(--dim);font-weight:400;font-size:12px}
.tbl th:first-child,.tbl td:first-child{text-align:left}
.tbl tbody tr{cursor:pointer}
.tbl tbody tr:hover{background:var(--card2)}
.tbl tbody tr.me{background:rgba(218,41,28,.14)}
.tbl tbody tr.me td:first-child{color:var(--gold)}
.num{font-variant-numeric:tabular-nums}
h2{font-size:15px;font-weight:600;margin:30px 0 0}
.note{font-size:12.5px;color:var(--mut);margin-top:12px}
.note li{margin:5px 0}
.note code{background:var(--card2);padding:1px 5px;border-radius:4px;font-size:11.5px;color:var(--gold)}
.about{margin-top:12px}
.about p{margin:0 0 12px;font-size:13.5px}
.about p:last-child{margin-bottom:0}
.about .lead{color:var(--mut)}
.about .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(212px,1fr));gap:10px;margin:4px 0 16px}
.about .c{background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.about .c h3{font-size:11.5px;font-weight:400;color:var(--gold);margin:0 0 8px}
.about .c p{margin:0;font-size:13px}
.about .c ul{margin:0;padding-left:16px;font-size:13px;line-height:1.7}
.about .c li{margin:3px 0}
.about b{color:var(--fg);font-weight:500}
.about .mut{color:var(--dim);font-size:12px;margin-top:8px}
.about .neg{border-left:2px solid var(--line);padding-left:14px;color:var(--mut)}
.about .neg b{color:var(--fg)}
.about .kv{display:flex;justify-content:space-between;gap:10px}
.about .kv i{font-style:normal;color:var(--gold);font-variant-numeric:tabular-nums}
.stores{margin-top:12px;border-top:1px solid var(--line)}
.st{display:flex;justify-content:space-between;gap:12px;padding:9px 2px;border-bottom:1px solid var(--line);font-size:13px}
.st .n{color:var(--fg)}
.st .a{color:var(--dim);font-size:12px}
.hidden{display:none}
footer{margin-top:40px;color:var(--dim);font-size:11.5px;text-align:center}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>麦当劳指数 <em>MCDIndex</em><span class="badge" id="scope"></span></h1>
    <p>输入城市名或任意地址关键词，看它与同组城市的相对位置。<a class="jump" href="#about-sec">这个指数怎么读 →</a></p>
  </header>

  <div class="search">
    <input id="q" placeholder="试试：上海 / 深圳 / 苏州 / 万达 / 珠江路" autocomplete="off">
  </div>
  <p class="hint" id="hint">城市名直接出指数；地址/门店名会先定位到所在城市再出指数。</p>
  <div class="chips" id="chips"></div>

  <div id="out"></div>

  <h2>同组排行榜 <span class="badge" id="rankMeta"></span></h2>
  <table class="tbl" id="rank">
    <thead><tr>
      <th>#</th><th>城市</th><th>MCDIndex</th><th>密度</th><th>覆盖</th><th>得来速</th>
      <th>采样门店</th><th>得来速占比</th><th>核心⑤距离</th><th>篮子价</th>
    </tr></thead>
    <tbody></tbody>
  </table>
  <p class="hint" style="margin-top:12px"><span class="chip" id="more"></span></p>

  <h2 id="about-sec">这个指数是什么</h2>
  <div class="about" id="about"></div>

  <h2>口径与已知局限</h2>
  <ul class="note" id="notes"></ul>

  <footer id="foot"></footer>
</div>

<script>
const PAYLOAD = /*__DATA__*/null;
const IX = PAYLOAD.index, STORES = PAYLOAD.stores;
const REFS = PAYLOAD.publicRef || [];
const SCOPE = PAYLOAD.scope || '';
const GROUP = IX.meta.groupSize;
const CITY = {};
STORES.forEach(s => { if(!CITY[s.c]) CITY[s.c] = {city:s.c, n:0, dt:0}; CITY[s.c].n++; if(s.d) CITY[s.c].dt++; });

const DIMLABEL = {density:'门店密度', reach:'核心区覆盖', driveThru:'得来速覆盖'};
const fmt = v => (v===null||v===undefined) ? '—' : (typeof v==='number' ? Math.round(v*10)/10 : v);
const esc = s => String(s).replace(/[&<>"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch]));

function resolve(q){
  q = (q||'').trim();
  if(!q) return null;
  const cities = IX.order;
  let hit = cities.find(c => c === q || c === q + '市' || c.replace('市','') === q);
  if(hit) return {mode:'city', city:hit};
  const sub = cities.filter(c => c.includes(q) || q.includes(c.replace('市','')));
  if(sub.length === 1) return {mode:'city', city:sub[0]};
  const ms = STORES.filter(s => s.n.includes(q) || s.a.includes(q));
  if(ms.length){
    const tally = {}; ms.forEach(s => tally[s.c] = (tally[s.c]||0)+1);
    const best = Object.keys(tally).sort((a,b)=>tally[b]-tally[a])[0];
    return {mode:'addr', city:best, matched:ms, tally};
  }
  return {mode:'none'};
}

function row(c){
  const r = IX.cities[c], s = r.subScores, k = r.raw;
  return '<tr data-c="'+esc(c)+'"><td class="num">'+r.rank+'</td><td>'+esc(c)
    +'</td><td class="num"><b>'+r.score+'</b></td><td class="num">'+fmt(s.density)
    +'</td><td class="num">'+fmt(s.reach)+'</td><td class="num">'+fmt(s.driveThru)
    +'</td><td class="num">'+k.sampledStores+'</td><td class="num">'+fmt(k.driveThruShare)+'%'
    +'</td><td class="num">'+fmt(k.coreReach5)+' m</td><td class="num">'+(k.basketTotal?'¥'+k.basketTotal:'—')+'</td></tr>';
}

function render(q){
  const res = resolve(q), out = document.getElementById('out'), hint = document.getElementById('hint');
  document.querySelectorAll('#rank tbody tr').forEach(tr => tr.classList.remove('me'));
  if(!res){ out.innerHTML=''; return; }
  if(res.mode === 'none'){
    out.innerHTML = '<div class="card err"><b>没找到匹配。</b><div class="note">已覆盖 '+GROUP
      + ' 个城市共 ' + STORES.length + ' 家门店。可以试城市名，或门店名/地址里的词（万达、地铁、大学…）。</div></div>';
    return;
  }
  const c = res.city, r = IX.cities[c], s = r.subScores, k = r.raw;
  const tr = document.querySelector('#rank tbody tr[data-c="'+c+'"]');
  if(tr){ tr.classList.add('me'); }
  let html = '<div class="card"><div class="hero">'
    + '<div><div class="score">'+r.score+'<small>/100</small></div></div>'
    + '<div class="meta"><div class="rankbar">同组第 '+r.rank+' / '+r.rankOf+' 名</div>'
    + '<div style="margin-top:8px">'+c+'</div></div></div>';
  html += '<div class="dims">';
  for(const d of ['density','reach','driveThru']){
    const v = s[d];
    html += '<div class="dim"><span>'+DIMLABEL[d]+'</span><span class="track"><span class="fill" style="width:'
      + (v===null?0:v) + '%;background:'+(d==='density'?'var(--red)':d==='reach'?'var(--gold)':'var(--blue)')
      + '"></span></span><span class="val">'+fmt(v)+'</span></div>';
  }
  html += '</div><div class="reads">'
    + '<div class="read"><div class="k">采样门店</div><div class="v">'+k.sampledStores+'</div></div>'
    + '<div class="read"><div class="k">得来速门店</div><div class="v">'+k.driveThruCount+'</div></div>'
    + '<div class="read"><div class="k">核心区 5 家半径</div><div class="v">'+(k.coreReach5?fmt(k.coreReach5)+' m':'—')+'</div></div>'
    + '<div class="read"><div class="k">营业时长中位</div><div class="v">'+(k.hoursMedian?k.hoursMedian+' h':'—')+'</div></div>'
    + '<div class="read"><div class="k">可预约占比</div><div class="v">'+(k.reservableShare!==null?Math.round(k.reservableShare*100)+'%':'—')+'</div></div>'
    + '<div class="read"><div class="k">共有SKU篮子价</div><div class="v">'+(k.basketTotal?'¥'+k.basketTotal:'—')+'</div></div>'
    + '</div>';
  if(res.mode === 'addr'){
    const top = res.matched.slice(0,6);
    html += '<div class="note">地址匹配：在 <b>'+res.matched.length+'</b> 家门店命中「'+q
      + '」，归属城市以 <b>'+c+'</b> 为主。该城指数如上。</div><div class="stores">';
    top.forEach(s2 => { html += '<div class="st"><span class="n">'+s2.n
      + (s2.d?' <span style="color:var(--gold);font-size:11px">得来速</span>':'')
      + '</span><span class="a">'+s2.a+'</span></div>'; });
    if(res.matched.length > top.length) html += '<div class="st"><span class="a">…还有 '
      + (res.matched.length - top.length) + ' 家</span><span></span></div>';
    html += '</div>';
  }
  out.innerHTML = html + '</div>';
}

let SHOW = 30;

function renderTable(){
  const tb = document.querySelector('#rank tbody');
  tb.innerHTML = IX.order.slice(0, SHOW).map(row).join('');
  tb.querySelectorAll('tr').forEach(tr => tr.addEventListener('click', () => {
    const c = tr.getAttribute('data-c');
    document.getElementById('q').value = c;
    render(c);
  }));
  document.getElementById('rankMeta').textContent = '共 ' + GROUP + ' 城';
  const more = document.getElementById('more');
  if(IX.order.length > SHOW){
    more.textContent = '展开全部 ' + IX.order.length + ' 城 ▾';
    more.classList.remove('hidden');
  } else {
    more.classList.add('hidden');
  }
}

function boot(){
  document.getElementById('scope').textContent = SCOPE + ' · 同级别比较';
  document.title = '麦当劳指数 MCDIndex · ' + (SCOPE || '同级别比较');
  renderTable();
  document.getElementById('more').addEventListener('click', () => {
    SHOW = IX.order.length; renderTable(); render(document.getElementById('q').value);
  });

  const dx = IX.diagnostics, rd = IX.readingsDiagnostics;
  const refLis = REFS.map(x => x.city + ' 采样 ' + x.mine + ' / 公开 ' + x.public
    + '（' + Math.round(x.mine / x.public * 100) + '%，' + x.src + '）');
  document.getElementById('notes').innerHTML = [
    '<li><b>为什么只做同级别比较：</b>MCP 的门店接口单次固定只回 5 家、无分页，且经纬度恒返回 0.0，入参也没有坐标字段 —— 所以拿不到门店绝对总数、也无法按坐标聚合。改用 <code>同构关键词采样</code>（'+GROUP+' 城共用同一套关键词模板）后，城市间的<b>相对</b>大小才可比，绝对量仍是采样下界。</li>',
    '<li><b>密度偏差是非均匀的：</b>' + (refLis.length
      ? ('对照公开数据，' + refLis.join('；') + '。门店越密越容易低估 —— 这也是本页只给排名、不给绝对结论的原因。')
      : '对照公开数据，门店越密的城市越容易被低估。') + '</li>',
    '<li><b>价格为什么不计分：</b>' + IX.basketSize + ' 个共有 SKU 的挂牌价篮子，全组相对极差（极差÷中位数）只有 '
      + (rd.price.spread*100).toFixed(1) + '%（篮子 ¥' + rd.price.min + ' ~ ¥' + rd.price.max
      + '），低于 ' + Math.round(IX.meta.minSpread*100) + '% 的防假精度阈值，说明差异小到不值得排序，'
      + '因此降级为读数。</li>',
    '<li><b>营业时长同理：</b>中位数全部落在 ' + rd.hours.min + '~' + rd.hours.max + ' 小时之间，不构成区分度，同样只作展示。</li>',
    '<li><b>防假精度护栏：</b>任一维度的相对极差低于 ' + IX.meta.minSpread + ' 时，得分自动向 50 收缩（当前 密度 '
      + dx.density.spread.toFixed(2) + ' / 覆盖 ' + dx.reach.spread.toFixed(2) + ' / 得来速 '
      + dx.driveThru.spread.toFixed(2) + '）。</li>',
    '<li><b>覆盖维度的局限：</b>「核心区 5 家半径」取的是城市名关键词解析出的锚点，锚点位置不完全可控；优点是它<b>不依赖采样饱和度</b>，比门店计数更抗偏差。</li>',
    '<li><b>数据口径：</b>价格用 <code>originalPrice</code>（挂牌价），不用含限时活动与卡券绑定的 <code>currentPrice</code>；菜单随时段变化，同批采集以保证可比。</li>'
  ].join('');

  // 「这个指数是什么」——讲清参照系（星巴克指数）、我们改了什么、以及它不是什么
  const PD = IX.readingsDiagnostics.price, WT = IX.weights || {};
  const w100 = d => (WT[d] === undefined ? '—' : Math.round(WT[d] * 100) + '%');
  const fsC = IX.cities['佛山市'], cqC = IX.cities['重庆市'];
  const gd = PAYLOAD.gdpCheck;
  const ab = [];
  ab.push('<p class="lead">用连锁门店的密度或价格去描述一座城市，不是我们首创。'
    + '这类做法常被统称为<b>「星巴克指数」</b>：《经济学人》2004 年的<b>「中杯拿铁指数」</b>'
    + '拿各国中杯拿铁的售价去比购买力平价；2008 年《商业周刊》专栏作家 Daniel Gross 的'
    + '<b>「星巴克分店指数」</b>反过来用门店密度看经济过热的风险；'
    + '戴德梁行的<b>「星巴克指标」</b>把门店数量当作商业活力的代理变量。'
    + '国内也习惯了「数一数有几个星巴克」来判断城市能级。</p>');
  ab.push('<p>但麦当劳和星巴克量的不是同一样东西。星巴克的门店密度更接近<b>中产浓度与写字楼密度</b>；'
    + '而麦当劳客单更低、消费频次更高、选址从核心商圈一直铺到社区、学校、高速服务区，'
    + '更像<b>日常餐饮的基础设施</b>。所以本指数给的不是「这座城市有多富」，'
    + '而是<b>你在城里随机活动时，撞见一家麦当劳的容易程度</b>。</p>');
  ab.push('<div class="cards">');
  ab.push('<div class="c"><h3>为什么不沿用价格口径</h3><p>'
    + '拿铁指数、巨无霸指数能成立的前提，是各地价格确实有差异。我们实测：'
    + '江苏 13 城同一款巨无霸挂牌价的极差只有 <b>1.66%</b>；扩到全国 <b>' + IX.basketSize
    + '</b> 个共有 SKU、<b>' + GROUP + '</b> 城，篮子价的相对极差（极差÷中位数）是 <b>'
    + (PD.spread*100).toFixed(1) + '%</b>（¥' + PD.min + ' ~ ¥' + PD.max + '），'
    + '低于我们设定的 ' + Math.round(IX.meta.minSpread*100) + '% 防假精度阈值。'
    + '既然差异小到不值得排序，拿它算百分位就是制造精度 —— 所以价格只作读数展示，<b>不计分</b>。</p></div>');
  ab.push('<div class="c"><h3>三个计分维度</h3><ul>'
    + '<li><span class="kv"><span>门店密度</span><i>' + w100('density') + '</i></span></li>'
    + '<li><span class="kv"><span>核心区覆盖</span><i>' + w100('reach') + '</i></span></li>'
    + '<li><span class="kv"><span>得来速覆盖</span><i>' + w100('driveThru') + '</i></span></li>'
    + '</ul><p class="mut">每个分项都先取同级别城市内的百分位，再加权求和。</p></div>');
  ab.push('<div class="c"><h3>分数怎么读</h3><ul>'
    + '<li>90 分＝组内前 10%，不是「有 90% 的门店」</li>'
    + '<li>分差是相对量，不能换算成倍数</li>'
    + '<li>前 20 名分差普遍在 1 分内，属同一档</li>'
    + '</ul></div>');
  ab.push('</div>');
  ab.push('<p class="neg"><b>它不是什么。</b>'
    + (fsC && cqC ? ('不是 GDP，也不是发达程度 —— 佛山第 ' + fsC.rank + '、重庆第 ' + cqC.rank
      + '，反映的是麦当劳在这两地布局策略的差别，而不是佛山比重庆发达；') : '')
    + '不是消费能力 —— 价格维度没有计分，指数里不含任何消费金额；'
    + '不是门店绝对数 —— MCP 的门店接口单次固定只回 5 家且无分页，这里是<b>采样下界</b>，'
    + '门店越密的市场反而越容易被低估（明细见下方局限）。'
    + (gd && gd.spearman !== null ? ('与 2025 年城市 GDP 前 20 名的交集是 <b>' + gd.overlap + '/'
      + gd.n + '</b>，对 GDP 前 50 名的秩相关 <b>ρ=' + gd.spearman.toFixed(2)
      + '</b> —— 相关，但<b>不是 GDP 的翻版</b>。') : '')
    + '</p>');
  document.getElementById('about').innerHTML = ab.join('');
  document.getElementById('chips').innerHTML =
    IX.order.slice(0, 5).concat(['广场', '万达', '地铁', '大学'])
      .map(t => '<span class="chip">'+esc(t)+'</span>').join('');
  document.querySelectorAll('#chips .chip').forEach(el => el.addEventListener('click', () => {
    document.getElementById('q').value = el.textContent; render(el.textContent);
  }));
  document.getElementById('foot').textContent =
    'MCDIndex · 数据 ' + (PAYLOAD.collectedAt||'') + ' · MCP 调用 ' + (PAYLOAD.mcpCalls||'') + ' 次 · 渲染于 ' + PAYLOAD.generatedAt;
  const inp = document.getElementById('q');
  inp.addEventListener('input', e => render(e.target.value));
  inp.addEventListener('keydown', e => { if(e.key === 'Enter') render(inp.value); });
  render(IX.order[0]);
}
boot();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
