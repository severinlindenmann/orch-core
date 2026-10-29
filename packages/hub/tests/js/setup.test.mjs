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
const { totpPanel } = await import("../../src/orch_hub/web/totp.js");
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

test("the panel shows the QR first and the key behind Can't scan", () => {
  const out = totpPanel(URI, "JBSWY3DPEHPK3PXP", { userId: "ab".repeat(16) });
  const all = out.all();
  assert.ok(all.some((n) => n.tag === "svg"));
  const details = all.find((n) => n.tag === "details");
  assert.equal(details.getAttribute("open"), null, "the key is folded while a QR is shown");
  assert.match(details.textContent, /Can't scan\? Show the key/);
  assert.equal(all.find((n) => n.attrs.id === "new-totp").textContent, "JBSWY3DPEHPK3PXP");
  assert.ok(details.all().some((n) => n.attrs.id === "new-user-id"), "the user id stays reachable, folded");
  assert.ok(!out.textContent.includes("Write these down"));
});

test("without a usable uri the key is shown open, so nothing needed is hidden", () => {
  const out = totpPanel("", "KEY234");
  assert.ok(!out.all().some((n) => n.tag === "svg"));
  assert.equal(out.all().find((n) => n.tag === "details").getAttribute("open"), "");
});

test("app.js: after /setup the page offers Open the hub (afterLogin), not a second sign-in", async () => {
  const { readFileSync } = await import("node:fs");
  const src = readFileSync(new URL("../../src/orch_hub/web/app.js", import.meta.url), "utf8");
  const setup = src.slice(src.indexOf("function setupView"), src.indexOf("async function refresh"));
  assert.match(setup, /text: "Open the hub", onclick: \(\) => afterLogin\(\)/);
  assert.ok(!/Write these down|Continue to sign in|loginView\(\)/.test(setup));
  assert.match(setup, /totpPanel\(r\.body\.totp_uri/);
});
