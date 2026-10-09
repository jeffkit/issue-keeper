/**
 * shot-duty.js —— 值守总览页截图 + 样式自检（本地 playwright，无需浏览器扩展）
 * 用法: node shot-duty.js [url] [outPrefix]
 */
const PW = "/Users/kong/projects/infra4agent/browser-bridge/node_modules/playwright";
const { chromium } = require(PW);

(async () => {
  const url = process.argv[2] || "http://127.0.0.1:7433/?view=duty";
  const out = process.argv[3] || "/tmp/duty";
  const browser = await chromium.launch();
  const page = await browser.newPage({
    viewport: { width: 1680, height: 1000 },
    deviceScaleFactor: 2,
  });
  const errs = [];
  page.on("console", (m) => { if (m.type() === "error") errs.push(m.text().slice(0, 200)); });
  page.on("pageerror", (e) => errs.push("pageerror: " + String(e).slice(0, 200)));

  await page.goto(url, { waitUntil: "networkidle", timeout: 60000 });
  // 若深链未生效（旧构建），回退为点击 tab
  const hasDuty = await page.locator(".duty-view").count();
  if (!hasDuty) {
    await page.click("text=值守总览");
  }
  await page.waitForSelector(".duty-topo", { timeout: 30000 });
  await page.waitForTimeout(2500);

  await page.screenshot({ path: `${out}-top.png` });
  await page.screenshot({ path: `${out}-full.png`, fullPage: true });

  // 样式自检：卡片背景/文字色是否来自深色主题
  const probes = await page.evaluate(() => {
    const g = (sel, prop) => {
      const el = document.querySelector(sel);
      return el ? getComputedStyle(el)[prop] : null;
    };
    const rect = (sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { w: Math.round(r.width), h: Math.round(r.height) };
    };
    return {
      bodyBg: g("body", "backgroundColor"),
      bodyColor: g("body", "color"),
      cardBg: g(".duty-card", "backgroundColor"),
      cardColor: g(".duty-card .v", "color"),
      sectionH3: g(".duty-section h3", "color"),
      svgW: rect(".duty-topo"),
      docScrollW: document.documentElement.scrollWidth,
      winW: window.innerWidth,
      overflowX: document.documentElement.scrollWidth > window.innerWidth + 2,
    };
  });

  console.log(JSON.stringify({ errors: errs, probes }, null, 1));
  await browser.close();
})();
