// The /c page (P13): the connect link in the address fragment becomes one button, "Open my workspace".
// Everything here is fixed-shape: the fragment is parsed with the same grammar as connect.parse_link (hub/connect.py),
// anything that does not match in full is ignored, and the deep link is rebuilt from the parsed parts, never taken raw.
// The fragment never reaches a server. The workspace only PREFILLS its join dialog; its owner still signs.

const HEX32 = /^[0-9a-f]{32}$/;
const CODE = /^[A-Za-z0-9_-]{1,64}$/;
const ENDPOINT = /^(local|(\[[0-9a-fA-F:.]{2,45}\]|[A-Za-z0-9.-]{1,253}):[0-9]{1,5})$/;
/** Where a workspace dashboard runs on this computer when the hub has not seen one open (the headless dashboard port). */
export const DEFAULT_DASHBOARD = "http:" + "//127.0.0.1:47821";
const KEY = "orch.dashboard_origin";

/** `#h1.<hub32>.<code>[.<endpoint>][~<owner32>]` -> {hubId, code, endpoint, ownerUser} or null. */
export function parseFragment(hash) {
  if (typeof hash !== "string" || !hash.startsWith("#")) return null;
  const [frag, ...rest] = hash.slice(1).split("~");
  if (rest.length > 1) return null;
  const owner = rest.length ? rest[0] : null;
  const parts = frag.split(".");
  if (parts.length < 3 || parts[0] !== "h1") return null;
  const hubId = parts[1], code = parts[2], endpoint = parts.length > 3 ? parts.slice(3).join(".") : null;
  if (!HEX32.test(hubId) || !CODE.test(code)) return null;
  if (endpoint !== null && !ENDPOINT.test(endpoint)) return null;
  if (owner !== null && !HEX32.test(owner)) return null;
  return { hubId, code, endpoint, ownerUser: owner };
}

/** The canonical connect link, rebuilt from the parsed parts on this hub's own origin. */
export const canonicalLink = (origin, p) =>
  origin + "/c#h1." + p.hubId + "." + p.code + (p.endpoint ? "." + p.endpoint : "") + (p.ownerUser ? "~" + p.ownerUser : "");

/** A plain http(s) origin without credentials, path or query; anything else is null. */
export function cleanOrigin(value) {
  if (typeof value !== "string") return null;
  let u;
  try { u = new URL(value); } catch { return null; }
  if ((u.protocol !== "http:" && u.protocol !== "https:") || u.username || u.password) return null;
  return u.origin;
}

/** The address of Settings > Hub in the workspace dashboard, with the link in the fragment (it stays on that computer). */
export const deepLink = (dashboardOrigin, link) => {
  const o = cleanOrigin(dashboardOrigin);
  return o ? o + "/settings/hub#connect=" + encodeURIComponent(link) : null;
};

/** Remembered by the hub's own Open button, read here; a failing store is the same as nothing remembered. */
export function rememberDashboard(url) {
  try { const o = cleanOrigin(url); if (o) globalThis.localStorage.setItem(KEY, o); } catch { /* private window */ }
}
export function knownDashboard() {
  try { return cleanOrigin(globalThis.localStorage.getItem(KEY)) ?? DEFAULT_DASHBOARD; } catch { return DEFAULT_DASHBOARD; }
}

function render(root, hash) {
  const parsed = parseFragment(hash);
  if (!parsed) return; // keep the static text
  const link = canonicalLink(location.origin, parsed);
  const open = deepLink(knownDashboard(), link);
  const el = (tag, props = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) { if (k === "text") n.textContent = v; else if (k === "class") n.className = v; else n[k] = v; }
    for (const kid of kids) n.append(kid);
    return n;
  };
  const note = el("p", { class: "small muted", text: "Nothing is sent from this page. The link works once and expires after 30 minutes." });
  const copyHint = el("p", { class: "small muted" });
  const copy = el("button", { type: "button", text: "Copy the link instead", onclick: async () => {
    try { await navigator.clipboard.writeText(link); copyHint.textContent = "Copied. In your workspace go to Settings, then Hub, then Connect a hub, and paste."; }
    catch { copyHint.textContent = "Could not copy. Select the address of this page and copy it."; }
  } });
  root.replaceChildren(
    el("h1", { text: "Connect your workspace to " + location.host }),
    el("p", { text: "One button opens your workspace on this computer with the Connect a hub dialog already filled in. You only press Connect." }),
    open ? el("p", {}, el("a", { class: "pri", id: "open-workspace", href: open, text: "Open my workspace" })) : null,
    el("p", {}, copy), copyHint, note);
}

if (typeof document !== "undefined" && typeof location !== "undefined" && document.getElementById?.("connect-page")) {
  render(document.getElementById("connect-page"), location.hash);
}
