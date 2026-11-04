// Seals a company secret in the browser to one workspace's exchange key (protocol.md 7.5, purpose "org-secret").
// WebCrypto only: ECDH P-256 with a fresh NON-EXTRACTABLE ephemeral key per call, HKDF-SHA-256, AES-256-GCM with a zero
// nonce (the key is unique per call). The only export is sealOrgSecret; the value is never stored here.
const SUITE_ID = 2;
const PURPOSE = "org-secret";
const AAD_LABEL = "orch/v2/seal-aad|";
const KDF_LABEL = "orch/v2/seal";
const VALUE_MIN = 8;
const VALUE_MAX = 4096;
const NAME = /^[a-z][a-z0-9-]{0,39}$/;
const HEX16 = /^[0-9a-f]{32}$/;
const enc = new TextEncoder();

function valueProblem(value) {
  if (typeof value !== "string") return "bad_character";
  if (value.length < VALUE_MIN) return "too_short";
  if (value.length > VALUE_MAX) return "too_long";
  if (!/^[\x20-\x7e]+$/.test(value)) return "bad_character";
  if (value[0] === " " || value[value.length - 1] === " ") return "edge_space";
  return null;
}

function id16(hex) {
  if (typeof hex !== "string" || !HEX16.test(hex)) throw new Error("malformed");
  const out = new Uint8Array(16);
  for (let i = 0; i < 16; i++) out[i] = parseInt(hex.slice(2 * i, 2 * i + 2), 16);
  return out;
}

function uint(n, bits) {
  if (!Number.isSafeInteger(n) || n < 0 || BigInt(n) >= 1n << BigInt(bits)) throw new Error("malformed");
  return BigInt(n);
}

function concat(...parts) {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) { out.set(p, at); at += p.length; }
  return out;
}

function u32(n) { const b = new Uint8Array(4); new DataView(b.buffer).setUint32(0, Number(uint(n, 32))); return b; }
function u64(n) { const b = new Uint8Array(8); new DataView(b.buffer).setBigUint64(0, uint(n, 64)); return b; }

function wrapAadExtra(hubId, workspaceId, wxkVersion, expiresMs, name) {
  if (typeof name !== "string" || !NAME.test(name)) throw new Error("malformed");
  const n = enc.encode(name);
  return concat(id16(hubId), id16(workspaceId), u32(wxkVersion), u64(expiresMs), Uint8Array.of(n.length), n);
}

function sealAad(secretId, version, extra) {
  const p = enc.encode(PURPOSE);
  return concat(enc.encode(AAD_LABEL), Uint8Array.of(SUITE_ID, p.length), p, id16(secretId), u32(version), extra);
}

// ephPriv: a CryptoKey for ECDH; ephPub: its 65-byte public point. The served path passes a non-extractable key.
async function sealWith(ephPriv, ephPub, f) {
  if (valueProblem(f.value) !== null) throw new Error("value");
  if (!(f.wxkPub instanceof Uint8Array) || f.wxkPub.length !== 65 || f.wxkPub[0] !== 4) throw new Error("malformed");
  const extra = wrapAadExtra(f.hubId, f.workspaceId, f.wxkVersion, f.expiresMs, f.name);
  const aad = sealAad(f.secretId, f.version, extra);
  const rcpt = await crypto.subtle.importKey("raw", f.wxkPub, { name: "ECDH", namedCurve: "P-256" }, false, []);
  const ss = new Uint8Array(await crypto.subtle.deriveBits({ name: "ECDH", public: rcpt }, ephPriv, 256));
  const ikm = await crypto.subtle.importKey("raw", ss, "HKDF", false, ["deriveBits"]);
  const info = enc.encode(`${KDF_LABEL}|${PURPOSE}|${f.workspaceId}`);
  const keyBytes = await crypto.subtle.deriveBits({ name: "HKDF", hash: "SHA-256", salt: concat(ephPub, f.wxkPub), info }, ikm, 256);
  const key = await crypto.subtle.importKey("raw", keyBytes, "AES-GCM", false, ["encrypt"]);
  const ct = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv: new Uint8Array(12), additionalData: aad, tagLength: 128 }, key, enc.encode(f.value)));
  return concat(ephPub, ct);
}

// A wrap that is already expired, or that outlives the 366-day cap, is refused before any key is made. Only this served
// entry checks the clock; the test seam calls sealWith directly with the fixed historical expiries of the vectors.
const EXPIRY_MAX_MS = 366 * 86400000;

export async function sealOrgSecret(f) {
  const now = Date.now();
  if (!Number.isSafeInteger(f.expiresMs) || f.expiresMs <= now || f.expiresMs > now + EXPIRY_MAX_MS) throw new Error("malformed");
  const eph = await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, false, ["deriveBits"]);
  const ephPub = new Uint8Array(await crypto.subtle.exportKey("raw", eph.publicKey));
  return sealWith(eph.privateKey, ephPub, f);
}
