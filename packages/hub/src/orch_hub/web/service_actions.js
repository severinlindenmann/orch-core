// HUB-SV-02: what an admin signs to start, stop or restart a part (see services/helper_proto.py). Pure: the digest is
// recomputed here from the context the helper made, and the sentence shown must be the one the context binds
// (text_sha256), so the words on screen and the bytes signed cannot differ. The signing itself stays in app.js.
import { cj, hex, sha256, utf8 } from "./cj.js";
import { h } from "./ui.js";

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

export const LOG_NOTE = "Secrets are hidden as far as we can tell; don't paste logs publicly.";

/** True when pressing `verb` on `name` must first ask a second plain click (only stopping the Hub itself). */
export const needsConfirm = (name, verb) => name === "hub" && verb === "stop";

/** The second click before the Hub stops itself. Nothing is sent until `onYes` runs. */
export function hubStopConfirm({ onYes, onCancel }) {
  return [h("h1", { text: "Stop the Hub?" }),
    h("p", { class: "sentence", text: "This console and every workspace link go offline. Workspaces keep working on their own; you start the hub again on the server." }),
    h("div", { class: "row" }, h("button", { text: "Cancel", onclick: onCancel }), h("button", { class: "dan", id: "hub-stop-confirm", text: "Yes, stop the Hub", onclick: onYes }))];
}

/** The Logs dialog's nodes. The log is set with textContent only, never parsed as HTML. */
export function logsDialog(name, logText, { copy, onClose }) {
  const body = logText || "Nothing was logged in the last hour.";
  const pre = h("pre", { class: "mono small logs", id: "log-text" });
  pre.textContent = body;
  const header = "orch " + name + " log, last hour, " + new Date().toISOString() + "\n";
  return [h("h1", { text: "Logs: " + name }), pre,
    h("p", { class: "small muted", id: "log-note", text: LOG_NOTE }),
    h("div", { class: "row" }, h("button", { id: "log-copy", text: "Copy for IT", onclick: () => copy(header + body) }), h("button", { text: "Close", onclick: onClose }))];
}
