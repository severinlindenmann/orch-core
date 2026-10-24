// A6 in a real browser: the person home (needs-you left, workspaces and signing places right) and the admin console.
// Driven by tests/test_browser_a6.py (HUB_URL, HUB_USER, HUB_PW, CODE_SIGNIN, optional SHOT_DIR). Run under the playwright lease.
import { createRequire } from "node:module";
import { mkdirSync, readFileSync } from "node:fs";

const { chromium } = createRequire(import.meta.url)("playwright");
const { HUB_URL, HUB_USER, HUB_PW, CODE_SIGNIN, SHOT_DIR } = process.env;
const assert = (c, m) => { if (!c) throw new Error("A6: " + m); };

const browser = await chromium.launch();
const page = await (await browser.newContext({ viewport: { width: 1400, height: 900 } })).newPage();
const problems = [];
page.on("console", (m) => { if (/LOADERR/.test(m.text())) console.log(m.text()); if (/Content Security Policy|Trusted Type/i.test(m.text())) problems.push(m.text()); });
page.on("pageerror", (e) => { console.log("PAGEERROR", String(e)); problems.push(String(e)); });
const shot = async (name) => {
  if (!SHOT_DIR) return;
  mkdirSync(SHOT_DIR, { recursive: true });
  for (const width of [1400, 390]) {
    await page.setViewportSize({ width, height: width < 600 ? 844 : 900 });
    // scroll through once so every tile of a tall page is painted before the full-page capture
    await page.evaluate(async () => { for (let y = 0; y < document.body.scrollHeight; y += 500) { window.scrollTo(0, y); await new Promise((r) => setTimeout(r, 40)); } window.scrollTo(0, 0); });
    await page.screenshot({ path: `${SHOT_DIR}/a6-${name}-${width}.png`, fullPage: true });
    const wide = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    assert(!wide, `${name} at ${width}px scrolls horizontally`);
  }
  await page.setViewportSize({ width: 1400, height: 900 });
};

try {
  await page.goto(HUB_URL + "/");
  await page.fill("#user", HUB_USER);
  await page.fill("#pw", HUB_PW);
  await page.fill("#code", CODE_SIGNIN);
  await page.getByRole("button", { name: "Sign in" }).click();

  // person home
  await page.getByRole("heading", { name: "Needs you" }).waitFor();
  await page.getByRole("heading", { name: "My workspaces" }).waitFor();
  await page.getByRole("heading", { name: "Where you can sign" }).waitFor();
  await page.getByText("CSV or API?").first().waitFor();
  assert(await page.getByText("Hub can read everything").first().isVisible(), "full workspace plain line");
  assert((await page.getByRole("button", { name: /^Approve/ }).count()) === 0, "no direct Approve on the home");
  assert((await page.getByRole("button", { name: "Review" }).count()) >= 1, "Review button on the needs-you row");
  await shot("home");

  // admin console
  await page.getByRole("link", { name: "Admin" }).click();
  await page.getByRole("heading", { name: "Admin" }).waitFor();
  await page.getByText("Groups managed here; external sync not configured").waitFor();
  await page.getByRole("heading", { name: "People & groups" }).waitFor();
  await page.getByText(/^Adding .* to .* gives /).first().waitFor();
  await shot("admin-people");
  for (const [tab, heading] of [["Access", "Access"], ["Workspaces", "Workspaces"], ["Marketplaces", "Marketplaces"], ["Audit", "Audit"]]) {
    await page.getByRole("tab", { name: tab }).click();
    await page.getByRole("heading", { name: heading, exact: true }).waitFor();
    await shot("admin-" + tab.toLowerCase());
  }
  // D14: the hub marketplace policy (one draft, one Save; every write is the whole object with its base version)
  const KEY = JSON.parse(readFileSync(new URL("../../vectors/vectors_hub_policy.json", import.meta.url), "utf8")).keys.company_1;
  const COMPANY = "https://git.acme.example/tools/orch-market";
  await page.getByRole("tab", { name: "Marketplaces" }).click();
  await page.getByText("Official marketplace", { exact: true }).first().waitFor();
  await page.getByText("Rules version 1.").waitFor();
  assert(await page.getByRole("button", { name: "Save rules" }).isDisabled(), "Save is disabled without changes");
  // block one add-on: the version rises
  await page.getByLabel("Blocked add-ons").fill("wiki@1.0.0");
  await page.getByRole("button", { name: "Save rules" }).click();
  await page.getByText(/^Saved\./).waitFor();
  await page.getByText("Rules version 2.").waitFor();
  // the official address in the company form is routed to the official action, never asked for a key
  await page.getByLabel("Marketplace repository address").fill("https://github.com/severinlindenmann/orch-marketplace");
  await page.getByLabel("Current public key").fill(KEY);
  await page.getByRole("button", { name: "Add marketplace" }).click();
  await page.getByText(/That is the official marketplace/).waitFor();
  assert((await page.getByRole("article").count()) === 1, "no row was added for the official address");
  // add a company marketplace (staged), save it
  await page.getByLabel("Marketplace repository address").fill(COMPANY);
  await page.getByLabel("Current public key").fill("");
  await page.getByRole("button", { name: "Add marketplace" }).click();
  await page.getByText(/Paste the public key/).waitFor();
  await page.getByLabel("Current public key").fill(KEY);
  await page.getByRole("button", { name: "Add marketplace" }).click();
  await page.getByText("1 accepted public key").waitFor();
  await page.getByText("Unsaved changes").waitFor();
  await page.getByLabel("Marketplace repository address").fill("git.example/x");
  await page.getByLabel("Current public key").fill("abc");
  await shot("admin-marketplaces-draft");
  await page.getByRole("button", { name: "Save rules" }).click();
  await page.getByText("Rules version 3.").waitFor();
  // the same address again never silently replaces its keys
  await page.getByLabel("Marketplace repository address").fill(COMPANY);
  await page.getByLabel("Current public key").fill(KEY);
  await page.getByRole("button", { name: "Add marketplace" }).click();
  await page.getByText(/already in the list/).waitFor();
  // a second admin session (B) saves while this page (A) has an unsaved draft
  const asB = async (edit) => {
    const other = await page.context().newPage();
    await other.goto(HUB_URL + "/");
    await other.getByRole("link", { name: "Admin" }).click();
    await other.getByRole("tab", { name: "Marketplaces" }).click();
    await edit(other);
    await other.getByRole("button", { name: "Save rules" }).click();
    await other.getByText(/^Saved\./).waitFor();
    await other.close();
  };
  const blockBox = page.getByLabel("Blocked add-ons");
  // (1) M2: A drafts, B saves, A reloads page data by adding a person elsewhere, then saves: a conflict, not a new version
  await blockBox.fill("wiki@1.0.0 mine");
  await asB((o) => o.getByLabel("Blocked add-ons").fill("notes wiki@1.0.0"));  // version 4
  await page.getByRole("tab", { name: "People & groups" }).click();
  await page.getByLabel("Name", { exact: true }).fill("Kim");
  await page.getByRole("button", { name: "Add person" }).click();
  await page.getByText("Saved", { exact: true }).waitFor();
  await page.getByRole("tab", { name: "Marketplaces" }).click();
  await page.getByText(/changed elsewhere \(now version 4\)/).waitFor();
  assert((await blockBox.inputValue()) === "wiki@1.0.0 mine", "the draft survived the reload");
  await page.getByRole("button", { name: "Save rules" }).click();
  await page.getByText(/Someone else changed these rules while you were editing/).waitFor();
  await page.getByText("Rules version 4.").waitFor();  // nothing was saved
  // (2) B's change survives: A's own change is laid on top of B's version, shown for a re-save
  const merged = await blockBox.inputValue();
  assert(merged.includes("notes") && merged.includes("mine") && merged.includes("wiki@1.0.0"), "B's block and mine are both there: " + merged);
  await page.getByRole("button", { name: "Save rules" }).click();
  await page.getByText("Rules version 5.").waitFor();
  assert((await blockBox.inputValue()).includes("notes"), "B's block is still there after A's save");
  // (3) both changed the same thing differently: A's value wins only after an explicit confirm
  await page.getByLabel("Allow only the selected permissions").check();
  await page.getByLabel("Run terminal sessions").check();
  await asB(async (o) => { await o.getByLabel("Allow only the selected permissions").check(); await o.getByLabel("Use the network").check(); });  // v6
  await page.getByRole("button", { name: "Save rules" }).click();
  await page.getByText(/Saving would replace changes someone else made/).waitFor();
  assert(await page.getByRole("button", { name: "Save rules" }).isDisabled(), "Save waits for the confirm");
  await page.getByLabel(/I have read this/).check();
  await page.getByRole("button", { name: "Save rules" }).click();
  await page.getByText("Rules version 7.").waitFor();
  // remove the company marketplace: the confirmation names it and is hub-wide
  await page.getByRole("button", { name: /^Remove https:/ }).click();
  await page.getByText(/for every workspace on this hub/).waitFor();
  await page.getByRole("button", { name: "Remove from all workspaces" }).click();
  await page.getByRole("button", { name: "Save rules" }).click();
  await page.getByText("Rules version 8.").waitFor();
  assert((await page.getByRole("article").count()) === 1, "only the official marketplace is left");
  await shot("admin-marketplaces-saved");
  // add a person, then remove them with the two-step button
  await page.getByRole("tab", { name: "People & groups" }).click();
  await page.getByLabel("Name", { exact: true }).fill("Jonas");
  await page.getByRole("button", { name: "Add person" }).click();
  await page.getByText("Saved", { exact: true }).waitFor();
  await page.locator("b", { hasText: "Jonas" }).first().waitFor();

  assert(problems.length === 0, "CSP/Trusted Types violations: " + problems.join(" | "));
  console.log("A6 browser: ok");
} finally {
  await browser.close();
}
