// HUB-SV-02: what an admin signs to start, stop or restart a part (see services/helper_proto.py). Pure: the digest is
// recomputed here from the context the helper made, and the sentence shown must be the one the context binds
// (text_sha256), so the words on screen and the bytes signed cannot differ. The signing itself stays in app.js.
import { cj, hex, sha256, utf8 } from "./cj.js";
import { h } from "./ui.js";
import { NAME } from "./services.js";

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

const LOG_LINES = 300, LOG_BYTES = 256 * 1024 - 1024; // the helper returns at most this many lines / bytes (helper_backends.LOG_TAIL, LOG_MAX_BYTES)
export const LOG_NOTE = "Secrets are hidden as far as we can tell; don't paste logs publicly.";

/** What the hub-stop dialog adds under the signed sentence (the sentence itself is the helper's and stays bound). */
export const HUB_STOP_NOTE = "Workspaces keep working on their own. You start the hub again on the server.";

/** The title and the sign button: "Stop Relay", or "Stop the Hub" for the console itself. */
export const stopLabel = (name, verb) => name === "hub" && verb === "stop" ? "Stop the Hub" : VERBS[verb] + " " + (NAME[name] ?? name);

/** Run `resume` now when this browser holds a signing key; otherwise open the register step, which calls `resume` once
 *  the key is stored (P04: the admin stays where they are and the same action goes on). */
export function withKey(hasKey, openRegister, resume) {
  return hasKey() ? resume() : openRegister(resume);
}

/** The one dialog before signing. `sign` is the arm-delayed button made by the caller; nothing is sent from here. */
export function serviceDialog(name, verb, sentence, { sign, onCancel }) {
  const hubStop = name === "hub" && verb === "stop";
  return [h("h1", { text: stopLabel(name, verb) + "?" }),
    h("p", { class: "sentence", text: sentence }),
    hubStop ? h("p", { class: "small muted", id: "hub-stop-note", text: HUB_STOP_NOTE }) : null,
    h("div", { class: "row" }, h("button", { text: "Cancel", onclick: onCancel }), sign)].filter(Boolean);
}

const LINE = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?([+-]\d{2}):?(\d{2}|Z)?\s+\S+\s+([^\s:[]+)(?:\[\d+\])?:\s?(.*)$/;
const localTime = (d) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

/** The raw (already redacted) journal lines as short plain events. systemd's own Started / Stopped / Failed lines become
 *  "Started" / "Stopped" / "Stopped unexpectedly"; the benign "Starting", "Stopping" and "Deactivated successfully" lines fold away;
 *  every other line is kept as its message (unit, host and pid stripped), so an error is never hidden.
 *  Returns [{ time, text, ok }] where ok is false for anything that is not a plain start or stop. */
export function summarize(raw, { fmt = localTime, now = new Date() } = {}) {
  const out = [];
  const day = (d) => d.toDateString();
  for (const line of String(raw ?? "").split("\n")) {
    if (!line.trim()) continue;
    const m = LINE.exec(line);
    let when = "", msg = line.trim();
    if (m) {
      const d = new Date(m[1] + (m[3] === undefined ? "Z" : m[2] + ":" + (m[3] === "Z" ? "00" : m[3])));
      if (!Number.isNaN(d.getTime())) when = fmt(d) + (day(d) === day(now) ? "" : ", " + d.toLocaleDateString([], { day: "numeric", month: "short" }));
      msg = m[5].trim();
      if (m[4].startsWith("systemd")) {
        msg = msg.replace(/^[\w.@-]+\.service: /, "");
        if (/^(Deactivated successfully\.|Consumed .*)$/.test(msg)) continue;
        if (/^Starting\b/.test(msg)) { out.push({ time: when, text: "Starting", ok: true }); continue; }
        if (/^Started\b/.test(msg)) {
          if (out.at(-1)?.text === "Starting") out.pop();
          out.push({ time: when, text: "Started", ok: true }); continue;
        }
        if (/^Stopped\b/.test(msg)) {
          if (out.at(-1)?.text === "Stopping") out.pop();
          out.push({ time: when, text: "Stopped", ok: true }); continue;
        }
        if (/^Stopping\b/.test(msg)) { out.push({ time: when, text: "Stopping", ok: true }); continue; }
        if (/^Failed to start\b/.test(msg)) { out.push({ time: when, text: "Could not start", ok: false }); continue; }
        if (/^Failed\b/.test(msg)) { out.push({ time: when, text: "Stopped unexpectedly", ok: false }); continue; }
      }
    }
    out.push({ time: when, text: msg, ok: false });
  }
  return out;
}

/** The Logs dialog's nodes: the short list first, the raw lines in a closed "Technical lines" fold. Everything from the
 *  log is set with textContent only, never parsed as HTML. "Copy for IT" copies the header and the raw lines. */
export function logsDialog(name, logText, { copy, onClose, fmt, now }) {
  const events = summarize(logText, { fmt, now });
  const list = h("ul", { class: "events", id: "log-events" });
  for (const e of events) {
    const li = h("li", {});
    li.append(h("span", { class: "muted small", text: e.time }), " ");
    const t = h("span", {});
    t.textContent = e.text;
    li.append(t);
    list.append(li);
  }
  const wrong = events.some((e) => !e.ok);
  const full = (logText || "").split("\n").length >= LOG_LINES || (logText || "").length >= LOG_BYTES; // the helper cut it: older lines may be missing
  let pre = null, fold = null;
  if (logText) {
    pre = h("pre", { class: "mono small logs", id: "log-text" });
    pre.textContent = logText;
    fold = h("details", { id: "log-raw" }, h("summary", { text: "Technical lines" }), pre);
  }
  const header = "orch " + name + " log, last 24 hours, " + new Date().toISOString() + "\n";
  return [h("h1", { text: (NAME[name] ?? name) + ": recent activity" }),
    logText ? list : h("p", { class: "muted", id: "log-empty", text: "Nothing happened in the last 24 hours." }),
    logText ? h("p", { class: "small muted", id: "log-span", text: full ? "Last " + LOG_LINES + " lines." : wrong ? "Last 24 hours." : "Last 24 hours. Nothing went wrong in this time." }) : null,
    fold,
    h("p", { class: "small muted", id: "log-note", text: LOG_NOTE }),
    h("div", { class: "row" }, h("button", { id: "log-copy", text: "Copy for IT", onclick: () => copy(header + (logText || "")) }), h("button", { text: "Close", onclick: onClose }))].filter(Boolean);
}
