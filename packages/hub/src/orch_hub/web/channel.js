// This browser's channel key (design-a8): a non-extractable P-256 key in IndexedDB, and the two typed things it signs.
// There is no "sign these bytes" export: signRegistration takes a challenge object it has checked field by field,
// signProof takes the 32-byte digest the caller computed from the context it rendered. The private key is never
// exported, never sent, and never leaves IndexedDB.
import { b64u, cj, hex, sha256, unb64u, utf8 } from "./cj.js";

export const LABEL_REG = "orch/v2/hub-channel-reg|";
export const LABEL_PROOF = "orch/v2/hub-channel-proof|";
export const LABEL_HUB_REMOVE = "orch/v2/hub-remove|"; // the console digest of "Remove hub" (protocol.md, hub_remove_digest)
const LABEL_CHANNEL_ID = "orch/v2/id/channel|"; // H(label || suite byte 2 || key_pub)[:16], protocol.md section 2.3
const ECDSA = { name: "ECDSA", hash: "SHA-256" };
const SKEW_MS = 120_000;

export class ChannelError extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

/** A new signing key. `extractable: false` applies to the private key; the public key can always be exported. */
export async function generateKey() {
  const pair = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, ["sign", "verify"]);
  const raw = new Uint8Array(await crypto.subtle.exportKey("raw", pair.publicKey)); // 65-byte uncompressed point
  return { pair, keyPub: b64u(raw), channelId: hex((await sha256(new Uint8Array([...utf8(LABEL_CHANNEL_ID), 2, ...raw]))).slice(0, 16)) };
}

/** The mode of the hub that served this page: the `dev` flag of its held card. A challenge must carry exactly this flag. */
export async function fetchHubDev() {
  const res = await fetch("/api/v1/hub/card", { credentials: "same-origin", cache: "no-store" });
  const dev = res.ok ? (await res.json())?.card?.o?.dev : undefined;
  if (typeof dev !== "boolean") throw new ChannelError("challenge_invalid");
  return dev;
}

/** What the browser verifies about a registration challenge before it signs it (protocol.md 6.3).
 * `me.dev` is the serving hub's own mode: a dev challenge is never accepted by a real hub page, nor the reverse. */
export function checkChallenge(o, me, now) {
  const bad = () => {
    throw new ChannelError("challenge_invalid");
  };
  const keys = "attestation channel_kind dev expires_ms hub_id hv key_pub kind nonce suite user_id v";
  if (o === null || typeof o !== "object" || Object.keys(o).sort().join(" ") !== keys) bad();
  if (o.v !== 2 || o.suite !== 2 || o.hv !== 0 || o.kind !== "hub.channel_register") bad();
  if (typeof me.dev !== "boolean" || o.dev !== me.dev) bad();
  if (o.channel_kind !== "browser" || o.attestation !== "session") bad();
  if (o.key_pub !== me.keyPub || o.user_id !== me.userId) bad();
  if (me.hubId && o.hub_id !== me.hubId) bad();
  if (typeof o.hub_id !== "string" || !/^[0-9a-f]{32}$/.test(o.hub_id)) bad();
  if (!Number.isSafeInteger(o.expires_ms) || o.expires_ms > now + 120_000 + SKEW_MS) bad();
  try {
    unb64u(o.nonce, 32);
  } catch {
    bad();
  }
}

/** Signs the registration object after checkChallenge passed. Returns {o, sig} as /channels/register/complete wants it. */
export async function signRegistration(privateKey, o, me, now) {
  checkChallenge(o, { ...me, dev: me.dev ?? (await fetchHubDev()) }, now);
  const sig = new Uint8Array(await crypto.subtle.sign(ECDSA, privateKey, utf8(LABEL_REG + cj(o))));
  return { o, sig: b64u(sig) };
}

/** The digest the admin signs to remove the hub: SHA-256("orch/v2/hub-remove|" || cj(ctx)), computed here from the context
 * the hub returned. The page never signs a digest the hub sends; it compares it with this one and refuses on a mismatch. */
export async function hubRemoveDigest(ctx) {
  if (ctx === null || typeof ctx !== "object" || Array.isArray(ctx)) throw new ChannelError("malformed");
  return sha256(utf8(LABEL_HUB_REMOVE + cj(ctx)));
}

/** The custodial proof over a digest this page computed itself: Sign(key, "orch/v2/hub-channel-proof|" || digest). */
export async function signProof(privateKey, digest) {
  if (!(digest instanceof Uint8Array) || digest.length !== 32) throw new ChannelError("malformed");
  const msg = new Uint8Array(LABEL_PROOF.length + 32);
  msg.set(utf8(LABEL_PROOF));
  msg.set(digest, LABEL_PROOF.length);
  return b64u(new Uint8Array(await crypto.subtle.sign(ECDSA, privateKey, msg)));
}

// --- IndexedDB: one row per origin (this origin is one hub) -----------------------------------------------------------

const DB = "orch-hub";
const STORE = "channel";
const ROW = "this-browser";

function open() {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === "undefined") return reject(new ChannelError("storage_unavailable"));
    const r = indexedDB.open(DB, 1);
    r.onupgradeneeded = () => r.result.createObjectStore(STORE);
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(new ChannelError("storage_unavailable"));
  });
}

function run(mode, fn) {
  return open().then(
    (db) =>
      new Promise((resolve, reject) => {
        const tx = db.transaction(STORE, mode);
        const req = fn(tx.objectStore(STORE));
        tx.oncomplete = () => {
          db.close();
          resolve(req.result);
        };
        tx.onerror = tx.onabort = () => {
          db.close();
          reject(new ChannelError("storage_unavailable"));
        };
      }),
  );
}

/** The stored {pair, keyPub, channelId, userId, hubId, label} or null. Any storage failure reads as "no key here". */
export async function loadChannel() {
  try {
    return (await run("readonly", (s) => s.get(ROW))) ?? null;
  } catch {
    return null;
  }
}

/** Stores the row only after the hub's certificate was accepted (staged, then committed). */
export function saveChannel(row) {
  return run("readwrite", (s) => s.put(row, ROW));
}

/** Deletes the key. Called only after the hub said channel_revoked, or on "Remove this browser" (A8-5). */
export function deleteChannel() {
  return run("readwrite", (s) => s.delete(ROW));
}
