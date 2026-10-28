// The hub console, minimum for the browser sign flow (design-a8): sign in, register this browser, see what needs me,
// review one decision and sign it. The DOM is built with createElement and textContent only (lint: tests/web_lint),
// every string from the workspace goes through renderText first, and nothing here calls a signer except the prompt.
import { CanonError, parseStrict } from "./cj.js";
import { ChannelError, deleteChannel, generateKey, loadChannel, saveChannel, signProof, signRegistration } from "./channel.js";
import { homeView } from "./home.js";
import { adminView } from "./admin.js";
import { servicesView } from "./services.js";
import { workspacesView, workspaceDetailsView } from "./workspaces.js";
import { OPEN_FIRST, colour, initials, landing, landingPlan, railRows, safeUrl, ticketFromHash } from "./switch.js";
import { Refusal, addonFacts, addonTermsOk, settingsFacts, settingsTermsOk, checkBindings, checkPrepared, describe, renderMulti, renderText, shortHash, verifyQuestion, DECISIONS } from "./render.js";

const root = document.getElementById("app");
const state = { user: null, me: null, channels: [], notice: null, rail: { hubName: "Hub", rows: [] }, menu: false };

// --- DOM helpers (no HTML strings anywhere) ----------------------------------------------------------------------------
function h(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, "");
    else if (v !== false && v != null) e.setAttribute(k, String(v));
  }
  for (const c of kids.flat()) if (c != null && c !== false) e.append(c.nodeType ? c : document.createTextNode(String(c)));
  return e;
}
const show = (...nodes) => { root.classList.remove("shelled"); root.replaceChildren(...nodes.flat().filter(Boolean)); };
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
    return { status: 0, body: { error: "unavailable" } };
  }
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(await res.arrayBuffer());
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
  malformed: "The answer could not be read safely.",
  unavailable: "The hub cannot be reached.",
  bad_ticket: "This link expired. Go back and click again.",
  no_origin: "Open that workspace's dashboard once first, then try again.",
};
const say = (code) => WORDS[code] ?? "Refused (" + renderText(code) + ").";

// --- views -------------------------------------------------------------------------------------------------------------
function loginView(err) {
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
  }, field("Name or user id", id), field("Password", pw), field("6-digit code", code), h("div", { class: "row top" }, h("button", { class: "pri", type: "submit", text: "Sign in" })));
  show(h("h1", { text: "Sign in to your hub" }), err ? banner("bad", "Not signed in", err) : null, form);
}

// First admin (prototype): the installer's one-time link opens this page; the hub shows the authenticator secret once.
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
        banner("ok", "Write these down", "They are shown once."),
        h("dl", { class: "facts" }, h("dt", { text: "User id" }), h("dd", { class: "fp", id: "new-user-id", text: r.body.user_id }), h("dt", { text: "Authenticator secret (add it to your authenticator app)" }), h("dd", { class: "fp", id: "new-totp", text: r.body.totp_secret })),
        h("div", { class: "row top" }, h("button", { class: "pri", text: "Continue to sign in", onclick: () => loginView() })),
      );
    },
  }, field("Your name", name), field("Choose a password (10 or more characters)", pw), h("div", { class: "row top" }, h("button", { class: "pri", type: "submit", text: "Create the admin" })));
  show(h("h1", { text: "Set up your hub" }), err ? banner("bad", "Not created", err) : null, h("p", { class: "muted", text: "This creates the first admin, with a password and an authenticator code." }), form);
}

async function refresh() {
  const [who, chans] = await Promise.all([api("GET", "/session"), api("GET", "/me/channels")]);
  if (who.status !== 200) return false;
  state.user = who.body;
  state.channels = chans.status === 200 ? chans.body.channels : [];
  state.me = await loadChannel();
  return true;
}

function shell(active, ...nodes) {
  const tab = (id, label, go, cls) => h("a", { href: "#", class: cls + (active === id ? " on" : ""), "aria-current": active === id ? "page" : null, text: label, onclick: (e) => { e.preventDefault(); go(); } });
  const name = renderText(state.user?.name ?? "");
  const hubName = renderText(state.rail.hubName);
  const side = h("aside", { class: "side", "aria-label": "Hub" },
    h("div", { class: "logo" }, h("i"), h("span", { text: "orch" })),
    h("div", { class: "hubbox" }, h("span", { text: hubName })),
    tab("home", "Home", home, "nv"), tab("workspaces", "Workspaces", workspaces, "nv"), state.user?.is_admin ? tab("services", "Services", services, "nv") : null, state.user?.is_admin ? tab("admin", "Admin", admin, "nv") : null,
    h("div", { class: "sp" }), h("div", { class: "who" }, h("span", { text: name })));
  const top = h("header", { class: "topbar" }, h("span", { class: "t", text: hubName }),
    h("nav", { class: "navtabs top-nav", "aria-label": "Sections" }, tab("home", "Home", home, "nav"), tab("workspaces", "Workspaces", workspaces, "nav"), state.user?.is_admin ? tab("services", "Services", services, "nav") : null, state.user?.is_admin ? tab("admin", "Admin", admin, "nav") : null),
    h("span", { class: "grow" }), h("span", { class: "small muted", text: name }), signOutMenu());
  show(h("div", { class: "app" }, railView(), side, h("div", { class: "main" }, top, h("div", { class: "content" }, nodes.flat(2)))));
  root.classList.add("shelled");
}

// --- the company rail: this hub, then every workspace of mine (GET /rail); one click switches, no second sign-in ----------
function railView() {
  const hub = h("button", { class: "ra hub on", title: renderText(state.rail.hubName), "aria-label": "Hub " + renderText(state.rail.hubName), "aria-current": "page", text: initials(state.rail.hubName), onclick: home });
  const items = state.rail.rows.map((w) => {
    const b = h("button", { class: "ra " + colour(w.id), "aria-label": "Open " + renderText(w.name) + (w.online ? "" : " (offline)"), disabled: !w.canOpen,
      title: w.canOpen ? renderText(w.name) : renderText(w.name) + ": " + OPEN_FIRST, onclick: () => openWorkspace(w) },
      initials(w.name, w.id), w.badge ? h("span", { class: "bd", text: String(w.badge) }) : null);
    return b;
  });
  return h("nav", { class: "rail", "aria-label": "Hub and workspaces" }, hub, items.length ? h("div", { class: "rsep" }) : null, items);
}

async function loadRail() {
  let r = await api("GET", "/rail");
  if (r.status !== 200) r = await api("GET", "/me/workspaces"); // older hub without the rail: the plain list, nothing openable
  state.rail.rows = r.status === 200 ? railRows(r.body) : [];
  const n = r.status === 200 ? r.body.hub?.name : null;
  state.rail.hubName = typeof n === "string" && n ? n : "Hub";
}

async function openWorkspace(w) {
  const r = await api("POST", "/switch/ticket", { workspace_id: w.id, return_to: "/", kind: "open" });
  const url = r.status === 200 ? safeUrl(r.body.url) : null;
  if (!url) { state.notice = banner("bad", "Cannot open " + renderText(w.name), say(r.status === 200 ? "malformed" : r.body.error)); return home(); }
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
  if (l.kind === "signout") { state.user = null; return plain(h("h1", { text: "You are signed out" }), h("p", { class: "muted", text: "Your hub session ended." }), h("button", { class: "pri", text: "Sign in again", onclick: () => loginView() })); }
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
  if (plan === "confirm") return plain(h("h1", { text: "Continue as " + name + "?" }), h("p", { class: "muted", text: "You came from a workspace. Nothing happens until you continue." }),
    h("div", { class: "row" }, h("button", { class: "pri", text: "Continue", onclick: () => redeemTicket(t, false) }), h("button", { text: "Cancel", onclick: () => loginView() })));
  const cur = renderText(p.body.signed_in_as?.name ?? "someone else");
  return plain(h("h1", { text: "Different person" }), h("p", { text: "You are signed in as " + cur + " here. This link is for " + name + "." }),
    h("div", { class: "row" }, h("button", { text: "Stay signed in as " + cur, onclick: () => home() }), h("button", { class: "pri", text: "Sign out and continue as " + name, onclick: () => redeemTicket(t, true) })));
}

// A session that came from a workspace ticket cannot use admin tools until a real password sign-in.
function reauthView() {
  show(h("div", { class: "plain" }, h("h1", { text: "Sign in again to use admin tools" }), h("p", { class: "muted", text: "You arrived from a workspace. Admin tools need your password and code." }),
    h("button", { class: "pri", text: "Sign in again", onclick: async () => { await api("DELETE", "/session"); state.user = null; loginView(); } })));
}

async function admin() {
  if (!(await refresh())) return loginView();
  state.menu = false;
  await loadRail();
  if (!state.user.is_admin) return home();
  if (state.user.needs_sign_in) return reauthView();
  const notice = state.notice;
  state.notice = null;
  const adminApi = async (m, path, body) => { const r = await api(m, path, body); if (r.status === 403 && r.body.error === "reauth_required") reauthView(); return r; };
  shell("admin", adminView({ api: adminApi, say, notice, refresh }));
}

async function workspaces() {
  if (!(await refresh())) return loginView();
  state.menu = false;
  await loadRail();
  const [ws, ho, admin] = await Promise.all([api("GET", "/me/workspaces"), api("GET", "/me/handoffs"), api("GET", "/admin/workspaces")]);
  const adminRows = admin.status === 200 ? admin.body.workspaces ?? [] : null;
  const hub = ws.body?.hub;
  const actions = {
    open: (w) => { closeDialog(); return openWorkspace({ id: w.workspace_id, name: w.name ?? "Workspace" }); },
    copy: copyText,
    connect: async () => {
      const r = await api("POST", "/admin/connect", {});
      if (r.status < 200 || r.status >= 300 || typeof r.body.link !== "string" || !Number.isFinite(r.body.expires_ms)) throw new Error("connect failed");
      return r.body;
    },
    remove: async (w) => {
      const r = await api("DELETE", "/admin/workspaces/" + encodeURIComponent(w.workspace_id));
      if (r.status < 200 || r.status >= 300) throw new Error("remove failed");
      await workspaces();
    },
    details: async (w) => {
      const r = await api("GET", "/workspaces/" + encodeURIComponent(w.workspace_id) + "/roles");
      dialog(...workspaceDetailsView(w, { hub, actions, roles: r.status === 200 ? r.body : null,
        owner: adminRows?.find((row) => row.workspace_id === w.workspace_id)?.owner_alias }),
        h("div", { class: "row" }, h("button", { text: "Close", onclick: closeDialog })));
    },
  };
  shell("workspaces", workspacesView({ workspaces: ws.status === 200 ? ws.body.workspaces ?? [] : [],
    handoffs: ho.status === 200 ? ho.body.handoffs ?? [] : [], admin: adminRows, hub, actions }));
}

async function copyText(t) {
  try { await navigator.clipboard.writeText(t); return true; } catch { return false; }
}

async function services() {
  if (!(await refresh())) return loginView();
  state.menu = false;
  await loadRail();
  if (!state.user.is_admin) return home();
  if (state.user.needs_sign_in) return reauthView();
  const r = await api("GET", "/admin/services");
  if (r.status === 403 && r.body.error === "reauth_required") return reauthView();
  if (r.status !== 200) return shell("services", h("h1", { text: "Services" }), banner("bad", "Cannot read the services", say(r.body.error)));
  shell("services", servicesView(r.body, { check: services, open: workspaces, copy: copyText }));
}

async function home() {
  if (!(await refresh())) return loginView();
  state.menu = false;
  await loadRail();
  const [ws, needs] = await Promise.all([api("GET", "/me/workspaces"), api("GET", "/me/needs-you")]);
  const mine = state.me && state.channels.find((c) => c.channel_id === state.me.channelId && c.status === "active");
  const workspaces = ws.status === 200 ? ws.body.workspaces : [];
  const dead = !mine && (state.me || state.channels.some((c) => c.status === "active" && c.kind === "browser"));
  const view = homeView({
    user: state.user,
    items: needs.status === 200 ? needs.body.items : [],
    offline: needs.status === 200 ? needs.body.offline : [],
    workspaces,
    channels: state.channels.map((c) => ({ ...c, status: c.status + (state.me?.channelId === c.channel_id ? " (this browser)" : "") })),
    thisBrowser: mine ? { state: "active", label: state.me.label } : dead ? { state: "dead", label: state.me?.label } : { state: "unregistered" },
    notice: state.notice,
    actions: { review: reviewItem, register: () => registerView(), removeChannel, forgetLocal },
  });
  shell("home", view, advancedForm(workspaces));
  state.notice = null;
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
  if (r.status === 200 && state.me?.channelId === id) await deleteChannel().catch(() => {}); // revoke first, then delete (A8-5)
  state.notice = r.status === 200 ? banner("ok", "Removed", null) : banner("bad", "Not removed", say(r.body.error));
  await home();
}

async function signOut() {
  await api("DELETE", "/session");
  state.user = null;
  state.menu = false;
  loginView();
}

function registerView(err) {
  const label = h("input", { id: "label", value: "This browser", maxlength: 60 });
  const pw = h("input", { id: "pw2", type: "password", autocomplete: "current-password", required: true });
  const code = h("input", { id: "code2", inputmode: "numeric", autocomplete: "one-time-code", maxlength: 6, required: true });
  const form = h("form", {
    onsubmit: async (ev) => {
      ev.preventDefault();
      const out = await registerBrowser(label.value.trim() || "This browser", pw.value, code.value.trim());
      pw.value = code.value = "";
      if (out) return registerView(out);
      state.notice = banner("ok", "This browser is ready", "It holds a signing key for you. You can remove it any time.");
      await home();
    },
  }, h("p", { class: "muted small", text: "Type your password and a new 6-digit code. Each code works only once, so wait for the next one if you just signed in." }), field("Browser name", label), field("Password", pw), field("6-digit code", code), h("div", { class: "row top" }, h("button", { type: "button", text: "Cancel", onclick: home }), h("button", { class: "pri", type: "submit", text: "Register this browser" })));
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
  const head = [h("h1", { text: renderText(c.ticket_title ?? it.title ?? "Question") }), h("p", { class: "sentence", text: renderMulti(c.text ?? it.title ?? "") })];
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
    h("button", { text: "Answer", onclick: () => startSign(it.workspace_id, { type: "question.answered", log: it.log, question: c.question, hash: it.hash, option: o.key }, qv) })));
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
    h("p", { class: "sentence", text: verdict ? "Give your verdict on this ticket." : "Review the " + renderText(c.gate ?? "") + " gate." }),
    h("p", { class: "small muted", text: "The content itself is not shown here. Check it where it lives; the reference below is what you are deciding on." }),
    h("details", { open: true }, h("summary", { text: "Review details" }), h("dl", { class: "facts" },
      h("dt", { text: "Content reference" }), h("dd", { class: "fp", text: renderText(shortHash(it.hash) || String(it.hash ?? "")) }),
      h("dt", { text: "Generation" }), h("dd", { text: renderText(String(c.gate_gen ?? "")) }),
      sources.flatMap((e) => [h("dt", { text: "Code to check" }), h("dd", { text: renderText(e.repo) + " " + renderText(e.sha) })]))),
    h("label", { for: "reason", text: "Reason" }),
    note,
    h("p", { class: "small muted", text: verdict ? "Needed only if you choose Fail." : "Needed only if you ask for changes." }),
    verdict
      ? h("div", { class: "row" }, h("button", { class: "pri", text: "Pass", onclick: () => act({ type: "verdict.given", outcome: "pass" }) }), h("button", { text: "Fail", onclick: () => withText({ type: "verdict.given", outcome: "fail" }) }))
      : h("div", { class: "row" }, h("button", { class: "pri", text: "Approve", onclick: () => act({ type: "gate.approved", gate: c.gate }) }), h("button", { text: "Ask for changes", onclick: () => withText({ type: "gate.changes_requested", gate: c.gate }) })),
  );
}

// After a sign-in: an add-on approval link keeps its fragment, everything else goes Home.
async function afterLogin() {
  const a = /^#\/w\/([0-9a-f]{32})\/addon\/([\w-]+)$/.exec(location.hash);
  if (a) return addonView(a[1], a[2]);
  const st = /^#\/w\/([0-9a-f]{32})\/settings\/([\w-]+)$/.exec(location.hash);
  return st ? settingsView(st[1], st[2]) : home();
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
    h("p", { class: "sentence", text: "The workspace " + renderText(mine.name ?? wsId.slice(0, 8)) + " asks to install and turn on " + renderText(terms.name) + "." }),
    h("dl", { class: "facts" }, addonFacts(terms).flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })])),
    h("p", { class: "small muted", text: "Approving here lets the workspace's computer install exactly this package. You can check the fingerprint against the one shown in the workspace." }),
    h("div", { class: "row" }, h("button", { class: "pri", text: "Review and approve", onclick: () => startSign(wsId, action) }))));
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
    h("p", { class: "sentence", text: "This link asks to change how long things are held in " + renderText(mine.name ?? wsId.slice(0, 8)) + ". Check the change below; nothing happens until you approve." }),
    h("dl", { class: "facts" }, settingsFacts(terms).flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })])),
    h("div", { class: "row" }, h("button", { class: "pri", text: "Review and approve", onclick: () => startSign(wsId, action) }))));
}

function advancedForm(workspaces) {
  const ws = h("select", {}, workspaces.filter((w) => w.can_start).map((w) => h("option", { value: w.workspace_id, text: renderText(w.name ?? w.workspace_id.slice(0, 8)) })));
  const type = h("select", {}, Object.keys(DECISIONS).filter((t) => t !== "question.answered").map((t) => h("option", { value: t, text: t })));
  const inputs = Object.fromEntries(["log", "question", "gate", "outcome", "hash", "option", "text"].map((k) => [k, h("input", { id: "f-" + k })]));
  const go = h("button", { class: "pri", text: "Review", onclick: () => {
    const action = { type: type.value, log: inputs.log.value.trim() };
    for (const k of DECISIONS[type.value].filter((k) => k in inputs)) if (inputs[k].value.trim()) action[k] = inputs[k].value.trim();
    startSign(ws.value, action);
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
  const sign = h("button", { class: "pri", id: "sign", disabled: true, text: "Please review…" });
  const arm = () => { sign.disabled = true; sign.textContent = "Please review…"; setTimeout(() => { if (document.visibilityState === "visible") { sign.disabled = false; sign.textContent = "Sign and send"; } }, 1000); };
  const onVis = () => arm();
  document.addEventListener("visibilitychange", onVis);
  window.addEventListener("focus", onVis);
  const done = () => { document.removeEventListener("visibilitychange", onVis); window.removeEventListener("focus", onVis); closeDialog(); };
  sign.addEventListener("click", async (ev) => {
    if (!ev.isTrusted || sign.disabled) return; // a script's .click() never signs
    sign.disabled = true;
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
    h("h1", { text: d.type === "addon.requested" ? "Approve an add-on" : d.type === "settings.changed" ? "Approve a settings change" : "Review your answer" }),
    h("p", { class: "sentence", text: d.sentence }),
    h("dl", { class: "facts" }, h("dt", { text: "On behalf of" }), h("dd", { text: renderText(state.user.name ?? "you") }), h("dt", { text: "Signature reference" }), h("dd", { class: "fp", text: d.fingerprint }), d.facts.length ? d.facts.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { class: k === "Content reference" ? "fp" : "", text: v })]) : d.contentHash ? [h("dt", { text: "Content reference" }), h("dd", { class: "fp", text: d.contentHash })] : null),
    h("details", {}, h("summary", { text: "Signed details" }), factsList(d.fields)),
    h("p", { class: "small muted who-signs", text: "You confirm with the key held by this browser. " + "The hub" + " signs this approval for you." }),
    h("div", { class: "row" }, h("button", { text: "Cancel", onclick: done }), sign),
  );
  arm();
}

// --- start -------------------------------------------------------------------------------------------------------------
(async () => {
  const t = ticketFromHash(location.hash);
  if (t) return landFromTicket(t);
  const m = /^#setup=([\w-]+)$/.exec(location.hash);
  if (m) return setupView(m[1]);
  const a = /^#\/w\/([0-9a-f]{32})\/addon\/([\w-]+)$/.exec(location.hash);
  if (a) return addonView(a[1], a[2]);
  const st = /^#\/w\/([0-9a-f]{32})\/settings\/([\w-]+)$/.exec(location.hash);
  if (st) return settingsView(st[1], st[2]);
  try { if (!(await refresh())) return loginView(); await home(); } catch { loginView("Something went wrong."); }
})();
