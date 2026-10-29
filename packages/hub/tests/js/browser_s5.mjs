// S5 in a real browser (design-a8): sign in, register this browser, review a question, tamper check, sign it.
// Driven by tests/test_browser_s5.py, which brings up a wired hub and passes HUB_URL, HUB_USER, HUB_PW and two TOTP
// codes (the sign-in code is one step older than the registration code: codes are single use).
// Run under the playwright lease: ~/orch-dev/scripts/lease.sh run playwright a8 -- uv run pytest tests/test_browser_s5.py
import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";

const { chromium } = createRequire(import.meta.url)("playwright");
const { HUB_URL, HUB_USER, HUB_PW, CODE_SIGNIN, CODE_REGISTER, SHOT_DIR } = process.env;
const assert = (c, m) => { if (!c) throw new Error("S5: " + m); };

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } });
const page = await ctx.newPage();
const cspViolations = [];
page.on("console", (m) => { if (/Content Security Policy|Trusted Type/i.test(m.text())) cspViolations.push(m.text()); });
page.on("pageerror", (e) => cspViolations.push(String(e)));
const shot = async (name, width) => {
  if (!SHOT_DIR) return;
  mkdirSync(SHOT_DIR, { recursive: true });
  if (width) await page.setViewportSize({ width, height: width < 600 ? 844 : 900 });
  await page.screenshot({ path: `${SHOT_DIR}/a8-${name}-${width ?? 1400}.png` });
};

try {
  // static page: headers
  const res = await page.goto(HUB_URL + "/");
  assert(res.headers()["content-security-policy"]?.includes("script-src 'self'"), "CSP header missing");
  await page.getByRole("heading", { name: "Sign in to your hub" }).waitFor();

  // the hub origin serves nothing else
  const other = await page.request.get(HUB_URL + "/etc/passwd");
  assert(other.status() === 404 && other.headers()["content-type"] === "application/json", "other path not JSON 404");

  await page.fill("#user", HUB_USER);
  await page.fill("#pw", HUB_PW);
  await page.fill("#code", CODE_SIGNIN);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByText("This browser cannot sign yet").waitFor();
  await shot("home-unregistered");

  // register: needs password + a later code again
  await page.getByRole("button", { name: "Register this browser" }).click();
  await page.getByRole("heading", { name: "Register this browser" }).waitFor();
  await page.fill("#pw2", HUB_PW);
  await page.fill("#code2", CODE_SIGNIN); // the code that signed us in must be refused
  await page.getByRole("button", { name: "Register this browser" }).click();
  await page.getByText("Browser not registered").waitFor();
  await page.fill("#pw2", HUB_PW);
  await page.fill("#code2", CODE_REGISTER);
  await shot("register");
  await page.getByRole("button", { name: "Register this browser" }).click();
  await page.getByText("This browser is ready").waitFor({ timeout: 3000 }); // the success banner is shown, not just the card below it
  await page.getByText("This browser holds a signing key for you").waitFor();

  // the key is non-extractable and in IndexedDB
  const keyInfo = await page.evaluate(async () => {
    const db = await new Promise((ok, no) => { const r = indexedDB.open("orch-hub", 1); r.onsuccess = () => ok(r.result); r.onerror = no; });
    const row = await new Promise((ok) => { const g = db.transaction("channel").objectStore("channel").get("this-browser"); g.onsuccess = () => ok(g.result); });
    let exported = "no";
    try { await crypto.subtle.exportKey("pkcs8", row.pair.privateKey); exported = "yes"; } catch { /* expected */ }
    return { extractable: row.pair.privateKey.extractable, exported };
  });
  assert(keyInfo.extractable === false && keyInfo.exported === "no", "private key must not be extractable");

  if (process.env.S5_STOP === "register") {
    console.log("S5 browser: ok (registered)");
    await browser.close();
    process.exit(0);
  }

  // review the question and sign it
  await page.getByText("CSV or API?").first().waitFor();
  await page.getByRole("button", { name: "Review" }).first().click();
  await page.getByRole("button", { name: "Answer" }).first().click();
  const sign = page.locator("#sign");
  await sign.waitFor();
  await page.getByText("Review your answer").waitFor();
  const sentence = await page.locator("#dlg .sentence").innerText();
  assert(/answer question Q1 on ticket .* with .CSV/.test(sentence), "sentence: " + sentence);
  await shot("sign-prompt");
  await shot("sign-prompt", 390);
  assert(await sign.isDisabled(), "Sign must be disabled right after render");
  // a script's click never signs (isTrusted) and a disabled button cannot be clicked
  await page.evaluate(() => document.getElementById("sign").click());
  await page.waitForTimeout(300);
  assert(await page.getByText("Review your answer").isVisible(), "a synthetic click must not sign");
  await page.waitForTimeout(1100);
  assert((await sign.innerText()) === "Sign and send", "button reads Sign and send once armed");
  await sign.click();
  await page.getByText("Signed and sent").waitFor();
  await shot("signed");

  assert(cspViolations.length === 0, "CSP/Trusted Types violations: " + cspViolations.join(" | "));
  console.log("S5 browser: ok");
} finally {
  await browser.close();
}
