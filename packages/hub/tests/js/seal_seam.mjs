// TEST-ONLY seam for web/seal.js: loads the SERVED source with internals appended (a second export path that no
// server ever sends), so tests run the very same code with a fixed ephemeral key and open wraps with WebCrypto.
import { readFileSync } from "node:fs";

export const SERVED = new URL("../../src/orch_hub/web/seal.js", import.meta.url);
const src = readFileSync(SERVED, "utf8") + "\nexport { sealWith, valueProblem, wrapAadExtra, sealAad, concat, enc, KDF_LABEL, PURPOSE, VALUE_MAX };\n";
export const internals = await import("data:text/javascript;base64," + Buffer.from(src).toString("base64"));

const hex = (h) => Uint8Array.from(h.match(/../g) ?? [], (b) => parseInt(b, 16));
const scalarJwk = async (scalarHex, pubHex) => {
  const pub = hex(pubHex);
  const b64u = (u) => Buffer.from(u).toString("base64url");
  return { kty: "EC", crv: "P-256", x: b64u(pub.slice(1, 33)), y: b64u(pub.slice(33)), d: b64u(hex(scalarHex)) };
};

// The ephemeral public point of a scalar: derived through an imported (extractable) JWK, test seam only.
export async function importPrivate(scalarHex, pubHex, usages = ["deriveBits"]) {
  return crypto.subtle.importKey("jwk", await scalarJwk(scalarHex, pubHex), { name: "ECDH", namedCurve: "P-256" }, true, usages);
}

// Public point from scalar via WebCrypto: no direct API, so the vector file carries wxk_pub only; for the ephemeral key the
// test computes the point with node:crypto (ECDH class).
import { createECDH } from "node:crypto";
export function pointOf(scalarHex) {
  const e = createECDH("prime256v1");
  e.setPrivateKey(Buffer.from(scalarHex, "hex"));
  return new Uint8Array(e.getPublicKey());
}

export async function sealFixed(ephScalarHex, f) {
  const ephPub = pointOf(ephScalarHex);
  const priv = await importPrivate(ephScalarHex, Buffer.from(ephPub).toString("hex"));
  return internals.sealWith(priv, ephPub, f);
}

// Opens a wrap exactly as the host does (AAD, KDF, value rule, expiry), for replaying the "open" cases.
export async function openFixed(privScalarHex, a, now, issued, EXPIRY_MAX_MS) {
  const { wrapAadExtra, sealAad, concat, enc, KDF_LABEL, PURPOSE, valueProblem } = internals;
  const refuse = (code) => Object.assign(new Error(code), { code });
  const extra = wrapAadExtra(a.hub_id, a.workspace_id, a.wxk_version, a.expires_ms, a.name);
  if (a.expires_ms <= now) throw refuse("expired");
  if (a.expires_ms > issued + EXPIRY_MAX_MS) throw refuse("horizon");
  const blob = hex(a.sealed);
  if (blob.length < 65 + 16) throw refuse("aead");
  const ephPub = blob.slice(0, 65), ct = blob.slice(65);
  const myPub = pointOf(privScalarHex);
  try {
    const priv = await importPrivate(privScalarHex, Buffer.from(myPub).toString("hex"));
    const peer = await crypto.subtle.importKey("raw", ephPub, { name: "ECDH", namedCurve: "P-256" }, false, []);
    const ss = new Uint8Array(await crypto.subtle.deriveBits({ name: "ECDH", public: peer }, priv, 256));
    const ikm = await crypto.subtle.importKey("raw", ss, "HKDF", false, ["deriveBits"]);
    const info = enc.encode(`${KDF_LABEL}|${PURPOSE}|${a.workspace_id}`);
    const kb = await crypto.subtle.deriveBits({ name: "HKDF", hash: "SHA-256", salt: concat(ephPub, myPub), info }, ikm, 256);
    const key = await crypto.subtle.importKey("raw", kb, "AES-GCM", false, ["decrypt"]);
    const pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv: new Uint8Array(12), additionalData: sealAad(a.secret_id, a.version, extra), tagLength: 128 }, key, ct);
    const bytes = new Uint8Array(pt);
    if (bytes.some((b) => b > 0x7f)) throw refuse("value");
    const value = new TextDecoder().decode(bytes);
    if (valueProblem(value) !== null) throw refuse("value");
    return value;
  } catch (e) {
    if (e.code) throw e;
    throw refuse("aead");
  }
}
