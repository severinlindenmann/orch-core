// P10: after the first admin is created the page shows a QR (the key behind "Can't scan?") and the hub opens from it.
import test from "node:test";
import assert from "node:assert/strict";

class Node_ {
  constructor(tag, text) { this.tag = tag; this.nodeType = tag === "#text" ? 3 : 1; this.children = []; this.attrs = {}; this._text = text ?? ""; this.className = ""; this.listeners = {}; }
  append(...kids) { for (const k of kids) this.children.push(k && k.nodeType ? k : new Node_("#text", String(k))); }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener(k, f) { this.listeners[k] = f; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this.tag === "#text" ? this._text : this._text + this.children.map((c) => c.textContent).join(""); }
  all() { return [this, ...this.children.flatMap((c) => c.all())]; }
}
globalThis.document = {
  createElement: (tag) => new Node_(tag), createTextNode: (t) => new Node_("#text", t),
  createElementNS: (ns, tag) => { assert.equal(ns, "http://www.w3.org/2000/svg"); return new Node_(tag); },
};
const { totpPanel, KEY_GONE_NOTE } = await import("../../src/orch_hub/web/totp.js");
const { encode, qrSvg } = await import("../../src/orch_hub/web/qr.js");

const URI = "otpauth://totp/orch-hub:Admin?secret=JBSWY3DPEHPK3PXP&issuer=orch-hub";

test("the QR encoder makes a square module matrix with the three finder patterns", () => {
  const { data, size } = encode(URI, { ecc: "M", border: 0 });
  assert.equal(data.length, size);
  assert.ok(size >= 25 && (size - 17) % 4 === 0);
  for (const [x, y] of [[0, 0], [size - 7, 0], [0, size - 7]]) {
    assert.ok(data[y][x] && data[y + 6][x + 6] && data[y + 3][x + 3] && !data[y + 1][x + 1]);
  }
});

test("qrSvg builds an svg with createElementNS only", () => {
  const svg = qrSvg(URI);
  assert.equal(svg.tag, "svg");
  assert.match(svg.getAttribute("viewBox"), /^0 0 (\d+) \1$/);
  assert.ok(svg.all().some((n) => n.tag === "path" && n.getAttribute("d").startsWith("M")));
});

test("the panel shows the QR and the key right under it, visible, with no fold", () => {
  const out = totpPanel(URI, "JBSWY3DPEHPK3PXP");
  const all = out.all();
  assert.ok(all.some((n) => n.tag === "svg"));
  assert.ok(!all.some((n) => n.tag === "details" || n.tag === "summary"), "no folded key");
  assert.match(out.textContent, /Can't scan\? Type this key into your app instead\./);
  assert.equal(all.find((n) => n.attrs.id === "new-totp").textContent, "JBSWY3DPEHPK3PXP");
  assert.doesNotMatch(out.textContent, /only once|not see this key again/, "the gone-note lives beside the continue button");
  assert.ok(!out.textContent.match(/user id/i), "no user id: the name signs in");
  assert.ok(!out.textContent.includes("Write these down"));
  assert.equal(KEY_GONE_NOTE, "You will not see this key again after this page.");
});

test("without a usable uri the key is shown with a plain label", () => {
  const out = totpPanel("", "KEY234");
  assert.ok(!out.all().some((n) => n.tag === "svg" || n.tag === "details"));
  assert.match(out.textContent, /Authenticator key/);
  assert.match(out.textContent, /KEY234/);
});

test("app.js: after /setup the page offers Open the hub (afterLogin), not a second sign-in", async () => {
  const { readFileSync } = await import("node:fs");
  const src = readFileSync(new URL("../../src/orch_hub/web/app.js", import.meta.url), "utf8");
  const setup = src.slice(src.indexOf("function setupView"), src.indexOf("async function refresh"));
  assert.match(setup, /text: "I've added it, open the hub", onclick: \(\) => afterLogin\(\)/);
  assert.equal((setup.match(/KEY_GONE_NOTE/g) || []).length, 1, "one gone-note beside the button");
  assert.ok(!/console\.|localStorage|sessionStorage|location\.(hash|search)|document\.title/.test(setup.slice(setup.indexOf("totpPanel"))), "the key never goes to a log, storage, URL or title");
  assert.ok(!/Write these down|Continue to sign in|loginView\(\)/.test(setup));
  assert.match(setup, /totpPanel\(r\.body\.totp_uri/);
});

test("the key has a Copy button where the clipboard exists, and none where it does not", async () => {
  const old = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  let written;
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { clipboard: { writeText: async (v) => { written = v; } } } });
  try {
    const out = totpPanel(URI, "JBSWY3DPEHPK3PXP");
    const copy = out.all().find((n) => n.tag === "button" && n.textContent === "Copy");
    assert.ok(copy, "Copy next to the key");
    await copy.listeners.click();
    assert.equal(written, "JBSWY3DPEHPK3PXP");
    assert.equal(copy.textContent, "Copied ✓");
    assert.equal(out.all().find((n) => n.attrs.id === "new-totp").textContent, "JBSWY3DPEHPK3PXP", "the key is still shown as text");
    Object.defineProperty(globalThis, "navigator", { configurable: true, value: {} });
    assert.ok(!totpPanel(URI, "K").all().some((n) => n.tag === "button"), "no clipboard: no button, the key stays selectable");
  } finally {
    if (old) Object.defineProperty(globalThis, "navigator", old); else delete globalThis.navigator;
  }
});
