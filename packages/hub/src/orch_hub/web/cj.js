// Canonical JSON and byte forms, exactly as orch-hub contract/canon.py (protocol-v2 section 2.2-2.3). Pure: no DOM, no
// storage, so `node --test` runs it against the hub vectors. Strict on purpose (design-a8 A8-9): JSON.parse rounds big
// integers and floats and accepts duplicate keys; this parser refuses all of them, so the object that is shown is the
// object that is hashed.

export const MAX_SAFE_INT = Number.MAX_SAFE_INTEGER;
export const MAX_DEPTH = 16;

export class CanonError extends Error {}

const LONE_SURROGATE = /[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/;
const ESC = { '"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t" };
const ASCII_KEY = /^[\x00-\x7f]+$/;

function str(s) {
  if (LONE_SURROGATE.test(s)) throw new CanonError("lone surrogate");
  let out = '"';
  for (const ch of s) {
    const e = ESC[ch];
    if (e !== undefined) out += e;
    else if (ch < " ") out += "\\u" + ch.charCodeAt(0).toString(16).padStart(4, "0");
    else out += ch;
  }
  return out + '"';
}

function ser(o, depth) {
  if (o === null) return "null";
  if (o === true) return "true";
  if (o === false) return "false";
  if (typeof o === "number") {
    if (!Number.isInteger(o) || Math.abs(o) > MAX_SAFE_INT) throw new CanonError("not a safe integer");
    return String(o === 0 ? 0 : o);
  }
  if (typeof o === "string") return str(o);
  if (Array.isArray(o)) {
    if (depth > MAX_DEPTH) throw new CanonError("nesting deeper than 16");
    return "[" + o.map((v) => ser(v, depth + 1)).join(",") + "]";
  }
  if (typeof o === "object") {
    const proto = Object.getPrototypeOf(o);
    if (proto !== Object.prototype && proto !== null) throw new CanonError("not a plain object");
    if (depth > MAX_DEPTH) throw new CanonError("nesting deeper than 16");
    const keys = Object.keys(o);
    for (const k of keys) if (k === "" || !ASCII_KEY.test(k)) throw new CanonError("object keys must be non-empty ASCII");
    keys.sort();
    return "{" + keys.map((k) => str(k) + ":" + ser(o[k], depth + 1)).join(",") + "}";
  }
  throw new CanonError("type is not in the canonical subset");
}

/** cj(o) as a string (the UTF-8 of it is what is hashed or signed). */
export function cj(o) {
  return ser(o, 1);
}

export function utf8(s) {
  return new TextEncoder().encode(s);
}

export function cjBytes(o) {
  return utf8(cj(o));
}

/** Strict parse of one JSON text into the canonical subset. Throws CanonError on anything else. */
export function parseStrict(text) {
  if (typeof text !== "string") throw new CanonError("not text");
  let i = 0;
  const bad = (m) => {
    throw new CanonError(m);
  };
  const ws = () => {
    while (i < text.length && (text[i] === " " || text[i] === "\t" || text[i] === "\n" || text[i] === "\r")) i++;
  };
  function string() {
    i++; // opening quote
    let out = "";
    for (;;) {
      if (i >= text.length) bad("unterminated string");
      const c = text[i++];
      if (c === '"') break;
      if (c < " ") bad("control character in string");
      if (c !== "\\") {
        out += c;
        continue;
      }
      const e = text[i++];
      if (e === "u") {
        const h = text.slice(i, i + 4);
        if (!/^[0-9a-fA-F]{4}$/.test(h)) bad("bad \\u escape");
        i += 4;
        out += String.fromCharCode(parseInt(h, 16));
      } else {
        const m = { '"': '"', "\\": "\\", "/": "/", b: "\b", f: "\f", n: "\n", r: "\r", t: "\t" }[e];
        if (m === undefined) bad("bad escape");
        out += m;
      }
    }
    if (LONE_SURROGATE.test(out)) bad("lone surrogate");
    return out;
  }
  function value(depth) {
    ws();
    const c = text[i];
    if (c === "{") {
      if (depth > MAX_DEPTH) bad("nesting deeper than 16");
      i++;
      const o = Object.create(null);
      ws();
      if (text[i] === "}") {
        i++;
        return o;
      }
      for (;;) {
        ws();
        if (text[i] !== '"') bad("object key expected");
        const k = string();
        if (k === "" || !ASCII_KEY.test(k)) bad("object keys must be non-empty ASCII");
        if (Object.prototype.hasOwnProperty.call(o, k)) bad("duplicate key");
        ws();
        if (text[i++] !== ":") bad("colon expected");
        o[k] = value(depth + 1);
        ws();
        const d = text[i++];
        if (d === "}") return o;
        if (d !== ",") bad("comma expected");
      }
    }
    if (c === "[") {
      if (depth > MAX_DEPTH) bad("nesting deeper than 16");
      i++;
      const a = [];
      ws();
      if (text[i] === "]") {
        i++;
        return a;
      }
      for (;;) {
        a.push(value(depth + 1));
        ws();
        const d = text[i++];
        if (d === "]") return a;
        if (d !== ",") bad("comma expected");
      }
    }
    if (c === '"') return string();
    if (text.startsWith("true", i)) return ((i += 4), true);
    if (text.startsWith("false", i)) return ((i += 5), false);
    if (text.startsWith("null", i)) return ((i += 4), null);
    const m = /^-?(0|[1-9][0-9]*)/.exec(text.slice(i, i + 40));
    if (!m) bad("unexpected character");
    const after = text[i + m[0].length];
    if (after === "." || after === "e" || after === "E") bad("floating-point number");
    if (m[0].replace("-", "").length > 16) bad("integer outside +-(2^53 - 1)");
    const n = Number(m[0]);
    if (!Number.isSafeInteger(n)) bad("integer outside +-(2^53 - 1)");
    i += m[0].length;
    return n === 0 ? 0 : n;
  }
  // objects are built without a prototype, so "__proto__" is a plain key and nothing inherits
  const v = value(1);
  ws();
  if (i !== text.length) bad("trailing characters");
  return v;
}

// --- bytes: canonical unpadded base64url and lower-case hex ---------------------------------------------------------

export function b64u(bytes) {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function unb64u(s, n) {
  if (typeof s !== "string" || !/^[A-Za-z0-9_-]*$/.test(s) || s.length % 4 === 1) throw new CanonError("not canonical base64url");
  const raw = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (s.length % 4)) % 4));
  const out = Uint8Array.from(raw, (c) => c.charCodeAt(0));
  if (b64u(out) !== s) throw new CanonError("non-canonical base64url");
  if (n !== undefined && out.length !== n) throw new CanonError("wrong length");
  return out;
}

export function hex(bytes) {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

export function unhex(s) {
  if (typeof s !== "string" || !/^([0-9a-f]{2})*$/.test(s)) throw new CanonError("not lower-case hex");
  return Uint8Array.from(s.match(/../g) ?? [], (h) => parseInt(h, 16));
}

export async function sha256(bytes) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
}
