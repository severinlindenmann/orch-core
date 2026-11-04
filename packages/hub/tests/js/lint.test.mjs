// The DOM is built from createElement and textContent only (design-a8 A8-8): Trusted Types are enforced in Chromium
// only, so the real control everywhere is that no HTML/script string sink appears in the console's own code.
import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";

const DIR = new URL("../../src/orch_hub/web/", import.meta.url);
const SINKS = [
  [/\binnerHTML\b/, "innerHTML"], [/\bouterHTML\b/, "outerHTML"], [/\binsertAdjacentHTML\b/, "insertAdjacentHTML"],
  [/\bdocument\s*\.\s*write(ln)?\b/, "document.write"], [/\beval\s*\(/, "eval("], [/\bnew\s+Function\b|\bFunction\s*\(/, "Function("],
  [/\bset(Timeout|Interval)\s*\(\s*["'`]/, "string timer"], [/\bsrcdoc\b/, "srcdoc"], [/javascript\s*:/i, "javascript:"],
  [/\bcreateContextualFragment\b/, "createContextualFragment"], [/\bDOMParser\b/, "DOMParser"], [/\.setHTML\b|\bsetHTMLUnsafe\b/, "setHTML"],
  [/\bimportScripts\b|\bnew\s+(Shared)?Worker\b/, "worker"], [/\blocalStorage\b|\bsessionStorage\b/, "web storage (key lives in IndexedDB only)"],
  [/\bexportKey\s*\(\s*["'](pkcs8|jwk)["']/, "exportKey of a private key"], [/\bsetAttribute\s*\(\s*["'](style|on\w+|href|src|srcdoc)["']/i, "dangerous attribute"],
  [/\bimport\s*\(/, "dynamic import"], [/https?:\/\//, "absolute URL (nothing third party)"],
];

test("the console's own files contain no HTML, script or storage sink", () => {
  const files = [...readdirSync(DIR).filter((f) => /\.(js|html|css)$/.test(f)), ...readdirSync(new URL("pages/", DIR)).map((f) => "pages/" + f)];
  assert.ok(files.includes("app.js") && files.includes("channel.js") && files.includes("pages/spaces.js"));
  for (const f of files) {
    const source = readFileSync(new URL(f, DIR), "utf8");
    // SVG namespace is an identifier, never a network request.
    const text = f === "pages/connections.js" || f === "qr.js" ? source.replaceAll('"http://www.w3.org/2000/svg"', '"svg-namespace"') : source;
    // connect.js alone remembers the (non-secret) address of the workspace dashboard in the browser, nothing else.
    for (const [rx, name] of SINKS) assert.ok((f === "connect.js" && name.startsWith("web storage")) || !rx.test(text), `${f}: ${name}`);
    if (f.endsWith(".html")) {
      assert.ok(!/<script(?![^>]*\bsrc=)/i.test(text), `${f}: inline script`);
      assert.ok(!/\son\w+\s*=|\sstyle\s*=|<style/i.test(text), `${f}: inline handler or style`);
    }
  }
});

test("the lint itself catches each sink", () => {
  for (const bad of ["a.innerHTML = x", "document.write(1)", "eval('1')", "new Function('x')", "setTimeout('x', 1)", "x.srcdoc = 1", "javascript:alert(1)"]) {
    assert.ok(SINKS.some(([rx]) => rx.test(bad)), bad);
  }
});
