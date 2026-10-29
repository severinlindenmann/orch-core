// Small DOM helpers shared by the Services and Workspaces pages (createElement and textContent only, see tests/js/lint.test.mjs).
import { renderText } from "./render.js";

export function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v != null && v !== false) el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const child of kids.flat()) if (child != null && child !== false) el.append(child.nodeType ? child : document.createTextNode(String(child)));
  return el;
}
export const text = (v) => renderText(String(v ?? ""));
/** A workspace's display name for the admin pages: its own name, else the name the rail holds, else "<owner>'s workspace", else "A workspace" (never an id). */
export const wsLabel = (w, rows = []) => {
  const id = w?.workspace_id;
  const own = typeof w?.name === "string" && w.name ? w.name : null;
  const row = rows.find((r) => r.id === id);
  const railName = row && row.name && row.name !== String(id).slice(0, 8) ? row.name : null;
  const owner = typeof w?.owner_name === "string" && w.owner_name ? w.owner_name + "'s workspace" : null;
  return own || railName || owner || "A workspace";
};
export const withNames = (list, rows = []) => (Array.isArray(list) ? list.map((w) => ({ ...w, name: wsLabel(w, rows) })) : list);
export const chip = (label, kind = "") => h("span", { class: "chip " + kind, text: label });
export const dot = (kind) => h("span", { class: "dot " + kind, "aria-hidden": "true" });

const pad = (n) => String(n).padStart(2, "0");
/** "Today 09:12", "Yesterday 17:55", "12 Oct 09:12": the viewer's local time. */
export function when(ms, now = Date.now()) {
  if (!Number.isFinite(ms)) return "";
  const d = new Date(ms), t = new Date(now), hm = pad(d.getHours()) + ":" + pad(d.getMinutes());
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(t) - day(d)) / 86400000);
  if (diff === 0) return "Today " + hm;
  if (diff === 1) return "Yesterday " + hm;
  return d.getDate() + " " + d.toLocaleString("en", { month: "short" }) + " " + hm;
}
/** "just now", "2 min ago", "3 h ago", "2 days ago". */
export function ago(ms, now = Date.now()) {
  if (!Number.isFinite(ms)) return "";
  const s = Math.max(0, Math.round((now - ms) / 1000));
  if (s < 10) return "just now";
  if (s < 60) return s + " s ago";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago";
  const d = Math.round(s / 86400);
  return d + (d === 1 ? " day ago" : " days ago");
}

/** Agent identity never falls back to the person who started it. */
export function actorLabel(actor) {
  if (!actor || typeof actor !== "object" || Array.isArray(actor)) return "unknown";
  if (actor.kind === "agent") {
    const session = actor.session_id ?? actor.session;
    return typeof session === "string" && session.trim() ? "agent:" + text(session) : "agent";
  }
  if (actor.kind === "person") return typeof actor.name === "string" && actor.name.trim() ? text(actor.name) : "person";
  if (actor.kind === "host") return "host";
  return "unknown";
}
