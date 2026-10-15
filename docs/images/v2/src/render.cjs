// Renders diagrams.html into ../<name>-<theme>.png (2x, transparent background).
// Needs Playwright with a Chrome channel: node render.cjs [playwright module path]
const path = require("path");
const { chromium } = require(process.argv[2] || "playwright");
(async () => {
  const b = await chromium.launch({ channel: "chrome" });
  for (const d of ["problems", "flow", "gates", "architecture"]) for (const t of ["dark", "light"]) {
    const p = await b.newPage({ viewport: { width: 1700, height: 1100 }, deviceScaleFactor: 2 });
    await p.goto("file://" + path.join(__dirname, "diagrams.html") + `?d=${d}&t=${t}`);
    await p.waitForSelector("body[data-ready]"); await p.waitForTimeout(300);
    await p.locator("#" + d).screenshot({ path: path.join(__dirname, "..", `${d}-${t}.png`), omitBackground: true });
    await p.close(); console.log(d, t);
  }
  await b.close();
})();
