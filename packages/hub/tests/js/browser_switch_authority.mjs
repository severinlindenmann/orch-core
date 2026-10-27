// Run 12 in a real browser: trusted-browser switch is full with no prompt; a fresh profile is read-mostly with a
// structured step-up; a replayed hub session cannot change anything; revoking downgrades. Driven by
// tests/test_switch_authority_browser.py (real hub, real dashboard, two origins).
import { createRequire } from "node:module";

const { chromium } = createRequire(import.meta.url)("playwright");
const { DASH_ORIGIN, LAUNCH_CODE, TICKET_TRUSTED, TICKET_FRESH, WS } = process.env;
const assert = (c, m) => { if (!c) throw new Error("switch authority: " + m); };

const browser = await chromium.launch();
const api = (page, method, path, token, body) => page.evaluate(async ([method, path, token, body]) => {
  const r = await fetch(path, { method, credentials: "omit", headers: { Authorization: "Bearer " + token, ...(body === undefined ? {} : { "content-type": "application/json" }) }, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await r.text();
  let json = null; try { json = JSON.parse(text); } catch {}
  return { status: r.status, json };
}, [method, path, token, body]);
const WRITE = `/api/w/${WS}/worktrees/remove`;
const stepUp = (r) => r.status === 403 && r.json?.error?.code === "step_up_required";
const tokenOf = (page) => page.evaluate(() => sessionStorage.getItem("orch-session"));
const land = async (page, ticket) => {
  await page.goto(`${DASH_ORIGIN}/#t=${ticket}`);
  await page.getByRole("button", { name: "Continue" }).click();
  // the confirm page leaves once the ticket is used AND the browser's key had its say (then the app loads)
  await page.waitForFunction(() => !document.body.textContent.includes("Continue from your hub"), null, { timeout: 20000 });
  await page.waitForFunction(() => !!sessionStorage.getItem("orch-session"), null, { timeout: 15000 });
};

try {
  // 1. a real sign-in on this computer (launch link): the browser enrols its key
  const trusted = await browser.newContext();
  const page = await trusted.newPage();
  await page.goto(`${DASH_ORIGIN}/#launch=${LAUNCH_CODE}`);
  await page.waitForFunction(() => !!sessionStorage.getItem("orch-session"), null, { timeout: 15000 });
  const local = await tokenOf(page);
  let rows = [];
  for (let i = 0; i < 40 && rows.length === 0; i++) {
    const r = await api(page, "GET", "/api/trusted", local);
    rows = r.status === 200 ? r.json : [];
    if (!rows.length) await page.waitForTimeout(250);
  }
  assert(rows.length === 1, "the real sign-in enrolled one trusted browser");
  const keyId = rows[0].id;

  // 2. same browser: a hub switch lands FULL, with only the 'Continue' click (no other prompt)
  const switcher = await trusted.newPage();
  await land(switcher, TICKET_TRUSTED);
  const full = await tokenOf(switcher);
  assert(full && full !== local, "the switch produced its own session");
  assert((await api(switcher, "GET", "/api/trusted", full)).status === 200, "same browser: full session (it may list trusted browsers)");
  assert(!stepUp(await api(switcher, "POST", WRITE, full, {})), "same browser: a change is not asked to step up");

  // 3. a fresh profile: read-mostly
  const fresh = await (await browser.newContext()).newPage();
  await land(fresh, TICKET_FRESH);
  const hubMade = await tokenOf(fresh);
  assert((await api(fresh, "GET", `/api/w/${WS}/hub/rail`, hubMade)).status === 200, "fresh profile: reads work");
  for (const [m, p] of [["POST", WRITE], ["POST", `/api/w/${WS}/terminals/open`], ["POST", `/api/w/${WS}/click/sign`], ["GET", "/api/trusted"]]) {
    assert(stepUp(await api(fresh, m, p, hubMade, m === "POST" ? {} : undefined)), `fresh profile: ${m} ${p} gets step_up_required`);
  }
  assert((await api(fresh, "POST", "/api/trusted/enrol", hubMade, { jwk: {} })).status === 403, "a switch session enrols no key");

  // 4. the hub-made session replayed from the trusted browser (loopback, same machine) still changes nothing
  for (const p of [WRITE, `/api/w/${WS}/terminals/open`]) assert(stepUp(await api(switcher, "POST", p, hubMade, {})), `replay: ${p} refused`);
  const challenge = await api(switcher, "POST", "/api/auth/challenge", hubMade, {});
  assert(challenge.status === 200, "replay: a challenge may be asked");
  const bad = await api(switcher, "POST", "/api/auth/upgrade", hubMade, { key_id: keyId, signature: "A".repeat(86) });
  assert(bad.status === 401, "replay: the trusted key's id without its signature upgrades nothing");

  // 5. revoke: the session the key upgraded goes back to read-mostly
  assert((await api(switcher, "DELETE", `/api/trusted/${keyId}`, full)).status === 200, "revoked");
  assert(stepUp(await api(switcher, "POST", WRITE, full, {})), "after revoke the upgraded session is read-mostly again");
  console.log("switch authority browser: ok");
} finally {
  await browser.close();
}
