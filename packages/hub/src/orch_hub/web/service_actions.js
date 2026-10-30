// HUB-SV-02: what an admin signs to start, stop or restart a part (see services/helper_proto.py). Pure: the digest is
// recomputed here from the context the helper made, and the sentence shown must be the one the context binds
// (text_sha256), so the words on screen and the bytes signed cannot differ. The signing itself stays in app.js.
import { cj, hex, sha256, utf8 } from "./cj.js";

export const TAG = "orch.hub.service.action/v1";
const CTX_KEYS = "action channel_id hub_id host_id kind nonce part target_version text_sha256 user_id iat".split(" ").sort().join(" ");
export const VERBS = { start: "Start", stop: "Stop", restart: "Restart" };

/** The digest to sign. Throws Error("event_changed" | "malformed") unless the context is exactly this part, this action,
 *  this person and this browser's key, the shown text is the bound one, and the helper's digest equals ours. */
export async function checkServiceAction(prepared, { part, action, userId, channelId }) {
  const ctx = prepared?.ctx;
  if (ctx === null || typeof ctx !== "object" || Array.isArray(ctx) || Object.keys(ctx).sort().join(" ") !== CTX_KEYS) throw new Error("malformed");
  if (typeof prepared.text !== "string" || typeof prepared.digest !== "string") throw new Error("malformed");
  let digest, textHash;
  try {
    digest = await sha256(utf8(TAG + "\n" + cj(ctx)));
    textHash = hex(await sha256(utf8(prepared.text)));
  } catch {
    throw new Error("malformed");
  }
  if (ctx.kind !== TAG || ctx.part !== part || ctx.action !== action || ctx.target_version !== null) throw new Error("event_changed");
  if (ctx.user_id !== userId || ctx.channel_id !== channelId || textHash !== ctx.text_sha256) throw new Error("event_changed");
  if (!/^[0-9a-f]{32}$/.test(ctx.nonce) || hex(digest) !== prepared.digest) throw new Error("event_changed");
  return digest;
}

export const resultWords = {
  start: "started",
  stop: "stopped",
  restart: "restarted",
};
