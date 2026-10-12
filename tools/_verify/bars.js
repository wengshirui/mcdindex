const { chromium } = require('playwright');
const { pathToFileURL } = require('url');
const fs = require('fs');
const path = require('path');
// 与 verify.js 一致：路径从脚本位置推导，不写死本机绝对路径
const DIR = __dirname + path.sep;
const SHOT = path.join(DIR, '_shots') + path.sep;
const PAGE = pathToFileURL(path.resolve(__dirname, '..', '..', 'demo', 'index.html')).href;

(async () => {
  const browser = await chromium.launch({ channel: 'msedge' });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 3 });
  await page.goto(PAGE);
  await page.waitForTimeout(600);
  await page.fill('#q', '苏州');
  await page.waitForTimeout(400);

  const bars = await page.$$eval('#out .dim', els => els.map(e => {
    const f = e.querySelector('.fill'), t = e.querySelector('.track');
    const cs = getComputedStyle(f);
    return {
      label: e.querySelector('span').textContent,
      inlineW: f.style.width,
      boxW: Math.round(f.getBoundingClientRect().width),
      trackW: Math.round(t.getBoundingClientRect().width),
      bg: cs.backgroundColor,
      h: Math.round(f.getBoundingClientRect().height),
    };
  }));

  const el = await page.$('#out .dims');
  await el.screenshot({ path: SHOT + 'zoom-bars.png' });
  fs.writeFileSync(DIR + '_bars.json', JSON.stringify(bars, null, 2), 'utf8');
  process.stdout.write(JSON.stringify(bars, null, 1) + '\n');
  await browser.close();
})();
