import { skeleton, loading, enter, toast, watchDialogs, replaceContent, retryDelay, searchEntries, filterSearch, shortcut, focusIndex, busyButton, reconcileRows, shouldPoll } from "./feel.js";
// The hub console, minimum for the browser sign flow (design-a8): sign in, register this browser, see what needs me,
// review one decision and sign it. The DOM is built with createElement and textContent only (lint: tests/web_lint),
// every string from the workspace goes through renderText first, and nothing here calls a signer except the prompt.
import { CanonError, parseStrict } from "./cj.js";
import { ChannelError, deleteChannel, generateKey, loadChannel, saveChannel, signProof, signRegistration } from "./channel.js";
import { inboxView, reviewMeaning, signedMeaning } from "./inbox.js";
import { parseRoute, registerPages } from "./route.js";
import { PAGES } from "./pages.js";
import { firstRunOutgrown, homeView, sharedEnded, workspaceStatus } from "./home.js";
import { activityView } from "./activity.js";
import { accountFromWorkspaceView, accountView } from "./account.js";
import { adminView } from "./admin.js";
import { changesView, checkChange, sentence as changeSentence } from "./changes.js";
import { totpPanel } from "./totp.js";
import { inviteView } from "./invite.js";
import { rememberDashboard, rememberName } from "./connect.js";
import { servicesView } from "./services.js";
import { VERBS, checkServiceAction, resultWords } from "./service_actions.js";
import { handoffsView } from "./handoffs.js";
import { phonesView } from "./phones.js";
import { workspacesView, workspaceDetailsView, newWorkspaces } from "./workspaces.js";
import { OPEN_FIRST, colour, initials, landing, landingPlan, railRows, safeUrl } from "./switch.js";
import { Refusal, grantTermsOk, grantSentence, grantFacts, addonFacts, addonTermsOk, settingsFacts, settingsTermsOk, checkBindings, checkPrepared, describe, renderMulti, renderText, shortHash, verifyQuestion, DECISIONS } from "./render.js";

const root = document.getElementById("app");
watchDialogs();
const skip = h("a", { class: "skip-link", href: "#main-content", text: "Skip to main content", onclick: (event) => { event.preventDefault(); (document.getElementById("main-content") ?? root).focus(); } });
root.tabIndex = -1;
document.body.prepend(skip);
const state = { user: null, me: null, channels: [], newChannels: [], notice: null, rail: { hubName: "Hub", rows: [] }, menu: false };

// --- DOM helpers (no HTML strings anywhere) ----------------------------------------------------------------------------
function h(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k === "onclick" && props["data-sign"] !== undefined) e.addEventListener("click", (event) => busyButton(e, () => v(event)));
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, "");
    else if (v !== false && v != null) e.setAttribute(k, String(v));
  }
  for (const c of kids.flat()) if (c != null && c !== false) e.append(c.nodeType ? c : document.createTextNode(String(c)));
  if (props["data-sign"] !== undefined) { e.arm = () => arm(e); e.arm(); }
  return e;
}
let servicesTimer, workspacesTimer, viewRevision = 0;
let frame = null;
let railSnapshot = "";
let paintedRoute = null;
const show = (...nodes) => { clearInterval(servicesTimer); clearInterval(workspacesTimer); viewRevision++; frame = null; paintedRoute = null; root.classList.remove("shelled"); root.replaceChildren(...nodes.flat().filter(Boolean)); };
const field = (label, input) => h("div", {}, h("label", { text: label }), input);
const banner = (kind, title, text) => h("div", { class: "banner " + kind, role: kind === "bad" ? "alert" : "status" }, h("b", { text: title }), text ? h("div", { class: "small", text }) : null);

// --- API: strict parse, CSRF header, same origin cookie ----------------------------------------------------------------
async function api(method, path, body) {
  const headers = { "X-Orch-Csrf": "1" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch("/api/v1" + path, { method, headers, credentials: "same-origin", cache: "no-store", body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    connectionFailed();
    return { status: 0, body: { error: "unavailable" } };
  }
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(await res.arrayBuffer());
    if (res.status >= 500) connectionFailed();
    return { status: res.status, body: parseStrict(text) };
  } catch {
    return { status: res.status, body: { error: "malformed" } };
  }
}
const WORDS = {
  no_session: "Your sign-in ended. Sign in again.",
  bad_credentials: "Wrong user id, password or code. A code works once; wait for the next one.",
  rate_limited: "Too many tries. Wait a moment.",
  event_changed: "The request changed while you were deciding.",
  prepared_expired: "Time ran out.",
  unknown_prepared: "This request is no longer valid.",
  channel_revoked: "This browser's key was removed.",
  not_left: "This workspace must leave before it can be removed.",
  forbidden_role: "Your role does not allow this.",
  too_many_channels: "You have the maximum number of devices. Remove one first.",
  bad_channel_proof: "The signature was not accepted.",
  busy: "Another action is running. Try again in a moment.",
  helper_unavailable: "The service helper does not answer. Ask your IT person to start it on the server.",
  not_set_up: "The service helper is not set up on this server.",
  unknown_part: "This service is not managed from here.",
  bad_action: "That action is not allowed.",
  update_unavailable: "Updates from here need signed releases, which do not exist yet.",
  action_failed: "The server refused the command.",
  malformed: "The answer could not be read safely.",
  unavailable: "The hub cannot be reached.",
  bad_ticket: "This link expired. Go back and click again.",
  no_origin: "Open that workspace's dashboard once first, then try again.",
  stale_change: "Access to this workspace changed since this was asked, so the request was closed. Ask for it again.",
  unknown_change: "This request is no longer waiting for you.",
  not_pending: "This request has already been answered.",
  expired: "This request has lapsed. Ask for it again.",
  no_channel: "Set up this browser first (Home > Where you can sign).",
  owner_protected: "This would take away the workspace owner's own admin access, which is never allowed.",
  last_admin: "This is the last administrator. Make someone else an administrator first.",
  self_disable: "You cannot disable yourself.",
  conflict: "That is not possible in the person's current state.",
  bad_invite: "This link does not work. It may have been used already or it has expired.",
  proof_required: "Open this link in a browser where you already approve, or ask your admin for a recovery link.",
};
const say = (code) => WORDS[code] ?? "Refused (" + renderText(code) + ").";

// --- views -------------------------------------------------------------------------------------------------------------
function loginView(err) {
  clearSession();
  const id = h("input", { id: "user", autocomplete: "username", required: true });
  const pw = h("input", { id: "pw", type: "password", autocomplete: "current-password", required: true });
  const code = h("input", { id: "code", inputmode: "numeric", autocomplete: "one-time-code", maxlength: 6, required: true });
  const form = h("form", {
    onsubmit: async (ev) => {
      ev.preventDefault();
      const r = await api("POST", "/session", { user_id: id.value.trim(), password: pw.value, totp: code.value.trim(), client: "browser" });
      pw.value = code.value = "";
      if (r.status !== 200) return loginView(say(r.body.error === "malformed" ? "bad_credentials" : r.body.error)); // run-11 #11: a sign-in failure is never "could not be read safely"
      await afterLogin();
    },
  }, field("Your name", id), field("Password", pw), field("6-digit code", code), h("div", { class: "row top" }, h("button", { class: "pri", type: "submit", text: "Sign in" })));
  show(h("h1", { text: "Sign in to your hub" }), err ? banner("bad", "Not signed in", err) : null, form);
}

// First admin (prototype): the installer's one-time link opens this page; the hub shows the authenticator QR once and signs the new admin in.
function setupView(token, err) {
  const name = h("input", { id: "name", autocomplete: "name", required: true, value: "Admin" });
  const pw = h("input", { id: "pw", type: "password", autocomplete: "new-password", required: true, minlength: 10 });
  const form = h("form", {
    onsubmit: async (ev) => {
      ev.preventDefault();
      const r = await api("POST", "/setup", { token, name: name.value.trim(), password: pw.value });
      pw.value = "";
      if (r.status !== 200) return setupView(token, r.body.error === "bad_credentials" ? "This setup link was already used or is wrong." : say(r.body.error));
      history.replaceState(null, "", location.pathname);
      show(
        h("h1", { text: "Admin created" }),
        banner("ok", "You are signed in"),
        h("h2", { text: "Add your sign-in code to your phone" }),
        h("p", { class: "muted", text: "Scan this with your authenticator app. You will need the app's 6-digit code each time you sign in." }),
        totpPanel(r.body.totp_uri, r.body.totp_secret),
        h("div", { class: "row top" }, h("button", { class: "pri", id: "open-hub", text: "Open the hub", onclick: () => afterLogin() })),
      );
    },
  }, field("Your name", name), field("Choose a password (10 or more characters)", pw), h("div", { class: "row top" }, h("button", { class: "pri", type: "submit", text: "Create the admin" })));
  show(h("h1", { text: "Set up your hub" }), err ? banner("bad", "Not created", err) : null, h("p", { class: "muted", text: "This creates the first admin, with a password and an authenticator code." }), form);
}

/** Hub admin routes need a full sign-in; roles also need an admin or owner role in that workspace (the hub refuses the rest). */
const canAdmin = (w) => !state.user?.needs_sign_in && (w ? ["admin", "owner"].includes(w.role) : !!state.user?.is_admin);

async function refresh() {
  // A session that came from a workspace (needs_sign_in) is refused /me/channels: ask only when the hub will answer.
  const who = await api("GET", "/session");
  if (who.status !== 200) { clearSession(); return false; }
  const chans = who.body.needs_sign_in ? { status: 0, body: {} } : await api("GET", "/me/channels");
  if (state.user?.user_id !== who.body.user_id) clearSession();
  state.user = who.body;
  state.channels = chans.status === 200 ? chans.body.channels : [];
  state.newChannels = chans.status === 200 ? chans.body.new_channels ?? [] : [];
  state.me = await loadChannel();
  return true;
}

function pageLoading(active, kind = active) {
  if (frame && paintedRoute === location.hash) return;
  if (state.user) shell(active, skeleton(kind));
  else show(skeleton(kind));
}

function shell(active, ...nodes) {
  nodes = [recoveryBanner(), ...nodes];
  if (active !== "workspaces") clearInterval(workspacesTimer);
  if (active !== "services") { clearInterval(servicesTimer); viewRevision++; } // leave the Services page: stop its refresh
  const sameRoute = paintedRoute === location.hash;
  const tab = (id, label, cls) => h("a", { href: "#/" + id, "data-tab": id, class: cls + (active === id ? " on" : ""), "aria-current": active === id ? "page" : null, text: label, onclick: (e) => { e.preventDefault(); navigate("#/" + id); } });
  // The section tabs: orch's own, with every page module of pages/ slotted in after the tab it names (PAGES is generated
  // from the folder by the server; nothing here lists a page).
  const items = [["home", "Home"], ["approvals", "Approvals"], ["workspaces", "Workspaces"], ["phones", "Phones"], ["handoffs", "Hand-offs"], ["account", "Account"], ["services", "Services", true], ["activity", "Activity", true], ["admin", "Admin", true]].map(([id, label, admin]) => ({ id, label, admin: !!admin }));
  for (let todo = [...PAGES].sort((x, y) => (x.path < y.path ? -1 : 1)), moved = true; todo.length && moved;) {
    moved = false;
    for (const pg of [...todo]) {
      const at = items.findIndex((t) => t.id === pg.nav.after);
      if (at < 0) continue;
      items.splice(at + 1, 0, { id: pg.path, label: pg.nav.label, admin: pg.nav.admin });
      todo.splice(todo.indexOf(pg), 1); moved = true;
    }
  }
  for (const pg of PAGES) if (!items.some((t) => t.id === pg.path)) items.push({ id: pg.path, label: pg.nav.label, admin: pg.nav.admin }); // an unresolved nav.after is still reachable (pages.test.mjs fails on it)
  const tabs = (cls) => items.filter((t) => !t.admin || state.user?.is_admin).map((t) => tab(t.id, t.label, cls));
  const name = renderText(state.user?.name ?? "");
  const hubName = renderText(state.rail.hubName);
  const side = h("aside", { class: "side", "aria-label": "Hub" },
    h("div", { class: "logo" }, h("i"), h("span", { text: "orch" })),
    h("div", { class: "hubbox" }, h("span", { text: hubName })),
    ...tabs("nv"),
    h("div", { class: "sp" }), h("div", { class: "who" }, h("span", { text: name })));
  if (!frame) {
    const top = h("header", { class: "topbar" }, h("span", { class: "t breadcrumb", text: hubName }),
      h("nav", { class: "navtabs top-nav", "aria-label": "Sections" }, ...tabs("nav")),
      h("span", { class: "grow" }), h("button", { text: "Search ⌘K", onclick: () => openSearch() }), h("button", { text: "?", "aria-label": "Keyboard shortcuts", onclick: () => openShortcuts() }), h("span", { class: "small muted", text: name }), signOutMenu());
    show(h("div", { class: "app" }, railView(), side, h("div", { class: "main" }, top, h("div", { class: "content", id: "main-content", tabindex: "-1" }, nodes.flat(2)))));
    root.classList.add("shelled");
    frame = root.querySelector(".app");
    railSnapshot = JSON.stringify(state.rail);
  } else {
    if (!sameRoute || railSnapshot !== JSON.stringify(state.rail)) frame.querySelector(".side").replaceWith(side);
    replaceContent(frame.querySelector(".content"), nodes.flat(2).filter(Boolean), sameRoute);
    if (railSnapshot !== JSON.stringify(state.rail)) {
      frame.querySelector(".rail").replaceChildren(...railView().childNodes);
      railSnapshot = JSON.stringify(state.rail);
    }
  }
  for (const link of frame.querySelectorAll("[data-tab]")) {
    const selected = link.getAttribute("data-tab") === active;
    link.classList.toggle("on", selected);
    if (selected) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  if (!sameRoute) enter(frame.querySelector(".content"));
  paintedRoute = location.hash;
  const route = parseRoute(location.hash);
  for (const button of frame.querySelectorAll("[data-workspace]")) {
    const selected = button.getAttribute("data-workspace") === (route.workspace ?? "");
    button.classList.toggle("on", selected);
    if (selected) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  const menu = frame.querySelector(".menu");
  if (menu) menu.hidden = !state.menu;
  const workspace = state.rail.rows.find((w) => w.id === route.workspace);
  frame.querySelector(".breadcrumb").textContent = hubName + (workspace ? " › " + renderText(workspace.name) : "");
}

// After a recovery link reset the account the person sees who did it, until they dismiss it (GET /session: recovery).
function recoveryBanner() {
  const r = state.user?.recovery;
  if (!r) return null;
  const on = new Date(r.at_ms).toLocaleDateString();
  const box = h("div", { class: "banner warn recovery-notice", role: "status" },
    h("div", { text: renderText("Your account was reset by " + (r.by_name ?? "an administrator") + " on " + on + ". If you did not expect this, tell your IT person.") }),
    h("button", { type: "button", text: "Dismiss", onclick: async () => {
      const x = await api("POST", "/me/recovery/dismiss");
      if (x.status === 200) { state.user.recovery = null; box.remove(); }
    } }));
  return box;
}

// --- the company rail: this hub, then every workspace of mine (GET /rail); one click switches, no second sign-in ----------
function railView() {
  const hub = h("button", { class: "ra hub" + (parseRoute(location.hash).workspace ? "" : " on"), "data-workspace": "", "data-live-key": "hub", title: renderText(state.rail.hubName), "aria-label": "Hub " + renderText(state.rail.hubName), "aria-current": parseRoute(location.hash).workspace ? null : "page", text: initials(state.rail.hubName), onclick: () => navigate("#/home") });
  const items = state.rail.rows.map((w) => {
    const b = h("button", { "data-workspace": w.id, "data-live-key": w.id, class: "ra " + colour(w.id) + (w.online ? "" : " offline") + (parseRoute(location.hash).workspace === w.id ? " on" : ""), "aria-label": "Open " + renderText(w.name) + (w.online ? "" : " (offline)"),
      title: renderText(w.name), onclick: () => navigate("#/w/" + w.id) },
      initials(w.name, w.id), w.badge ? h("span", { class: "bd", text: String(w.badge) }) : null);
    return b;
  });
  return h("nav", { class: "rail", "aria-label": "Hub and workspaces" }, hub, items.length ? h("div", { class: "rsep" }) : null, items);
}

async function loadRail() {
  const user = state.user;
  let r = await api("GET", "/rail");
  if (r.status !== 200) r = await api("GET", "/me/workspaces"); // older hub without the rail: the plain list, nothing openable
  if (!user || state.user !== user) return;
  state.rail.rows = r.status === 200 ? railRows(r.body) : [];
  const n = r.status === 200 ? r.body.hub?.name : null;
  state.rail.hubName = typeof n === "string" && n ? n : "Hub";
}

async function openWorkspace(w) {
  const r = await api("POST", "/switch/ticket", { workspace_id: w.id, return_to: "/", kind: "open" });
  const url = r.status === 200 ? safeUrl(r.body.url) : null;
  if (!url) { state.notice = banner("bad", "Cannot open " + renderText(w.name), say(r.status === 200 ? "malformed" : r.body.error)); return home(); }
  // r11 P03: remembered for the signed-in person only when this is THEIR workspace (the hub's owner alias is them), so a
  // connect link minted for them can open it later; opening someone else's workspace never becomes anyone's target.
  if (state.rail.rows.find((x) => x.id === w.id)?.role === "owner") rememberDashboard(url, state.user?.user_id);
  location.assign(url);
}

// Sign-out: two named plain clicks, no question. "Sign out of <workspace> too" signs this hub out and lets that dashboard end its own session.
function signOutMenu() {
  const entries = [h("button", { text: "Sign out of " + renderText(state.rail.hubName), onclick: signOut })];
  for (const w of state.rail.rows.filter((x) => x.canOpen)) entries.push(h("button", { text: "Sign out of " + renderText(w.name) + " too", onclick: () => signOutAlso(w) }));
  const menu = h("div", { class: "menu", role: "menu", hidden: !state.menu }, entries.length > 1 ? h("div", { class: "hd", text: "SIGN OUT" }) : null, entries);
  return h("div", { class: "signout" }, h("button", { text: "Sign out", "aria-haspopup": "true", "aria-expanded": String(state.menu),
    onclick: () => { if (entries.length === 1) return signOut(); state.menu = !state.menu; menu.hidden = !state.menu; } }), menu);
}

async function signOutAlso(w) {
  const r = await api("POST", "/switch/ticket", { workspace_id: w.id, return_to: "/", kind: "signout" });
  const url = r.status === 200 ? safeUrl(r.body.url) : null;
  await signOut();
  if (url) location.assign(url);
}

// A ticket in the address (#t=...): strip it at once and NEVER sign in silently (login CSRF): ask the hub what the ticket is for,
// then either redeem (same person, or a sign-out), or let the person confirm.
const plain = (...kids) => show(h("div", { class: "plain" }, kids));
const expiredView = () => plain(h("h1", { text: "This link expired" }), h("p", { class: "muted", text: "This link expired. Go back and click again." }), h("button", { text: "Sign in with your password", onclick: () => loginView() }));

async function redeemTicket(t, replace) {
  const r = await api("POST", "/switch/redeem", replace ? { t, replace: true } : { t });
  const l = landing(r.status, r.body);
  if (l.kind === "signout") { clearSession(); return plain(h("h1", { text: "You are signed out" }), h("p", { class: "muted", text: "Your hub session ended." }), h("button", { class: "pri", text: "Sign in again", onclick: () => loginView() })); }
  if (l.kind === "expired") return expiredView();
  if (l.kind === "path") return location.replace(l.path);
  if (l.kind === "forward") {
    const o = await api("POST", "/switch/ticket", { workspace_id: l.workspace_id, return_to: "/", kind: "open" });
    const url = o.status === 200 ? safeUrl(o.body.url) : null;
    if (url) return location.assign(url);
  }
  await home();
}

async function landFromTicket(t) {
  history.replaceState(null, "", location.pathname);
  const p = await api("POST", "/switch/peek", { t });
  if (p.status !== 200) return expiredView();
  const name = renderText(p.body.name ?? "you");
  const plan = landingPlan(p.body);
  if (plan === "redeem") return redeemTicket(t, false);
  if (plan === "confirm") return plain(h("h1", { text: "Continue as " + name + "?" }), h("p", { class: "muted", text: "You opened the hub from your workspace. Nothing happens until you continue." }),
    h("div", { class: "row" }, h("button", { class: "pri", text: "Continue", onclick: () => redeemTicket(t, false) }), h("button", { text: "Cancel", onclick: () => loginView() })));
  const cur = renderText(p.body.signed_in_as?.name ?? "someone else");
  return plain(h("h1", { text: "Different person" }), h("p", { text: "You are signed in as " + cur + " here. This link is for " + name + "." }),
    h("div", { class: "row" }, h("button", { text: "Stay signed in as " + cur, onclick: () => home() }), h("button", { class: "pri", text: "Sign out and continue as " + name, onclick: () => redeemTicket(t, true) })));
}

// A session that came from a workspace ticket cannot use admin tools until a real password sign-in.
function reauthView() {
  show(h("div", { class: "plain" }, h("h1", { text: "Sign in again to use admin tools" }), h("p", { class: "muted", text: "You arrived from a workspace. Admin tools need your password and code." }),
    h("button", { class: "pri", text: "Sign in again", onclick: async () => { await api("DELETE", "/session"); clearSession(); loginView(); } })));
}

// P17: an admin whose session came from a workspace keeps "Connect a workspace" in reach. The click explains why one real
// sign-in is needed, then the normal login form (password + code) runs; nothing here grants a session.
function signInToAddWorkspace() {
  show(h("div", { class: "plain" }, h("h1", { text: "Sign in once to add a workspace" }), h("p", { class: "muted", text: "You arrived from a workspace. Adding a workspace needs your password and code. You come straight back here afterwards." }),
    h("button", { class: "pri", id: "sign-in-to-connect", text: "Sign in", onclick: async () => { await api("DELETE", "/session"); clearSession(); returnHash = "#/workspaces"; loginView(); } })));
}

async function activity() {
  pageLoading("activity");
  if (!(await refresh())) return loginView();
  if (!state.user.is_admin) return home();
  if (state.user.needs_sign_in) return reauthView();
  await loadRail();
  const [users, workspaces] = await Promise.all([api("GET", "/admin/users"), api("GET", "/admin/workspaces")]);
  let rows = [], revision = 0, applied = {};
  const query = (filters, before) => {
    const params = new URLSearchParams(Object.entries(filters).filter(([, v]) => v !== "" && v != null));
    if (before) params.set("before", before);
    return "/admin/activity?" + params;
  };
  const exportAll = async (filters) => { // every page that matches the filters, not only what is on screen
    const all = [];
    let before = null;
    for (let page = 0; page < 200; page++) {
      const r = await api("GET", query({ ...applied, ...filters, limit: "200" }, before));
      if (r.status !== 200) throw new Error("export failed");
      all.push(...r.body.rows);
      if (!r.body.before) break;
      before = r.body.before;
    }
    return all;
  };
  const load = async (filters = {}, before = null) => {
    const request = ++revision;
    if (!before) applied = filters;
    const r = await api("GET", query(filters, before));
    if (location.hash !== "#/activity" || request !== revision) return;
    if (r.status !== 200) return shell("activity", banner("bad", "Activity could not be loaded", say(r.body.error)));
    rows = before ? [...rows, ...r.body.rows] : r.body.rows;
    shell("activity", activityView({ ...r.body, rows, filters, users: users.body.users ?? [], workspaces: workspaces.body.workspaces ?? [] }, {
      filter: (next) => load(next), older: (cursor) => load(applied, cursor), exportAll: () => exportAll(applied),
    }));
  };
  await load();
}

async function admin() {
  const finishLoading = loading();
  pageLoading("admin");
  try {
    if (!(await refresh())) return loginView();
    state.menu = false;
    await loadRail();
    if (!state.user.is_admin) return home();
    if (state.user.needs_sign_in) return reauthView();
    const notice = state.notice;
    state.notice = null;
    const adminApi = async (m, path, body) => {
      const done = loading();
      try { const r = await api(m, path, body); if (r.status === 403 && r.body.error === "reauth_required") reauthView(); return r; }
      finally { done(); }
    };
    if (state.adminPage && frame && paintedRoute === location.hash && !frame.querySelector(".skeleton")) {
      await state.adminPage.reload();
      shell("admin", state.adminPage);
    } else {
      state.adminPage = adminView({ api: adminApi, say, notice, refresh, self: state.user?.user_id });
      shell("admin", state.adminPage);
    }
  } finally { finishLoading(); }
}

async function handoffs() {
  const finishLoading = loading();
  pageLoading("handoffs");
  try {
    if (!(await refresh())) return loginView();
    await loadRail();
    const [ws, ho] = await Promise.all([api("GET", "/me/workspaces"), api("GET", "/me/handoffs")]);
    if (ws.status !== 200 || ho.status !== 200) return shell("handoffs", h("h1", { text: "Hand-offs" }),
      h("p", { role: "alert", text: "Could not load hand-offs. Please try again." }), h("button", { text: "Try again", onclick: handoffs }));
    shell("handoffs", handoffsView({ workspaces: ws.body.workspaces ?? [], handoffs: ho.body.handoffs ?? [] }, {
      dialog, close: closeDialog,
      open: async (w) => { await openWorkspace({ id: w.workspace_id, name: w.name ?? "Workspace" }); closeDialog(); }, // the dialog stays if opening fails
    }));
  } finally { finishLoading(); }
}

async function workspaces(joined = [], known = null) {
  const finishLoading = loading();
  pageLoading("workspaces");
  try {
      if (!(await refresh())) return loginView();
      state.menu = false;
      await loadRail();
      const [ws, ho, admin] = await Promise.all([api("GET", "/me/workspaces"), api("GET", "/me/handoffs"), canAdmin() ? api("GET", "/admin/workspaces") : Promise.resolve({ status: 0, body: {} })]);
      const adminRows = admin.status === 200 ? admin.body.workspaces ?? [] : null;
      const hub = ws.body?.hub;
      const peopleNames = new Map();
      let pageRev = -1; // r11 P12: the Connect panel's people watch runs only while this drawing of the page is current
      const actions = {
        alive: () => pageRev === viewRevision,
        open: (w) => { closeDialog(); return openWorkspace({ id: w.workspace_id, name: w.name ?? "Workspace" }); },
        copy: copyText,
        openLink: (link) => { window.open(link, "_blank", "noopener"); },
        signInToConnect: signInToAddWorkspace,
        people: async () => { // the real, active people of this hub (admin-only route); the hub rejects anyone else
          const r = await api("GET", "/admin/users");
          const list = r.status === 200 ? (r.body.users ?? []).filter((u) => !u.disabled) : [];
          for (const u of list) peopleNames.set(u.user_id, u.name);
          return list;
        },
        invite: async (name) => { // the existing admin invite (name checked by the hub): a one-time sign-in link
          const r = await api("POST", "/admin/invites", { name });
          if (r.status !== 200 || typeof r.body.link !== "string") throw new Error("invite failed");
          return r.body;
        },
        inviteStatus: async (inviteId) => { // r11 P12b: has the person used THIS sign-in link (only its maker may ask)
          const r = await api("GET", "/admin/invites/" + encodeURIComponent(inviteId));
          if (r.status === 404) return { state: "ended" }; // gone, or not ours: stop and let the admin pick
          return r.status === 200 && r.body && typeof r.body.state === "string" ? r.body : null; // a hiccup: ask again
        },
        connect: async (ownerUser) => {
          const r = await api("POST", "/admin/connect", ownerUser ? { owner_user: ownerUser } : {});
          if (r.status < 200 || r.status >= 300 || typeof r.body.link !== "string" || !Number.isFinite(r.body.expires_ms)) throw new Error("connect failed");
          if (ownerUser) rememberName(ownerUser, peopleNames.get(ownerUser)); // the /c page says whose workspace (display only)
          return r.body;
        },
        remove: async (w) => {
          const r = await api("DELETE", "/admin/workspaces/" + encodeURIComponent(w.workspace_id));
          if (r.status < 200 || r.status >= 300) throw new Error("remove failed");
          await workspaces();
        },
        details: async (w) => {
          const r = canAdmin(w) ? await api("GET", "/workspaces/" + encodeURIComponent(w.workspace_id) + "/roles") : { status: 0, body: {} };
          dialog(...workspaceDetailsView(w, { hub, actions, roles: r.status === 200 ? r.body : null,
            owner: adminRows?.find((row) => row.workspace_id === w.workspace_id)?.owner_alias }),
            h("div", { class: "row" }, h("button", { text: "Close", onclick: closeDialog })));
        },
      };
      const rows = ws.status === 200 ? ws.body.workspaces ?? [] : [];
      shell("workspaces", workspacesView({ workspaces: rows, handoffs: ho.status === 200 ? ho.body.handoffs ?? [] : [], admin: adminRows, hub, actions, joined, self: state.user.user_id,
        canConnect: !!state.user.is_admin, needsSignIn: !!state.user.needs_sign_in }));
      // Watch for joins while this page is open (3 s, paused while the tab is hidden, stopped by leaving the route).
      clearInterval(workspacesTimer);
      // Diff only against a successful first read: a failed one must not make every workspace look newly joined.
      let seen = known ?? (ws.status === 200 ? new Set(rows.map((w) => w.workspace_id)) : null);
      const mine = ++viewRevision;
      pageRev = mine;
      let checking = false;
      workspacesTimer = setInterval(async () => {
        if (document.hidden || checking || mine !== viewRevision) return;
        checking = true;
        try {
          const r = await api("GET", "/me/workspaces");
          if (mine !== viewRevision || r.status !== 200) return;
          if (seen === null) return void (await workspaces([], null)); // the first good read: draw it, nothing is "joined"
          const fresh = newWorkspaces(seen, r.body.workspaces);
          if (!fresh.length) return;
          for (const w of fresh) seen.add(w.workspace_id);
          void pollLive();
          await workspaces([...joined, ...fresh], seen);
        } finally { checking = false; }
      }, 3000);
  } finally { finishLoading(); }
}

async function copyText(t) {
  try { await navigator.clipboard.writeText(t); toast("Copied"); return true; } catch { toast("Copy failed: select the text by hand"); return false; }
}

async function dismissNew() {
  await api("POST", "/me/channels/seen", {});
  await (parseRoute(location.hash).kind === "phones" ? phones() : home());
}

// Phones & browsers: an admin reads every person's channels (grouped by person); anyone else reads their own. The hub
// decides which by the session; nothing sent from here names a person for the list.
async function phones() {
  const finishLoading = loading();
  pageLoading("phones");
  try {
    if (!(await refresh())) return loginView();
    state.menu = false;
    await loadRail();
    if (state.user.needs_sign_in) return reauthView();
    const me = state.user;
    const [all, users] = me.is_admin ? await Promise.all([api("GET", "/admin/channels"), api("GET", "/admin/users")]) : [null, null];
    const everyone = all?.status === 200 && users?.status === 200;
    const guard = (r) => { if (r.status === 403 && r.body.error === "reauth_required") reauthView(); return r; };
    const actions = {
      copy: copyText,
      dismiss: dismissNew,
      remove: async (c) => {
        const own = c.user_id === undefined || c.user_id === me.user_id;
        const r = guard(await api("POST", (own ? "/channels/" : "/admin/channels/") + encodeURIComponent(c.channel_id) + "/revoke", {}));
        if (r.status !== 200) throw new Error("remove failed");
        if (state.me?.channelId === c.channel_id) await deleteChannel().catch(() => {}); // revoke first, then delete (A8-5)
        await phones();
      },
      pair: async ({ password, totp }) => {
        const r = guard(await api("POST", "/me/pairing", { password, totp }));
        if (r.status !== 200) return { ok: false, error: say(r.body.error === "malformed" ? "bad_credentials" : r.body.error) };
        return { ok: true, ...r.body };
      },
    };
    shell("phones", phonesView({ admin: everyone, user_id: me.user_id, channels: everyone ? all.body.channels : state.channels,
      users: everyone ? users.body.users : [], newChannels: state.newChannels, thisChannelId: state.me?.channelId }, actions));
  } finally { finishLoading(); }
}

async function services(silent = false) {
  const revision = viewRevision;
  const finishLoading = silent ? () => {} : loading();
  if (!silent) pageLoading("services");
  try {
    if (!(await refresh())) return loginView();
    state.menu = false;
    await loadRail();
    if (!state.user.is_admin) return home();
    if (state.user.needs_sign_in) return reauthView();
    const [r, ws] = await Promise.all([api("GET", "/admin/services"), api("GET", "/me/workspaces")]);
    if (revision !== viewRevision) return;
    if (r.status === 403 && r.body.error === "reauth_required") return reauthView();
    if (r.status !== 200) return shell("services", h("h1", { text: "Services" }), banner("bad", "Cannot read the services", say(r.body.error)));
    shell("services", servicesView({ ...r.body, workspaces_list: ws.status === 200 ? ws.body.workspaces : undefined }, { check: () => services(), open: () => navigate("#/workspaces"), copy: copyText, service: serviceAction }));
    clearInterval(servicesTimer);
    let checking = false;
    servicesTimer = setInterval(async () => {
      if (document.hidden || checking) return;
      checking = true;
      try { await services(true); } finally { checking = false; }
    }, 15000);
  } finally { finishLoading(); }
}

async function account() {
  const finishLoading = loading();
  pageLoading("account");
  try {
    if (!(await refresh())) return loginView();
    state.menu = false;
    await loadRail();
    const fromWorkspace = { signInAgain: async () => { await api("DELETE", "/session"); clearSession(); loginView(); } };
    // a session that came from a workspace may not read or change the account: a plain line, not an error
    if (state.user.needs_sign_in) return shell("account", accountFromWorkspaceView(state.user, fromWorkspace));
    const r = await api("GET", "/me/channels");
    if (r.status === 403 && r.body?.error === "reauth_required") return shell("account", accountFromWorkspaceView(state.user, fromWorkspace));
    if (r.status !== 200) return shell("account", h("h1", { text: "Account" }), banner("bad", "Cannot read your phones and browsers", "Please try again."));
    const security = { // M2-6: the person's own password, authenticator and sessions (the hub refuses a switch session)
      password: (body) => api("POST", "/me/password", body),
      begin: (body) => api("POST", "/me/authenticator/begin", body),
      confirm: (code) => api("POST", "/me/authenticator/confirm", { code }),
      endAll: async () => { const e = await api("POST", "/me/sessions/end-all", {}); if (e.status === 200) loginView("You were signed out everywhere. Sign in again."); return e; },
      copy: copyText,
      signInAgain: async () => { await api("DELETE", "/session"); clearSession(); loginView(); },
    };
    shell("account", state.notice, accountView({ user: state.user, channels: r.body.channels, thisBrowserId: state.me?.channelId, needsSignIn: !!state.user.needs_sign_in }, { remove: removeChannel, security }));
    state.notice = null;
  } finally { finishLoading(); }
}

async function approvals(filter = {}) {
  if (!(await refresh())) return loginView();
  await loadRail();
  const [ws, needs, chg] = await Promise.all([api("GET", "/me/workspaces"), api("GET", "/me/needs-you"), api("GET", "/me/changes")]);
  if (ws.status !== 200 || needs.status !== 200) return shell("approvals", banner("bad", "Cannot read approvals", "Please try again."));
  const changes = chg.status === 200 ? changesView(chg.body, { approve: approveChange, reject: rejectChange, canSign: !!state.me }, Date.now()) : [];
  shell("approvals", changes, inboxView({ ...needs.body, workspaces: ws.body.workspaces, channels: state.channels, filter }, { review: reviewItem, open: openWorkspace, filter: approvals }));
}

// An access change that waits for me as the workspace owner: prepare, check the digest here, sign with this browser's key.
const changePath = (change, tail) => "/me/changes/" + encodeURIComponent(change.id) + tail;
function changeFailed(code) {
  dialog(banner("bad", "Nothing was approved", say(code)), h("div", { class: "row" }, h("button", { text: "Close", onclick: async () => { closeDialog(); await approvals(); } })));
}
async function approveChange(change, ap) {
  if (!state.me) { state.notice = banner("warn", "Register this browser first", null); return home(); }
  const me = state.me;
  const r = await api("POST", changePath(change, "/approve/prepare"), { workspace_id: ap.workspace_id });
  if (r.status === 401) return loginView(say("no_session"));
  if (r.status !== 200) return changeFailed(r.body.error);
  let digest;
  try { digest = await checkChange(r.body, change, ap.workspace_id); } catch (e) { return changeFailed(e.message === "event_changed" ? "event_changed" : "malformed"); }
  const sign = h("button", { class: "pri", id: "sign", "data-sign": "", disabled: true, text: "Please review…" });
  sign.arm = () => arm(sign, "Sign and send");
  const onVis = () => sign.arm();
  document.addEventListener("visibilitychange", onVis);
  window.addEventListener("focus", onVis);
  const done = () => { document.removeEventListener("visibilitychange", onVis); window.removeEventListener("focus", onVis); closeDialog(); };
  sign.addEventListener("click", async (ev) => {
    if (!ev.isTrusted || sign.disabled || offline || sign.getAttribute("aria-busy") === "true") return; // a script's .click() never signs
    sign.disabled = true;
    sign.setAttribute("aria-busy", "true");
    try {
      const proof = await signProof(me.pair.privateKey, digest);
      const a = await api("POST", changePath(change, "/approve"), { channel_id: me.channelId, proof, workspace_id: ap.workspace_id });
      document.removeEventListener("visibilitychange", onVis); window.removeEventListener("focus", onVis);
      if (a.status !== 200) {
        if (a.body.error === "channel_revoked") await deleteChannel().catch(() => {}); // only the hub's own word removes the key
        return changeFailed(a.body.error);
      }
      dialog(banner("ok", a.body.state === "applied" ? "Approved and applied" : "Approved", a.body.state === "applied" ? "The change is now in effect." : "It still waits for " + a.body.remaining + " more workspace owner(s)."),
        h("div", { class: "row" }, h("button", { class: "pri", text: "Back to approvals", onclick: async () => { closeDialog(); await approvals(); } })));
    } catch {
      changeFailed("unavailable");
    }
  });
  dialog(h("h1", { text: "Approve an access change" }),
    h("p", { class: "sentence", text: changeSentence(change, ap.workspace_name) + ". The hub signs this approval for you with this browser's key." }),
    h("details", {}, h("summary", { text: "What exactly is signed" }), factsList([["Change", change.id], ["Workspace", ap.workspace_id], ["Access version it was based on", String(r.body.ctx.base_snapshot_version)], ["Lapses on", new Date(change.expires_ms).toLocaleString()]])),
    h("div", { class: "row" }, h("button", { text: "Cancel", onclick: done }), sign));
  sign.arm();
}
// Start, stop or restart a part: the helper makes the request, this page checks the digest, the admin signs with this browser's key.
const svcPath = (name, tail) => "/admin/services/" + encodeURIComponent(name) + tail;
function serviceFailed(code, detail) {
  dialog(banner("bad", "Nothing was changed", say(code) + (detail ? " (" + renderText(detail) + ")" : "")), h("div", { class: "row" }, h("button", { text: "Close", onclick: closeDialog })));
}
async function serviceAction(name, verb) {
  if (!state.me) { state.notice = banner("warn", "Register this browser first", null); return home(); }
  const me = state.me;
  const r = await api("POST", svcPath(name, "/prepare"), { action: verb, channel_id: me.channelId });
  if (r.status === 401) return loginView(say("no_session"));
  if (r.status !== 200) return serviceFailed(r.body.error);
  let digest;
  try { digest = await checkServiceAction(r.body, { part: name, action: verb, userId: state.user.user_id, channelId: me.channelId }); } catch (e) { return serviceFailed(e.message === "event_changed" ? "event_changed" : "malformed"); }
  const sign = h("button", { class: verb === "stop" ? "dan" : "pri", id: "sign", "data-sign": "", disabled: true, text: "Please review…" });
  const label = VERBS[verb] + " " + (r.body.text.match(/^\w+ (\w+)/)?.[1] ?? name);
  sign.arm = () => arm(sign, label);
  const onVis = () => sign.arm();
  document.addEventListener("visibilitychange", onVis);
  window.addEventListener("focus", onVis);
  const done = () => { document.removeEventListener("visibilitychange", onVis); window.removeEventListener("focus", onVis); closeDialog(); };
  sign.addEventListener("click", async (ev) => {
    if (!ev.isTrusted || sign.disabled || offline || sign.getAttribute("aria-busy") === "true") return;
    sign.disabled = true;
    sign.setAttribute("aria-busy", "true");
    try {
      const proof = await signProof(me.pair.privateKey, digest);
      const a = await api("POST", svcPath(name, "/run"), { channel_id: me.channelId, nonce: r.body.ctx.nonce, proof });
      done();
      if (a.status !== 200) {
        if (a.body.error === "channel_revoked") await deleteChannel().catch(() => {});
        return serviceFailed(a.body.error, a.body.error === "action_failed" ? a.body.detail : undefined);
      }
      if (a.body.will_restart) return waitForHub(verb);
      toast(renderText(r.body.ctx.part) + " " + resultWords[verb]);
      await services(true);
    } catch {
      done();
      serviceFailed("unavailable");
    }
  });
  dialog(h("h1", { text: VERBS[verb] + " " + (r.body.text.match(/^\w+ (\w+)/)?.[1] ?? name) + "?" }),
    h("p", { class: "sentence", text: r.body.text }),
    h("p", { class: "small muted", text: "The hub signs this with this browser's key. Nothing else is changed." }),
    h("div", { class: "row" }, h("button", { text: "Cancel", onclick: done }), sign));
  sign.arm();
}
// The hub restarts or stops itself: this page cannot ask it anything while it is down, so it polls and says what to do if it stays away.
async function waitForHub(verb) {
  const cmd = "systemctl start orch-hub";
  const box = h("div", { class: "card" });
  const show = (...nodes) => { box.replaceChildren(...nodes); };
  show(banner("warn", verb === "stop" ? "The hub is stopping" : "The hub is restarting…", "This page cannot reach it for a moment."));
  dialog(box);
  if (verb === "stop") return show(banner("warn", "The hub is stopped", "Workspaces keep working on their own. To start it again, run this on the server:"), h("div", { class: "cmd" }, h("code", { text: cmd })), h("div", { class: "row" }, h("button", { text: "Copy", onclick: () => copyText(cmd) }), h("button", { text: "Close", onclick: closeDialog })));
  const until = Date.now() + 60000;
  while (Date.now() < until) {
    await new Promise((res) => setTimeout(res, 2000));
    const r = await api("GET", "/admin/services").catch(() => null);
    if (r?.status === 200 || r?.status === 401 || r?.status === 403) { closeDialog(); return services(true); }
  }
  show(banner("bad", "The hub did not come back", "Run this on the server:"), h("div", { class: "cmd" }, h("code", { text: cmd })), h("div", { class: "row" }, h("button", { text: "Copy", onclick: () => copyText(cmd) }), h("button", { text: "Close", onclick: closeDialog })));
}
async function rejectChange(change, ap) {
  const r = await api("POST", changePath(change, "/reject"), { workspace_id: ap.workspace_id });
  if (r.status === 401) return loginView(say("no_session"));
  if (r.status !== 200) return changeFailed(r.body.error);
  toast("Rejected. Nothing was changed.");
  await approvals();
}

async function home() {
  const finishLoading = loading();
  pageLoading("home");
  try {
    if (!(await refresh())) return loginView();
    state.menu = false;
    await loadRail();
    const user = state.user;
    const [ws, needs, sharedRes, adminRes] = await Promise.all([api("GET", "/me/workspaces"), api("GET", "/me/needs-you"), api("GET", "/me/shared"), canAdmin() ? api("GET", "/admin/workspaces") : Promise.resolve({ status: 0, body: {} })]);
    if (!user || state.user !== user) return;
    const mine = state.me && state.channels.find((c) => c.channel_id === state.me.channelId && c.status === "active");
    const workspaces = ws.status === 200 ? ws.body.workspaces : [];
    const dead = !mine && (state.me || state.channels.some((c) => c.status === "active" && c.kind === "browser"));
    const shared = sharedRes.status === 200 ? sharedRes.body.items ?? [] : [];
    // P04: per person, in this browser; diffed only after a successful read, dropped on sign out (clearSession)
    const ended = sharedRes.status === 200 ? trackShared(user.user_id, shared) : loadEnded(user.user_id);
    homeData = {
      user_id: user.user_id,
      user: state.user,
      items: needs.status === 200 ? needs.body.items : [],
      offline: needs.status === 200 ? needs.body.offline : [],
      workspaces,
      adminWorkspaces: adminRes.status === 200 ? adminRes.body.workspaces ?? [] : null,
      shared,
      ended,
      channels: state.channels.map((c) => ({ ...c, status: c.status + (state.me?.channelId === c.channel_id ? " (this browser)" : "") })),
      newChannels: state.newChannels,
      thisBrowser: state.user.needs_sign_in ? { state: "from_workspace" } : mine ? { state: "active", label: state.me.label } : dead ? { state: "dead", label: state.me?.label } : { state: "unregistered" },
      notice: state.notice,
      actions: { review: (it) => navigate("#/w/" + it.workspace_id + "/item/" + encodeURIComponent(it.key || it.item_id)), open: (id) => navigate("#/w/" + id), dismissEnded: (id) => { dismissEnded(user.user_id, id); void home(); }, openShared: (it) => navigate("#/shared/" + it.workspace_id + "/" + encodeURIComponent(it.key)), connect: () => navigate("#/workspaces"), register: () => registerView(), removeChannel, forgetLocal, dismissNew },
    };
    liveItems = { user_id: user.user_id, items: homeData.items };
    shell("home", homeView(homeData), workspaces.some((w) => w.can_start) ? advancedForm(workspaces) : null);
    state.notice = null;
  } finally { finishLoading(); }
}

async function forgetLocal() {
  // only reached when the hub no longer lists this key as active (revoked or unknown): a dead key is harmless, but we tidy up
  const c = state.channels.find((x) => x.channel_id === state.me?.channelId);
  if (c && c.status === "active") return;
  if (!c || c.status === "revoked") await deleteChannel().catch(() => {});
  await home();
}

async function removeChannel(id) {
  const r = await api("POST", "/channels/" + encodeURIComponent(id) + "/revoke", {});
  const wasThisBrowser = r.status === 200 && state.me?.channelId === id;
  if (wasThisBrowser) await deleteChannel().catch(() => {}); // revoke first, then delete (A8-5)
  // removing this browser's own key ends the session view: no notice may be left over for the next sign-in
  state.notice = wasThisBrowser ? null : r.status === 200 ? banner("ok", "Removed", null) : banner("bad", "Not removed", say(r.body.error));
  await (parseRoute(location.hash).kind === "account" ? account() : home());
}

function clearSession() {
  homeData = null;
  liveItems = { user_id: null, items: [] };
  state.user = null;
  state.rail = { hubName: "Hub", rows: [] };
  state.me = null;
  state.channels = [];
  state.newChannels = [];
  state.adminPage = null;
  state.notice = null;
  state.menu = false;
  forgetSharedMemory();
}

// P04: what the last good /me/shared read held, and the "stopped sharing" lines not yet dismissed, per person. In memory only
// (the console keeps nothing in web storage): gone on reload and on sign out. Diffed only after a successful read, so an
// unreachable hub never produces a false "stopped sharing".
const sharedMemory = new Map();
function loadEnded(uid) { return sharedMemory.get(uid)?.ended ?? []; }
function trackShared(uid, items) {
  const ended = sharedEnded(sharedMemory.get(uid)?.last ?? null, items, loadEnded(uid));
  sharedMemory.set(uid, { last: items.map((it) => ({ workspace_id: it.workspace_id, key: it.key, title: it.title, shared_by: it.shared_by })), ended });
  return ended;
}
function dismissEnded(uid, id) { const m = sharedMemory.get(uid); if (m) m.ended = m.ended.filter((e) => e.id !== id); }
function forgetSharedMemory() { sharedMemory.clear(); }

async function signOut() {
  await api("DELETE", "/session");
  clearSession();
  loginView();
}

async function registerRoute() { // a hub with no workspace has no Home signing card: register a browser from here
  if (!(await refresh())) return loginView();
  registerView();
}
function registerView(err) {
  const label = h("input", { id: "label", value: "This browser", maxlength: 60 });
  const pw = h("input", { id: "pw2", type: "password", autocomplete: "current-password", required: true });
  const code = h("input", { id: "code2", inputmode: "numeric", autocomplete: "one-time-code", maxlength: 6, required: true });
  const form = h("form", {
    onsubmit: async (ev) => {
      ev.preventDefault();
      const button = form.querySelector("[type=submit]");
      if (offline || button.disabled) return;
      const out = await busyButton(button, () => registerBrowser(label.value.trim() || "This browser", pw.value, code.value.trim()));
      if (out === undefined) return; // a second submit while the first is still running: that one decides, not this one
      pw.value = code.value = "";
      if (out) return registerView(out);
      state.notice = banner("ok", "This browser is ready", "It holds a signing key for you. You can remove it any time.");
      await home();
    },
  }, h("p", { class: "muted small", text: "Type your password and a new 6-digit code. Each code works only once, so wait for the next one if you just signed in." }), field("Browser name", label), field("Password", pw), field("6-digit code", code), h("div", { class: "row top" }, h("button", { type: "button", text: "Cancel", onclick: home }), h("button", { class: "pri", type: "submit", "data-sign": "", text: "Register this browser" })));
  show(h("h1", { text: "Register this browser" }), err ? banner("bad", "Browser not registered. Nothing was kept.", err) : null, form);
}

/** Returns an error text, or null on success. The key is stored only after the certificate checked out. */
async function registerBrowser(label, password, totp) {
  let gen;
  try {
    gen = await generateKey();
    const me0 = { keyPub: gen.keyPub, userId: state.user.user_id, hubId: state.me?.hubId ?? null };
    const begun = await api("POST", "/channels/register/begin", { kind: "browser", key_pub: gen.keyPub, label, password, totp });
    if (begun.status !== 200) return say(begun.body.error);
    const signed = await signRegistration(gen.pair.privateKey, begun.body.challenge, me0, Date.now());
    const done = await api("POST", "/channels/register/complete", { challenge_id: begun.body.challenge_id, signed });
    if (done.status !== 200) return say(done.body.error);
    const o = done.body.channel_cert?.o;
    if (!o || o.kind !== "hub.channel_cert" || o.user_id !== me0.userId || o.key_pub !== gen.keyPub || o.channel_id !== gen.channelId || o.factor !== "password+totp" || o.hub_id !== begun.body.challenge.hub_id)
      return "The hub's certificate did not match. Nothing was kept.";
    await saveChannel({ pair: gen.pair, keyPub: gen.keyPub, channelId: gen.channelId, userId: me0.userId, hubId: o.hub_id, label });
    if ((await loadChannel())?.channelId !== gen.channelId) return "This browser could not keep the key here."; // say "ready" only when the key is really stored
    return null;
  } catch (e) {
    return e instanceof ChannelError || e instanceof CanonError ? "This browser could not create or keep a key here." : "Something went wrong. Nothing was kept.";
  }
}

async function reviewItem(it) {
  const r = await api("GET", "/workspaces/" + it.workspace_id + "/items/" + it.item_id);
  if (r.status !== 200) { state.notice = banner("bad", "Cannot open this", say(r.body.error)); return home(); }
  const c = r.body.content ?? {};
  if (it.kind === "gate" || it.kind === "verdict") return reviewDecision(it, c);
  // #1123: the question is hashed HERE from exactly what the hub sent; no Answer button for one we cannot verify or render
  if (r.body.hash !== it.hash) { state.notice = banner("warn", "This question changed", "Reload your list and review it again."); return home(); }
  const qv = await verifyQuestion(it, c);
  const head = [h("p", { class: "sentence", text: reviewMeaning(it, state.rail.rows.find((w) => w.id === it.workspace_id)?.name || "this workspace") }), h("h1", { text: renderText(c.ticket_title ?? it.title ?? "Question") }), h("p", { class: "sentence", text: renderMulti(c.text ?? it.title ?? "") })];
  const back = h("button", { text: "Back", onclick: home });
  if (!qv) return show(...head, h("p", { class: "small muted", text: "This question cannot be shown or checked here. Answer it on your Mac." }), back);
  const spec = qv.spec;
  const more = [
    spec.details ? h("details", { open: true }, h("summary", { text: "More detail" }), h("p", { class: "small", text: renderMulti(spec.details) })) : null,
    spec.why ? h("p", { class: "muted", text: renderMulti(spec.why) }) : null,
  ];
  if (spec.kind !== "choice") return show(...head, ...more, h("p", { class: "small muted", text: "This is an open question. Answer it on your Mac." }), back);
  const card = (o) => h("div", { class: "card" },
    h("div", { class: "row" }, h("div", { class: "grow" }, h("strong", { text: renderText(o.label) + (spec.recommended === o.key ? " (recommended)" : "") }),
      o.text ? h("div", { class: "small", text: renderText(o.text) }) : null,
      (o.facts ?? []).map(([l, v]) => h("div", { class: "small muted", text: renderText(l) + ": " + renderText(v) })),
      o.cost ? h("div", { class: "small muted", text: "Cost: " + renderText(o.cost) }) : null),
    h("button", { "data-sign": "", text: "Answer", onclick: () => startSign(it.workspace_id, { type: "question.answered", log: it.log, question: c.question, hash: it.hash, option: o.key }, qv) })));
  show(...head, ...more, qv.options.map(card),
    spec.allow_other ? h("p", { class: "small muted", text: "To answer with something else, use your Mac." }) : null,
    back);
}

// D15b-3: a gate or verdict row. The action sends the hash the row was LISTED with (it.hash), never one re-read now; if the
// content moved since, the hub answers event_changed and the person reviews again. The prompt shows what is signed.
function reviewDecision(it, c) {
  const note = h("textarea", { id: "reason", rows: 3 });
  const act = (action) => startSign(it.workspace_id, { log: it.log, hash: it.hash, ...action });
  const withText = (action) => { const text = note.value.trim(); if (!text) { note.focus(); return; } return act({ ...action, text }); };
  const sources = Array.isArray(c.sources) ? c.sources : [];
  const verdict = it.kind === "verdict";
  show(
    h("p", { class: "small" }, h("a", { href: "#", text: "\u2190 Back to your list", onclick: (e) => { e.preventDefault(); home(); } })),
    h("h1", { text: renderText(c.ticket_title ?? it.title ?? "Review") }),
    it.key ? h("p", { class: "small muted", text: renderText(it.key) }) : null,
    h("p", { class: "sentence", text: reviewMeaning(it, state.rail.rows.find((w) => w.id === it.workspace_id)?.name || "this workspace", c.gate) }),
    h("p", { class: "small muted", text: "The content itself is not shown here. Check it where it lives; the reference below is what you are deciding on." }),
    h("details", {}, h("summary", { text: "What exactly is signed" }), h("dl", { class: "facts" },
      h("dt", { text: "Content reference" }), h("dd", { class: "fp", text: renderText(shortHash(it.hash) || String(it.hash ?? "")) }),
      h("dt", { text: "Generation" }), h("dd", { text: renderText(String(c.gate_gen ?? "")) }),
      sources.flatMap((e) => [h("dt", { text: "Code to check" }), h("dd", { text: renderText(e.repo) + " " + renderText(e.sha) })]))),
    h("label", { for: "reason", text: "Reason" }),
    note,
    h("p", { class: "small muted", text: verdict ? "Needed only if you choose Fail." : "Needed only if you ask for changes." }),
    verdict
      ? h("div", { class: "row" }, h("button", { class: "pri", "data-sign": "", text: "Pass", onclick: () => act({ type: "verdict.given", outcome: "pass" }) }), h("button", { "data-sign": "", text: "Fail", onclick: () => withText({ type: "verdict.given", outcome: "fail" }) }))
      : h("div", { class: "row" }, h("button", { class: "pri", "data-sign": "", text: "Approve", onclick: () => act({ type: "gate.approved", gate: c.gate }) }), h("button", { "data-sign": "", text: "Ask for changes", onclick: () => withText({ type: "gate.changes_requested", gate: c.gate }) })),
  );
}

// Keep the wanted fragment while the sign-in form is showing.
// The page to land on after the next sign-in; only a short same-origin hash route is ever accepted.
let returnHash = null;
const SAFE_RETURN = /^#\/[a-z0-9\/_-]{0,40}$/i;
async function afterLogin() {
  const back = returnHash;
  returnHash = null;
  if (typeof back === "string" && SAFE_RETURN.test(back) && location.hash !== back) { location.hash = back; return; }
  return renderRoute();
}

// Agent access terms are display and signing input; the host checks its pending record at landing.
async function accessView(wsId, raw) {
  let terms = null;
  try {
    const b = atob(raw.replaceAll("-", "+").replaceAll("_", "/"));
    terms = parseStrict(new TextDecoder("utf-8", { fatal: true }).decode(Uint8Array.from(b, (c) => c.charCodeAt(0))));
  } catch { terms = null; }
  const back = h("p", { class: "small" }, h("a", { href: "#", text: "\u2190 Back to your list", onclick: (e) => { e.preventDefault(); location.hash = ""; home(); } }));
  if (!(await refresh())) return loginView();
  const ws = await api("GET", "/me/workspaces");
  const mine = ws.status === 200 ? ws.body.workspaces.find((w) => w.workspace_id === wsId) : null;
  if (!grantTermsOk(terms) || !mine || !["edit", "admin"].includes(mine.role)) return shell("home", h("div", {}, back, banner("bad", "This link cannot be used", "It does not describe an agent access request this page can show safely.")));
  const action = { type: "grant.requested", log: "workspace", ...terms };
  shell("home", h("div", {}, back,
    h("h1", { text: "Approve agent access" }),
    h("p", { class: "sentence", text: grantSentence(terms, mine.name ?? "A workspace") }),
    h("dl", { class: "facts" }, grantFacts(terms).flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })])),
    h("div", { class: "row" }, h("button", { class: "pri", text: "Review and approve", onclick: () => startSign(wsId, action) }))));
}

// FB15 (protocol 8.8b): the link a workspace's Mission Control builds, #/w/<workspace>/addon/<b64u(JSON terms)>. The terms
// are parsed strictly and rendered from this page's own words; the signature still goes through the same prepare -> review
// -> sign prompt, whose digest is recomputed here. A link that names a workspace this person does not hold is refused.
async function addonView(wsId, raw) {
  let terms = null;
  try {
    const b = atob(raw.replaceAll("-", "+").replaceAll("_", "/"));
    terms = parseStrict(new TextDecoder("utf-8", { fatal: true }).decode(Uint8Array.from(b, (c) => c.charCodeAt(0))));
  } catch { terms = null; }
  const back = h("p", { class: "small" }, h("a", { href: "#", text: "\u2190 Back to your list", onclick: (e) => { e.preventDefault(); location.hash = ""; home(); } }));
  if (!(await refresh())) return loginView();
  const ws = await api("GET", "/me/workspaces");
  const mine = ws.status === 200 ? ws.body.workspaces.find((w) => w.workspace_id === wsId) : null;
  if (!addonTermsOk(terms) || !mine) return shell("home", h("div", {}, back, banner("bad", "This link cannot be used", "It does not describe an add-on request this page can show safely.")));
  const action = { type: "addon.requested", log: "workspace", ...terms };
  shell("home", h("div", {}, back,
    h("h1", { text: "Approve an add-on" }),
    h("p", { class: "sentence", text: "The workspace " + renderText(mine.name ?? "A workspace") + " asks to install and turn on " + renderText(terms.name) + "." }),
    h("dl", { class: "facts" }, addonFacts(terms).flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })])),
    h("p", { class: "small muted", text: "Approving here lets the workspace's computer install exactly this package. You can check the fingerprint against the one shown in the workspace." }),
    h("div", { class: "row" }, h("button", { class: "pri", "data-sign": "", text: "Review and approve", onclick: () => startSign(wsId, action) }))));
}

// A settings change (protocol 8.8): the link a workspace's Mission Control builds, #/w/<workspace>/settings/<b64u(JSON set)>.
// Parsed strictly, shown in this page's words, signed only after the click and the normal review.
const isObjTerms = (t) => t !== null && typeof t === "object" && !Array.isArray(t) && Object.keys(t).sort().join() === "exp,set";
async function settingsView(wsId, raw) {
  let terms = null;
  try {
    const b = atob(raw.replaceAll("-", "+").replaceAll("_", "/"));
    terms = parseStrict(new TextDecoder("utf-8", { fatal: true }).decode(Uint8Array.from(b, (c) => c.charCodeAt(0))));
  } catch { terms = null; }
  const back = h("p", { class: "small" }, h("a", { href: "#", text: "\u2190 Back to your list", onclick: (e) => { e.preventDefault(); location.hash = ""; home(); } }));
  if (!(await refresh())) return loginView();
  const ws = await api("GET", "/me/workspaces");
  const mine = ws.status === 200 ? ws.body.workspaces.find((w) => w.workspace_id === wsId) : null;
  const exp = terms?.exp;
  const fresh = Number.isInteger(exp) && exp * 1000 > Date.now();
  if (isObjTerms(terms) && !fresh) return shell("home", h("div", {}, back, banner("bad", "This link has expired", "Go back to the workspace and press Save again.")));
  terms = isObjTerms(terms) ? terms.set : null;
  if (!settingsTermsOk(terms) || !mine) return shell("home", h("div", {}, back, banner("bad", "This link cannot be used", "It does not describe a settings change this page can show safely.")));
  const action = { type: "settings.changed", log: "workspace", set: terms };
  shell("home", h("div", {}, back,
    h("h1", { text: "Change workspace settings" }),
    h("p", { class: "sentence", text: "This link asks to change how long things are held in " + renderText(mine.name ?? "A workspace") + ". Check the change below; nothing happens until you approve." }),
    h("dl", { class: "facts" }, settingsFacts(terms).flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })])),
    h("div", { class: "row" }, h("button", { class: "pri", "data-sign": "", text: "Review and approve", onclick: () => startSign(wsId, action) }))));
}

function advancedForm(workspaces) {
  const ws = h("select", {}, workspaces.filter((w) => w.can_start).map((w) => h("option", { value: w.workspace_id, text: renderText(w.name ?? "A workspace") })));
  const type = h("select", {}, Object.keys(DECISIONS).filter((t) => t !== "question.answered").map((t) => h("option", { value: t, text: t })));
  const inputs = Object.fromEntries(["log", "question", "gate", "outcome", "hash", "option", "text"].map((k) => [k, h("input", { id: "f-" + k })]));
  const go = h("button", { class: "pri", "data-sign": "", text: "Review", onclick: () => {
    const action = { type: type.value, log: inputs.log.value.trim() };
    for (const k of DECISIONS[type.value].filter((k) => k in inputs)) if (inputs[k].value.trim()) action[k] = inputs[k].value.trim();
    return startSign(ws.value, action);
  } });
  return h("details", {}, h("summary", { text: "Start a decision by hand" }), field("Workspace", ws), field("Decision", type), ...Object.entries(inputs).map(([k, el]) => field(k === "log" ? "Ticket (id)" : k, el)), h("div", { class: "row top" }, go));
}

// --- the sign prompt ---------------------------------------------------------------------------------------------------
function dialog(...nodes) {
  document.getElementById("dlg")?.remove();
  const d = h("div", { class: "dialog", id: "dlg", role: "dialog", "aria-modal": "true" }, h("div", { class: "card" }, nodes));
  document.body.append(d);
  return d;
}
const closeDialog = () => document.getElementById("dlg")?.remove();

function nothingSigned(code, extra) {
  dialog(banner("bad", "Nothing was signed", say(code)), extra, h("div", { class: "row" }, h("button", { text: "Close", onclick: async () => { closeDialog(); await home(); } })));
}

async function startSign(workspaceId, action, qv = null) {
  if (!state.me) { state.notice = banner("warn", "Register this browser first", null); return home(); }
  const me = state.me;
  const sent = { workspaceId, action };
  const r = await api("POST", "/sign/prepare", { workspace_id: workspaceId, channel_id: me.channelId, action });
  if (r.status === 401) return loginView(say("no_session"));
  // arrived by the rail switch: that session may read but not sign. Ask for password + code here, then carry on.
  if (r.status === 403 && r.body.error === "reauth_required") return signInToSign(action, () => startSign(workspaceId, action, qv));
  if (r.status !== 200) return nothingSigned(r.body.error);
  const p = r.body;
  let digest, d;
  try {
    digest = await checkPrepared(p.ctx, p.digest); // one parse: ctx is rendered below and was hashed here
    checkBindings(p.ctx, me, sent);
    d = describe(p.ctx, digest, sent, qv);
  } catch (e) {
    return nothingSigned(e instanceof Refusal ? e.code : "malformed");
  }
  if (!d.signable) return nothingSigned("event_changed", h("details", { open: true }, h("summary", { text: "What the hub built" }), factsList(d.fields)));
  promptView(me, p, digest, d);
}

// A session that came from a workspace (rail switch) cannot sign until the person proves it is them. One plain step in the
// prompt, no raw codes: password and the 6-digit code, then the same review opens. Nothing is signed here.
function signInToSign(action, again, err) {
  const verb = action?.type === "question.answered" ? "answer" : "approve";
  const pw = h("input", { id: "re-pw", type: "password", autocomplete: "current-password", required: true });
  const code = h("input", { id: "re-code", inputmode: "numeric", autocomplete: "one-time-code", maxlength: 6, required: true });
  const form = h("form", {
    onsubmit: async (ev) => {
      ev.preventDefault();
      const r = await api("POST", "/session", { user_id: state.user.user_id, password: pw.value, totp: code.value.trim(), client: "browser" });
      pw.value = code.value = "";
      if (r.status !== 200) return signInToSign(action, again, say(r.body.error === "malformed" ? "bad_credentials" : r.body.error));
      closeDialog();
      if (!(await refresh())) return loginView();
      await again();
    },
  }, h("p", { class: "muted small", text: "You came here from your workspace. To " + verb + ", confirm it is you with your password and a 6-digit code. Each code works only once." }), field("Password", pw), field("6-digit code", code),
    h("div", { class: "row top" }, h("button", { type: "button", text: "Cancel", onclick: () => closeDialog() }), h("button", { class: "pri", type: "submit", text: "Sign in to " + verb })));
  dialog(h("h1", { text: "Sign in to " + verb }), err ? banner("bad", "Not signed in", err) : null, form);
  pw.focus();
}

function factsList(rows) {
  return h("dl", { class: "facts" }, rows.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })]));
}

function promptView(me, p, digest, d) {
  const sign = h("button", { class: "pri", id: "sign", "data-sign": "", disabled: true, text: "Please review…" });
  sign.arm = () => arm(sign, "Sign and send");
  const onVis = () => sign.arm();
  document.addEventListener("visibilitychange", onVis);
  window.addEventListener("focus", onVis);
  const done = () => { document.removeEventListener("visibilitychange", onVis); window.removeEventListener("focus", onVis); closeDialog(); };
  sign.addEventListener("click", async (ev) => {
    if (!ev.isTrusted || sign.disabled || offline || sign.getAttribute("aria-busy") === "true") return; // a script's .click() never signs
    sign.disabled = true;
    sign.setAttribute("aria-busy", "true");
    try {
      const proof = await signProof(me.pair.privateKey, digest);
      const r = await api("POST", "/sign/sign", { prepared_id: p.prepared_id, channel_id: me.channelId, channel_proof: proof });
      document.removeEventListener("visibilitychange", onVis); window.removeEventListener("focus", onVis);
      if (r.status !== 200) {
        if (r.body.error === "channel_revoked") await deleteChannel().catch(() => {}); // only the hub's own word removes the key
        return nothingSigned(r.body.error);
      }
      dialog(banner("ok", "Signed and sent", "Recorded on behalf of " + renderText(state.user.name ?? "you") + ", via the hub."), h("p", { class: "sentence", text: d.sentence }), h("dl", { class: "facts" }, h("dt", { text: "Signature reference" }), h("dd", { class: "fp", text: d.fingerprint })), h("div", { class: "row" }, h("button", { class: "pri", text: "Back to home", onclick: async () => { closeDialog(); await home(); } })));
    } catch {
      nothingSigned("unavailable");
    }
  });
  dialog(
    h("h1", { text: d.type === "grant.requested" ? "Approve agent access" : d.type === "addon.requested" ? "Approve an add-on" : d.type === "settings.changed" ? "Approve a settings change" : "Review your answer" }),
    h("p", { class: "sentence", text: signedMeaning(d.sentence, state.rail.rows.find((w) => w.id === p.ctx.workspace_id)?.name || "this workspace") }),
    h("details", {}, h("summary", { text: "What exactly is signed" }), h("dl", { class: "facts" }, h("dt", { text: "On behalf of" }), h("dd", { text: renderText(state.user.name ?? "you") }), h("dt", { text: "Signature reference" }), h("dd", { class: "fp", text: d.fingerprint }), d.facts.length ? d.facts.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { class: k === "Content reference" ? "fp" : "", text: v })]) : d.contentHash ? [h("dt", { text: "Content reference" }), h("dd", { class: "fp", text: d.contentHash })] : null),
    factsList(d.fields)),
    h("p", { class: "small muted who-signs", text: "You confirm with the key held by this browser. " + "The hub" + " signs this approval for you." }),
    h("div", { class: "row" }, h("button", { text: "Cancel", onclick: done }), sign),
  );
  sign.arm();
}

// Presentation-only live state; never refresh the page or a signing prompt on a timer.
let homeData = null, liveItems = { user_id: null, items: [] }, offline = false, failures = 0, nextPoll = Date.now() + 15000, polling = false;
const offlineText = h("span");
const retry = h("button", { text: "Try now", onclick: () => pollLive() });
const offbar = h("div", { id: "offbar", class: "offbar", role: "status", hidden: true }, offlineText, retry);
document.body.prepend(offbar);
function connectionFailed() {
  if (!offline) { offline = true; nextPoll = Date.now() + retryDelay(0); }
  updateOffline();
}
function updateOffline() {
  offbar.hidden = !offline;
  document.body.classList.toggle("hub-offline", offline);
  offlineText.textContent = "No connection to the hub. What you see may be out of date; approving is paused. Trying again in " + Math.max(0, Math.ceil((nextPoll - Date.now()) / 1000)) + " s";
  retry.disabled = polling;
  syncSignButtons();
}
function arm(button, label = button.textContent) {
  clearTimeout(button.armTimer);
  button.disabled = true;
  button.armTimer = setTimeout(() => {
    if (document.visibilityState === "visible" && button.getAttribute("aria-busy") !== "true" && !offline) {
      button.disabled = false;
      button.textContent = label;
    }
  }, 1000);
}
function syncSignButtons() {
  for (const button of document.querySelectorAll("[data-sign]")) {
    if (offline) { button.dataset.offlineDisabled = "true"; button.disabled = true; }
    else if (button.dataset.offlineDisabled) { delete button.dataset.offlineDisabled; button.arm?.(); }
  }
}
new MutationObserver(() => { if (offline) syncSignButtons(); }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled"] });
document.addEventListener("click", (event) => {
  if (offline && event.target.closest("[data-sign]")) { event.preventDefault(); event.stopImmediatePropagation(); }
}, true);
async function pollLive() {
  if (polling || document.visibilityState !== "visible") return;
  polling = true;
  updateOffline();
  const user = state.user, retrying = offline;
  try {
    const [needs, rail] = await Promise.all([api("GET", "/me/needs-you"), api("GET", "/rail")]);
    if (user !== state.user) return;
    if ([needs.status, rail.status].some((s) => s === 401 || s === 403)) {
      offline = false; failures = 0; nextPoll = Date.now() + 15000;
      return loginView();
    }
    if (needs.status !== 200 || rail.status !== 200 || !Array.isArray(needs.body.items) || !Array.isArray(rail.body.workspaces)) {
      if ([needs.status, rail.status].some((s) => s === 0 || s >= 500)) {
        offline = true;
        failures = retrying ? failures + 1 : 0;
        nextPoll = Date.now() + retryDelay(failures);
      } else { offline = false; failures = 0; nextPoll = Date.now() + 15000; }
      return;
    }
    offline = false; failures = 0; nextPoll = Date.now() + 15000;
    if (!user || user !== state.user) return;
    liveItems = { user_id: user.user_id, items: needs.body.items ?? [] };
    state.rail.rows = railRows(rail.body);
    state.rail.hubName = rail.body.hub?.name || "Hub";
    if (frame?.isConnected) {
      reconcileRows(frame.querySelector(".rail"), railView());
      railSnapshot = JSON.stringify(state.rail);
    }
    if (frame?.isConnected && parseRoute(location.hash).kind === "home" && frame.querySelector(".first-run-root") && firstRunOutgrown(state.rail.rows, liveItems.items)) {
      void home(); // the first workspace connected while this page was open: draw the full home
      return;
    }
    // P04: the hub Home's Shared tickets card follows the same list; a change (or an ended share) redraws Home once
    if (frame?.isConnected && parseRoute(location.hash).kind === "home" && homeData?.user_id === user.user_id) {
      const sharedNow = await api("GET", "/me/shared");
      if (user !== state.user) return;
      if (sharedNow.status === 200 && Array.isArray(sharedNow.body.items)) {
        const before = JSON.stringify(homeData.shared) + JSON.stringify(homeData.ended);
        const ended = trackShared(user.user_id, sharedNow.body.items);
        if (before !== JSON.stringify(sharedNow.body.items) + JSON.stringify(ended)) { void home(); return; }
      }
    }
    const current = frame?.isConnected && parseRoute(location.hash).kind === "home" ? frame.querySelector(".needs-you") : null;
    if (current && homeData?.user_id === user.user_id) {
      homeData.items = liveItems.items;
      homeData.offline = needs.body.offline ?? [];
      homeData.workspaces = state.rail.rows.map((w) => ({ ...homeData.workspaces.find((old) => old.workspace_id === w.id), ...w, workspace_id: w.id }));
      const fresh = h("div", {}, homeView(homeData));
      const anchor = [...current.querySelectorAll("article")].find((el) => el.getBoundingClientRect().top >= 0);
      const top = anchor?.getBoundingClientRect().top;
      reconcileRows(current, fresh.querySelector(".needs-you"));
      reconcileRows(frame.querySelector(".health-strip"), fresh.querySelector(".health-strip"));
      const summary = frame.querySelector(".company-summary");
      if (summary) summary.textContent = fresh.querySelector(".company-summary").textContent;
      if (anchor?.isConnected) window.scrollBy(0, anchor.getBoundingClientRect().top - top);
    }
  } finally { polling = false; updateOffline(); }
}
setInterval(() => {
  if (offline) updateOffline();
  if (shouldPoll({ now: Date.now(), due: nextPoll, visible: document.visibilityState === "visible", active: !!state.user || offline, busy: polling })) void pollLive();
}, 1000);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && (state.user || offline)) void pollLive();
});

function openShortcuts() {
  dialog(h("h1", { text: "Keyboard shortcuts" }), factsList([
    ["g h", "Home"], ["g w", "Workspaces"], ...(state.user?.is_admin ? [["g s", "Services"], ["g a", "Admin"]] : []),
    ["/ or ⌘K / Ctrl+K", "Search"], ["↑ / ↓, Enter", "Choose a search result"], ["Esc", "Close dialog"], ["?", "Keyboard shortcuts"],
  ]), h("button", { text: "Close", onclick: closeDialog }));
}
async function openSearch() {
  if (!state.user || document.querySelector('[role="dialog"]')) return;
  const user = state.user;
  let people = [], shared = [], index = 0, matches = [];
  const results = h("div", { id: "search-results", role: "listbox", "aria-label": "Search results" });
  const input = h("input", { type: "search", placeholder: "Search pages, workspaces, tickets…", "aria-label": "Search everything", role: "combobox", "aria-controls": "search-results", "aria-expanded": "true", "aria-autocomplete": "list" });
  const choose = () => {
    if (state.user !== user) return;
    const entry = matches[index];
    if (!entry) return;
    closeDialog();
    if (entry.action === "connect") pendingConnect = true;
    navigate(entry.hash);
  };
  const draw = () => {
    if (state.user !== user) { results.replaceChildren(); return; }
    matches = filterSearch(searchEntries({ admin: user.is_admin, workspaces: state.rail.rows, items: liveItems.user_id === user.user_id ? liveItems.items : [], people, shared }), input.value);
    index = Math.max(0, Math.min(index, matches.length - 1));
    results.replaceChildren();
    let group;
    matches.forEach((entry, i) => {
      if (entry.group !== group) { group = entry.group; results.append(h("div", { class: "search-group", role: "presentation", text: group })); }
      results.append(h("div", { id: "search-result-" + i, role: "option", "aria-selected": String(i === index), class: "search-result" + (i === index ? " selected" : ""), text: renderText(entry.label), onclick: () => { index = i; choose(); } }));
    });
    if (!matches.length) results.append(h("p", { text: "Nothing found. Try another name or a shorter search." }));
    if (matches.length) input.setAttribute("aria-activedescendant", "search-result-" + index);
    else input.removeAttribute("aria-activedescendant");
  };
  input.addEventListener("input", () => { index = 0; draw(); });
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault(); index = focusIndex(index, matches.length, event.key === "ArrowUp"); draw();
      results.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
    } else if (event.key === "Enter") { event.preventDefault(); choose(); }
  });
  const layer = dialog(h("h1", { text: "Search everything" }), input, results, h("button", { text: "Close", onclick: closeDialog }));
  layer.classList.add("search-layer");
  draw(); input.focus();
  const [needs, rail, users, sharedRes] = await Promise.all([api("GET", "/me/needs-you"), api("GET", "/rail"), user.is_admin && !user.needs_sign_in ? api("GET", "/admin/users") : Promise.resolve(null), api("GET", "/me/shared")]);
  if (!layer.isConnected || state.user !== user) return;
  if (sharedRes.status === 200) shared = sharedRes.body.items ?? [];
  liveItems = { user_id: user.user_id, items: needs.status === 200 ? needs.body.items ?? [] : [] };
  if (rail.status === 200) state.rail.rows = railRows(rail.body);
  if (users?.status === 200) people = users.body.users ?? [];
  draw();
}
let prefixUntil = 0, pendingConnect = false;
document.addEventListener("keydown", (event) => {
  if (!state.user || event.defaultPrevented || event.isComposing || document.querySelector('[role="dialog"]')) return;
  const typing = !!event.target.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"])');
  const action = shortcut(event.key, { typing, meta: event.metaKey, ctrl: event.ctrlKey, alt: event.altKey, prefix: Date.now() < prefixUntil, admin: state.user.is_admin });
  prefixUntil = 0;
  if (!action) return;
  event.preventDefault();
  if (action === "prefix") prefixUntil = Date.now() + 1000;
  else if (action === "search") void openSearch();
  else if (action === "shortcuts") openShortcuts();
  else navigate("#/" + action);
});
// The action opens the existing Connect UI; it does not mint an invite automatically.
new MutationObserver(() => {
  if (!pendingConnect) return;
  const button = document.getElementById("connect-workspace");
  if (button) { pendingConnect = false; document.getElementById("admin-tab-2")?.click(); button.focus(); }
}).observe(root, { childList: true, subtree: true });

// --- fragment navigation ----------------------------------------------------------------------------------------------
function navigate(hash) {
  if (location.hash === hash) return renderRoute();
  location.hash = hash;
}

// A ticket shared with me, read only. Everything comes from GET /me/shared (caller-scoped): the page shows nothing the
// list does not already give this person. A ticket that is no longer in the list means the sharing ended.
async function sharedTicketPage(route) {
  const finishLoading = loading();
  pageLoading("home");
  try {
    if (!(await refresh())) return loginView();
    await loadRail();
    const res = await api("GET", "/me/shared");
    const it = res.status === 200 ? (res.body.items ?? []).find((x) => x.workspace_id === route.workspace && x.key === route.key) : null;
    const back = h("button", { text: it ? "Back" : "Back to Home", onclick: () => navigate("#/home") });
    if (res.status !== 200) return shell("home", h("h1", { text: "Shared ticket" }), h("p", { text: "This ticket could not be loaded right now. Try again in a moment." }), back);
    if (!it) return shell("home", h("h1", { text: "Sharing ended" }), h("p", { text: "The owner stopped sharing this ticket." }), back);
    shell("home", h("h1", { text: renderText(it.title ?? it.key) }),
      h("p", { class: "small muted", text: [it.shared_by ? "Shared by " + renderText(it.shared_by) : "Shared with you", it.status ? renderText(it.status) : null, "read only"].filter(Boolean).join(" · ") }),
      it.deadline ? h("p", { class: "small muted", text: "Due " + renderText(it.deadline) }) : null,
      it.description ? h("p", { class: "prewrap", text: renderText(it.description) }) : null, back);
  } finally { finishLoading(); }
}

async function workspacePage(route) {
  const finishLoading = loading();
  pageLoading("workspaces", "workspace");
  try {
    if (!(await refresh())) return loginView();
    await loadRail();
    const w = state.rail.rows.find((row) => row.id === route.workspace);
    if (!w) return shell("workspaces", h("h1", { text: "Workspace unavailable" }), h("p", { text: "This workspace is not in your list." }));
    const [needs, sharedRes] = await Promise.all([api("GET", "/me/needs-you"), api("GET", "/me/shared?workspace_id=" + encodeURIComponent(w.id))]);
    const items = needs.status === 200 ? needs.body.items.filter((it) => it.workspace_id === w.id) : [];
    const sharedTickets = sharedRes.status === 200 ? (sharedRes.body.items ?? []).filter((it) => it.workspace_id === w.id) : [];
    if (route.kind === "item" && w.online) {
      const item = items.find((it) => it.key === route.key || it.item_id === route.key);
      if (item) return reviewItem(item);
      return shell("workspaces", h("h1", { text: "This item is no longer waiting for you" }));
    }
    shell("workspaces", h("h1", { text: renderText(w.name) + (w.online ? "" : " is " + workspaceStatus(w)) }),
      h("p", { text: w.online ? "Online" : "Hand-offs and approvals wait until this workspace is back." }),
      h("p", { text: needs.status === 200 && w.online ? items.length + " things need you" : w.badge ? w.badge + " things need you. They wait until the workspace is back." : "Waiting items will be shown when the workspace is back." }),
      sharedTicketsView(sharedTickets),
      w.online ? h("button", { text: "Open workspace", disabled: !w.canOpen, onclick: () => openWorkspace(w) }) : null,
      w.online && !w.canOpen ? h("p", { text: OPEN_FIRST }) : null);
  } finally { finishLoading(); }
}

// "Shared tickets (n)": read-only rows; the owner sees with whom a ticket is shared, anyone else by whom.
function sharedTicketsView(list) {
  if (!list.length) return null;
  return h("section", { "aria-label": "Shared tickets" }, h("h2", { text: "Shared tickets (" + list.length + ")" }),
    list.map((it) => h("div", { class: "list-row" }, h("b", { text: renderText(it.title ?? it.key ?? "Ticket") }),
      h("div", { class: "small muted", text: Array.isArray(it.shared_with) ? (it.shared_with.length ? "Shared with " + it.shared_with.map(renderText).join(", ") : "Only you") : it.shared_by ? "Shared by " + renderText(it.shared_by) : "Shared" }))));
}

// Serialize renders so an earlier slow read cannot overwrite the final route.
let routing = false;
let pendingRoute = false;
// A page module's render gets the console's own helpers (the same object for every page). The gate is HERE, not left to the
// page: the session is read first, nobody signed in goes to sign-in, and a non-admin never reaches an admin page's render
// (a new page that forgets its own check is still gated; the pages keep theirs). The page's first refresh() is answered
// from this one, so a page does not ask the server twice.
const pageRender = (kind) => {
  const pg = PAGES.find((x) => x.path === kind);
  if (!pg) return null;
  return async () => {
    if (!(await refresh())) return loginView();
    if (pg.nav.admin && !state.user?.is_admin) return home();
    let first = true;
    const pageRefresh = async () => (first ? ((first = false), true) : refresh());
    return pg.render({ api, refresh: pageRefresh, loadRail, state, loginView, home, reauthView, pageLoading, shell, say, banner });
  };
};
registerPages(PAGES.map((x) => x.path));
async function renderRoute() {
  pendingRoute = true;
  if (routing) return;
  routing = true;
  try {
    while (pendingRoute) {
      pendingRoute = false;
      const route = parseRoute(location.hash);
      if (route.kind === "ticket") await landFromTicket(route.token);
      else if (route.kind === "setup") setupView(route.token);
      else if (route.kind === "invite") show(inviteView(route.token, { api, say, signIn: () => loginView(), openHub: () => afterLogin(), finished: () => history.replaceState(null, "", location.pathname) }));
      else if (route.kind === "access") await accessView(route.workspace, route.key);
      else if (route.kind === "addon") await addonView(route.workspace, route.key);
      else if (route.kind === "settings") await settingsView(route.workspace, route.key);
      else if (route.kind === "shared") await sharedTicketPage(route);
      else if (route.workspace) await workspacePage(route);
      else await ({ home, approvals, workspaces, services, admin, activity, account, handoffs, phones, register: registerRoute }[route.kind] ?? pageRender(route.kind) ?? home)();
    }
  } catch { loginView("Something went wrong. Please sign in again."); }
  finally { routing = false; }
}
window.addEventListener("hashchange", renderRoute);
renderRoute();
