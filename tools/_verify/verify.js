const { chromium } = require('playwright');
const { pathToFileURL } = require('url');
const fs = require('fs');
const path = require('path');

// 路径一律从脚本自身位置推导 —— 仓库里不得出现盘符 / 用户名 / 机器名
const DIR = __dirname + path.sep;
const PAGE_PATH = path.resolve(__dirname, '..', '..', 'demo', 'index.html');
const PAGE = pathToFileURL(PAGE_PATH).href;
const SHOT = path.join(DIR, '_shots') + path.sep;
fs.mkdirSync(SHOT, { recursive: true });

const o = {};
const P = s => process.stdout.write(s + '\n');
const t = async (k, fn) => {
  P('>> ' + k);
  try { o[k] = await fn(); } catch (e) {
    o[k] = 'ERR: ' + String(e.message || '').replace(/\s+/g, ' ').slice(0, 300);
  }
  P('<< ' + k + ' :: ' + String(JSON.stringify(o[k])).slice(0, 400));
};

process.on('uncaughtException', e => {
  fs.writeFileSync(DIR + '_uncaught.txt', String(e.stack || e), 'utf8');
  process.exit(1);
});

(async () => {
  const errs = [];
  const browser = await chromium.launch({ channel: 'msedge' });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.setDefaultTimeout(9000);
  page.on('pageerror', e => errs.push('PAGEERROR: ' + (e.stack || e.message)));
  page.on('console', m => { if (m.type() === 'error') errs.push('CONSOLE: ' + m.text()); });

  await page.goto(PAGE);
  await page.waitForTimeout(700);

  // 就绪门禁：没渲染出排行榜就直接中止，避免负向断言在空 DOM 上假绿。
  // 行数不写死 —— 数据集可能是 13 城也可能是 300 城，页面只渲染前 30 行。
  // 期望值从 #rankMeta 的「共 N 城」读出来算，别让门禁本身成为"换数据就失效"的那一环。
  const total = await page.$eval('#rankMeta', el => {
    const m = (el.textContent || '').match(/(\d+)/);
    return m ? parseInt(m[1], 10) : 0;
  });
  const rows = await page.$$eval('#rank tbody tr', els => els.length);
  const expect = Math.min(30, total);
  if (!total || rows !== expect) {
    fs.writeFileSync(DIR + '_notready.json', JSON.stringify({ rows, total, errs }, null, 2), 'utf8');
    P('!! 页面未就绪 rows=' + rows + ' 期望=' + expect + ' 中止');
    await browser.close();
    process.exit(2);
  }

  await t('结构·排行榜行数与城市总数一致', async () =>
    page.$$eval('#rank tbody tr td:first-child', els => els.map(e => e.textContent)));

  await t('结构·首行数值', async () =>
    page.$$eval('#rank tbody tr:first-child td', els => els.map(e => e.textContent.trim())));

  await t('口径说明 7 条', async () =>
    page.$$eval('#notes li', els => els.length));

  // 改名护栏：品牌名统一为 MCDIndex（赛事项目惯例 mcd- 前缀）。
  // 只查渲染后的可见文案 + title，不查内嵌 JSON（怕误伤无关字符串）。
  await t('品牌·全页无旧名 McIndex 残留', async () => {
    const r = await page.evaluate(() => {
      const txt = document.body.innerText;
      const ttl = document.title;
      const head = document.querySelector('header h1').innerText;
      return {
        title: ttl,
        h1: head.replace(/\s+/g, ' ').trim(),
        oldInBody: /McIndex/.test(txt),
        oldInTitle: /McIndex/.test(ttl),
        newInTitle: /MCDIndex/.test(ttl),
        newInH1: /MCDIndex/.test(head),
        foot: document.getElementById('foot').textContent.trim().slice(0, 40),
      };
    });
    r.ok = !r.oldInBody && !r.oldInTitle && r.newInTitle && r.newInH1;
    return r;
  });

  await t('口径说明无残留占位符', async () =>
    page.$eval('#notes', el => /undefined|null|NaN|\{\{/.test(el.textContent)));

  // 「这个指数是什么」区块：断言渲染出来的数字与内嵌数据一致 ——
  // 光断言"文字存在"是假绿（文案可以写死），必须卡住"文案是算出来的"。
  // ⚠️ 额外加一条**负向**断言：文案里不得出现竞品品牌。
  // 比赛规则 activityGuidelines.md 第 84 行禁止把麦当劳品牌/产品与其他品牌做对比，
  // 所以「没有竞品品牌」不是文风偏好，是合规要求 —— 一旦有人写回去必须红灯。
  await t('说明·区块渲染且引用巨无霸指数', async () => {
    const r = await page.evaluate(() => {
      const el = document.getElementById('about');
      if (!el) return { missing: true };
      const txt = el.innerText.replace(/\s+/g, ' ').trim();
      const pcts = Array.from(el.querySelectorAll('.kv i')).map(i => parseInt(i.textContent, 10));
      const w = IX.weights || {};
      const want = ['density', 'reach', 'driveThru'].map(d => Math.round(w[d] * 100));
      const g = PAYLOAD.gdpCheck;
      const mRank = txt.match(/佛山第 (\d+)、重庆第 (\d+)/);
      const mOv = txt.match(/交集是 (\d+)\/(\d+)/);
      const mRho = txt.match(/ρ=([\d.]+)/);
      return {
        cards: el.querySelectorAll('.c').length,
        len: txt.length,
        hasRef: txt.includes('巨无霸指数') && txt.includes('购买力平价'),
        hasNegativeList: txt.includes('它不是什么') && txt.includes('不是 GDP'),
        competitorHits: (txt.match(/星巴克|瑞幸|肯德基|汉堡王|华莱士|塔斯汀|德克士/g) || []),
        placeholder: /undefined|null|NaN|\{\{/.test(txt),
        weightsRendered: pcts, weightsWanted: want,
        weightsSum: pcts.reduce((a, b) => a + b, 0),
        rankRendered: mRank ? [+mRank[1], +mRank[2]] : null,
        rankWanted: [IX.cities['佛山市'].rank, IX.cities['重庆市'].rank],
        overlapRendered: mOv ? [+mOv[1], +mOv[2]] : null,
        overlapWanted: g ? [g.overlap, g.n] : null,
        rhoRendered: mRho ? mRho[1] : null,
        rhoWanted: g && g.spearman !== null ? g.spearman.toFixed(2) : null,
      };
    });
    r.rankOk = !!r.rankRendered && JSON.stringify(r.rankRendered) === JSON.stringify(r.rankWanted);
    r.weightsOk = JSON.stringify(r.weightsRendered) === JSON.stringify(r.weightsWanted) && r.weightsSum === 100;
    r.gdpOk = !!r.overlapRendered && JSON.stringify(r.overlapRendered) === JSON.stringify(r.overlapWanted)
      && r.rhoRendered === r.rhoWanted;
    r.ok = !r.missing && r.cards === 3 && r.len > 400 && r.hasRef && r.hasNegativeList
      && r.competitorHits.length === 0
      && !r.placeholder && r.weightsOk && r.rankOk && r.gdpOk;
    return r;
  });

  // 页内跳转必须真有落点 —— 断链是"看着有、点了没反应"的典型假绿
  await t('说明·顶部跳转锚点可达', async () => {
    const r = await page.evaluate(() => {
      const a = document.querySelector('a.jump');
      if (!a) return { missing: true };
      const id = (a.getAttribute('href') || '').replace('#', '');
      const tgt = document.getElementById(id);
      return { href: a.getAttribute('href'), targetFound: !!tgt, text: a.textContent.trim() };
    });
    r.ok = !r.missing && r.targetFound;
    return r;
  });

  const search = async (q) => {
    await page.fill('#q', '');
    await page.fill('#q', q);
    await page.waitForTimeout(280);
    return page.$eval('#out', el => el.innerText.replace(/\s+/g, ' ').trim());
  };

  // 具体的分数/名次会随数据集变化（江苏 13 城 → 全国 337 城就全变了），
  // 所以这里只断言"渲染出了指数与组内名次、且归属城市正确"这类结构事实，
  // 不写死数字 —— 否则换个数据集这条断言必红，测的就不是功能而是数据。
  await t('交互·搜索城市「苏州」', async () => {
    const txt = await search('苏州');
    return {
      len: txt.length,
      hasScore: /\d+(\.\d+)?\s*\/\s*100/.test(txt),
      hasRank: /同组第\s*\d+\s*\/\s*\d+\s*名/.test(txt),
      rightCity: txt.includes('苏州市'),
      snippet: txt.slice(0, 110),
    };
  });

  await t('交互·搜索城市简写「南京」', async () => {
    const txt = await search('南京');
    return {
      hasRank: /同组第\s*\d+\s*\/\s*\d+\s*名/.test(txt),
      hasScore: /\d+(\.\d+)?\s*\/\s*100/.test(txt),
      rightCity: txt.includes('南京市'),
    };
  });

  await t('交互·地址模式「新街口」', async () => {
    const txt = await search('新街口');
    return { isAddr: txt.includes('地址匹配'), hasCity: txt.includes('南京'), snippet: txt.slice(0, 150) };
  });

  await t('交互·地址模式「高铁站」', async () => {
    const txt = await search('高铁站');
    return { isAddr: txt.includes('地址匹配'), snippet: txt.slice(0, 130) };
  });

  await t('交互·无法匹配时给兜底', async () => {
    const txt = await search('zzz不存在的词');
    return { hasErr: txt.includes('没找到匹配'), snippet: txt.slice(0, 90) };
  });

  await t('交互·点排行榜行可联动', async () => {
    await page.fill('#q', '');
    await page.click('#rank tbody tr:nth-child(13)');
    await page.waitForTimeout(280);
    const txt = await page.$eval('#out', el => el.innerText.replace(/\s+/g, ' '));
    const cls = await page.$eval('#rank tbody tr:nth-child(13)', el => el.className);
    return { selected: cls.includes('me'), snippet: txt.slice(0, 90) };
  });

  await t('视觉·分项条真实几何（防止 inline 元素不生效）', async () => {
    await search('苏州');
    return page.$$eval('#out .dim', els => els.map(e => {
      const f = e.querySelector('.fill'), tr = e.querySelector('.track');
      const fb = f.getBoundingClientRect(), tb = tr.getBoundingClientRect();
      const val = parseFloat(e.querySelector('.val').textContent);
      return {
        label: e.querySelector('span').textContent,
        fillW: Math.round(fb.width), trackW: Math.round(tb.width), fillH: Math.round(fb.height),
        ratio: Math.round(fb.width / tb.width * 1000) / 10,
        want: val,
        ok: fb.height > 0 && fb.width > 0 && Math.abs(fb.width / tb.width * 100 - val) < 1.5,
      };
    }));
  });

  // 截图：先滚到顶
  await page.evaluate(() => window.scrollTo(0, 0));
  await search('苏州');
  await page.waitForTimeout(400);
  await page.screenshot({ path: SHOT + 'v1-苏州.png' });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.fill('#q', '');
  await page.waitForTimeout(300);
  await page.screenshot({ path: SHOT + 'v0-首屏.png', fullPage: true });
  await search('新街口');
  await page.waitForTimeout(300);
  await page.screenshot({ path: SHOT + 'v2-新街口.png' });
  await page.evaluate(() => {
    const el = document.getElementById('about');
    if (el) window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - 20);
  });
  await page.waitForTimeout(300);
  await page.screenshot({ path: SHOT + 'v3-说明.png' });

  o.errs = errs;
  fs.writeFileSync(DIR + '_report.json', JSON.stringify(o, null, 2), 'utf8');
  P('== errs: ' + JSON.stringify(errs));
  await browser.close();
})();
