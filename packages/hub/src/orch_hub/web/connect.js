// The /c page (P13): the connect link in the address fragment becomes one button, "Open my workspace".
// Everything here is fixed-shape: the fragment is parsed with the same grammar as connect.parse_link (hub/connect.py),
// anything that does not match in full is ignored, and the deep link is rebuilt from the parsed parts, never taken raw.
// The fragment never reaches a server. The workspace only PREFILLS its join dialog; its owner still signs.

const HEX32 = /^[0-9a-f]{32}$/;
const CODE = /^[A-Za-z0-9_-]{1,64}$/;
const ENDPOINT = /^(local|(\[[0-9a-fA-F:.]{2,45}\]|[A-Za-z0-9.-]{1,253}):[0-9]{1,5}|wss?:\/\/(\[[0-9a-fA-F:.]{2,45}\]|[A-Za-z0-9.-]{1,253})(:[0-9]{1,5})?)$/;
// r11 P03: remembered PER PERSON (hub user id -> {o: loopback origin, n: display name}). A link is only ever offered to the
// dashboard remembered for the person it was minted for; there is no default port and no "last opened" origin.
const KEY = "orch.connect_people";
const MAX_PEOPLE = 50;

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

/** Only a workspace dashboard on THIS computer is ever a target: the connect code must not leave it (a workspace host
 *  reports its own address to the hub, so a remembered non-loopback address is not trusted). */
export function loopbackOrigin(value) {
  const o = cleanOrigin(value);
  if (!o) return null;
  const host = new URL(o).hostname;
  return host === "127.0.0.1" || host === "localhost" || host === "[::1]" ? o : null;
}

/** The address of Settings > Hub in the workspace dashboard, with the link in the fragment (it stays on that computer). */
export const deepLink = (dashboardOrigin, link) => {
  const o = loopbackOrigin(dashboardOrigin);
  return o ? o + "/settings/hub#connect=" + encodeURIComponent(link) : null;
};

function readPeople() {
  try {
    const v = JSON.parse(globalThis.localStorage.getItem(KEY) ?? "{}");
    return v && typeof v === "object" && !Array.isArray(v) ? v : {};
  } catch { return {}; }
}
function writePerson(owner, patch) {
  if (typeof owner !== "string" || !HEX32.test(owner)) return;
  try {
    const all = readPeople();
    const entry = { ...(all[owner] && typeof all[owner] === "object" ? all[owner] : {}), ...patch };
    delete all[owner];
    all[owner] = entry; // newest last; the oldest fall out
    const keys = Object.keys(all);
    for (const k of keys.slice(0, Math.max(0, keys.length - MAX_PEOPLE))) delete all[k];
    globalThis.localStorage.setItem(KEY, JSON.stringify(all));
  } catch { /* private window */ }
}
/** Written by the hub's Open button for a workspace the signed-in person OWNS (app.js), keyed by that person's hub user id. */
export function rememberDashboard(url, ownerUser) {
  const o = loopbackOrigin(url);
  if (o) writePerson(ownerUser, { o });
}
/** The person's name, written by the hub when it makes a connect link for them; display text only. */
export function rememberName(ownerUser, name) {
  if (typeof name === "string" && name.trim() && name.length <= 80) writePerson(ownerUser, { n: name.trim() });
}
/** The dashboard remembered for exactly this person, or null (no owner, nothing remembered, or a bad stored value). */
export function knownDashboard(ownerUser) {
  if (typeof ownerUser !== "string" || !HEX32.test(ownerUser)) return null;
  const e = readPeople()[ownerUser];
  return e && typeof e === "object" ? loopbackOrigin(e.o) : null;
}
export function knownName(ownerUser) {
  if (typeof ownerUser !== "string" || !HEX32.test(ownerUser)) return null;
  const n = readPeople()[ownerUser]?.n;
  return typeof n === "string" && n && n.length <= 80 ? n : null;
}

/** What the /c page shows for a fragment, or null for anything that does not parse (the static text stays). */
export function pageModel(hash, origin, host) {
  const parsed = parseFragment(hash);
  if (!parsed) return null;
  const link = canonicalLink(origin, parsed);
  const name = knownName(parsed.ownerUser);
  const open = deepLink(knownDashboard(parsed.ownerUser), link);
  return {
    link, open,
    title: name ? "Connect " + name + "'s workspace" : "Connect your workspace to " + host,
    lead: open ? "One button opens your workspace on this computer with the Connect a hub dialog already filled in. You only press Connect."
      : name ? "Open this page on " + name + "'s computer, then press the button." : "Open this page on the computer of the workspace, then press the button.",
  };
}

function render(root, hash) {
  const m = pageModel(hash, location.origin, location.host);
  if (!m) return; // keep the static text
  const el = (tag, props = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) { if (k === "text") n.textContent = v; else if (k === "class") n.className = v; else n[k] = v; }
    for (const kid of kids) n.append(kid);
    return n;
  };
  const PASTE = "Then open your workspace, go to Settings, then Hub, and paste it there. Nothing else to type.";
  const copyHint = el("p", { class: "small muted", text: m.open ? "" : PASTE });
  const copy = el("button", { type: "button", class: m.open ? "" : "pri", text: m.open ? "Copy the link instead" : "Copy the link", onclick: async () => {
    try { await navigator.clipboard.writeText(m.link); copyHint.textContent = "Copied. " + PASTE; }
    catch { copyHint.textContent = "Could not copy. Select the address of this page and copy it."; }
  } });
  root.replaceChildren(
    el("h1", { text: m.title }),
    el("p", { text: m.lead }),
    m.open ? el("p", {}, el("a", { class: "pri", id: "open-workspace", href: m.open, text: "Open my workspace" })) : "",
    el("p", {}, copy), copyHint,
    el("p", { class: "small muted", text: "The link works once and expires after 30 minutes." }));
}

if (typeof document !== "undefined" && typeof location !== "undefined" && document.getElementById?.("connect-page")) {
  render(document.getElementById("connect-page"), location.hash);
}
