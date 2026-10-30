// The Company secrets page (M3, #1431-#1433): a value an admin types here is locked in THIS browser, once for every
// workspace that may read it, and only the locked copies are sent. The hub never sees the value. The value lives in one local
// variable while it is locked; it is never kept in a page-wide variable, in the address, in browser storage or in a log, and
// there is no automatic retry: after any outcome the box is empty and the person types it again.
import { sealOrgSecret } from "../seal.js";
import { loading } from "../feel.js";
import { h, text, withNames } from "../ui.js";

const DAY = 86400000;
const NAME = /^[a-z][a-z0-9-]{0,39}$/;
const info = (label, body) => h("details", { class: "info" }, h("summary", { "aria-label": label, text: "i" }), h("p", { class: "small muted", text: body }));
const none = () => h("div", { class: "empty" }, h("b", { text: "No company secrets yet" }), h("div", { class: "small", text: "Add one below and choose which workspaces may read it." }));
const wsName = (w) => text(w?.name || "A workspace");
const plainDate = (ms) => new Date(ms).toLocaleDateString("en", { day: "numeric", month: "short", year: "numeric" });

// Mirrors the rule the browser seal, the hub and the workspace all apply; the hub decides in the end.
export function valueProblem(value) {
  if (typeof value !== "string" || value.length < 8 || value.length > 4096) return "Use between 8 and 4096 characters.";
  if (!/^[\x20-\x7e]+$/.test(value)) return "Use plain letters, numbers and symbols only (no accents, no line breaks).";
  if (value[0] === " " || value[value.length - 1] === " ") return "Remove the spaces at the start and the end.";
  return null;
}
export function nameProblem(name) {
  return NAME.test(name) ? null : "Use a short name of lower-case letters, numbers and dashes.";
}

const fromB64u = (s) => Uint8Array.from(atob(s.replaceAll("-", "+").replaceAll("_", "/")), (c) => c.charCodeAt(0));
const toB64u = (u) => btoa(String.fromCharCode(...u)).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");

// Locks the value for every recipient and sends only the locked copies. `api` is the console's request function. The value is
// a parameter of this one call and nowhere else; every outcome is a plain code, never text from the server.
export async function saveSecret({ api, seal = sealOrgSecret, name, value, days, shared, workspaces }) {
  let step = "recipients";
  try {
    const r = await api("POST", "/admin/secrets/recipients", { name, shared, workspaces });
    if (r.status !== 200) return { ok: false, code: "failed" };
    const rec = r.body;
    const expires = rec.now_ms + days * DAY;
    const wraps = [];
    step = "seal";
    for (const x of rec.recipients) {
      const sealed = await seal({
        wxkPub: fromB64u(x.wxk_pub), hubId: rec.hub_id, workspaceId: x.workspace_id, secretId: rec.secret_id,
        version: rec.version, wxkVersion: x.wxk_version, expiresMs: expires, name, value,
      });
      wraps.push({ workspace_id: x.workspace_id, wxk_version: x.wxk_version, sealed: toB64u(sealed) });
    }
    step = "save";
    const s = await api("POST", "/admin/secrets", { name, secret_id: rec.secret_id, version: rec.version, expires_ms: expires, shared, workspaces, wraps });
    if (s.status === 200) return { ok: true, code: "saved", without_key: rec.without_key ?? [] };
    return { ok: false, code: s.status === 409 ? "changed" : "failed" };
  } catch {
    return { ok: false, code: step === "seal" ? "seal" : "failed" };
  }
}

const SAY = {
  failed: "The secret was not saved. Type it again to try again.",
  changed: "The workspaces changed while saving. Type the secret again.",
  seal: "The secret could not be locked in this browser. Type it again.",
};

function valueBox(label) {
  return h("input", { type: "password", autocomplete: "off", spellcheck: "false", "aria-label": label });
}
function expiryBox() {
  const sel = h("select", { "aria-label": "Valid for" },
    h("option", { value: "7", text: "7 days" }), h("option", { value: "30", text: "30 days" }), h("option", { value: "90", text: "90 days" }), h("option", { value: "365", text: "1 year" }));
  sel.value = "90";
  return sel;
}
function shareBox(workspaces, current, chosen) {
  const sel = h("select", { "aria-label": "Who can read it" }, h("option", { value: "all", text: "All workspaces" }), h("option", { value: "chosen", text: "Only chosen workspaces" }));
  sel.value = current;
  const boxes = workspaces.map((w) => {
    const box = h("input", { type: "checkbox", "data-ws": w.workspace_id });
    box.checked = chosen.includes(w.workspace_id);
    return { w, box };
  });
  const list = h("div", { class: "share-list" }, boxes.map(({ w, box }) => h("label", { class: "list-row row" }, box, h("span", { class: "grow", text: wsName(w) }))));
  return { sel, list, read: () => ({ shared: sel.value === "chosen" ? "chosen" : "all", workspaces: sel.value === "chosen" ? boxes.filter((x) => x.box.checked).map((x) => x.w.workspace_id) : [] }) };
}

// One form that takes a value: used for a new secret and for "Enter the value again". The box is emptied before anything is sent
// and again when the outcome is known, whatever it is.
function entryForm({ workspaces, name, nameIn, share, submitLabel, actions }) {
  const input = valueBox("Value of the secret");
  const days = expiryBox();
  const note = h("p", { class: "small", role: "alert" });
  const go = h("button", { type: "button", class: "primary", text: submitLabel, onclick: async () => {
    const nm = name ?? nameIn.value ?? "";
    const bad = nameProblem(nm) ?? valueProblem(input.value);
    if (bad) { note.textContent = bad; return; }
    const value = input.value;
    input.value = "";
    go.disabled = true;
    note.textContent = "";
    let out;
    try {
      const pick = share.read();
      out = await actions.save?.({ name: nm, value, days: Number(days.value), ...pick });
    } finally {
      input.value = "";
      go.disabled = false;
    }
    if (out && !out.ok) note.textContent = SAY[out.code] ?? SAY.failed;
  } });
  return { node: h("div", { class: "secret-entry" },
    h("label", { class: "small", text: "Value" }), input,
    h("label", { class: "small", text: "Valid for" }), days,
    h("p", { class: "small muted", text: "Recommended: 90 days. Pick 7 days for something temporary." }), note, go), input };
}

export function secretsView(data, actions = {}) {
  const workspaces = data.workspaces ?? [];
  const wsById = Object.fromEntries(workspaces.map((w) => [w.workspace_id, w]));
  const card = (s) => {
    const needs = Object.values(s.state ?? {}).some((x) => x.state !== "ready");
    const rows = Object.entries(s.state ?? {}).map(([id, x]) => h("div", { class: "list-row row" },
      h("span", { class: "grow" }, wsName(wsById[id]),
        x.state === "ready" ? null : h("div", { class: "small muted", text: "Waiting for you to type the value again for this workspace." })),
      h("span", { class: "chip " + (x.state === "ready" ? "ok" : "warn"), text: x.state === "ready" ? "Ready" : "Needs the value" })));
    const share = shareBox(workspaces, s.shared, s.workspaces ?? []);
    const again = entryForm({ workspaces, name: s.name, share: { read: () => ({ shared: s.shared, workspaces: s.workspaces ?? [] }) }, submitLabel: "Save the value", actions });
    again.node.hidden = true;
    let sure = false;
    const remove = h("button", { type: "button", text: "Remove", onclick: () => {
      if (!sure) { sure = true; remove.textContent = "Sure? Remove it"; return; }
      actions.remove?.(s);
    } });
    const reenter = needs ? h("button", { type: "button", class: "primary", text: "Enter the value", onclick: () => { again.node.hidden = !again.node.hidden; } }) : null;
    return h("article", { class: "card secret", "data-secret": s.name },
      h("div", { class: "row" }, h("h3", { class: "grow", text: s.name }), remove),
      h("p", { class: "small muted", text: (s.expired ? "Expired on " : "Expires on ") + plainDate(s.expires_ms) + ". " + (s.shared === "all" ? "Shared with all workspaces." : "Shared with chosen workspaces only.") }),
      h("p", { class: "small muted", text: "Used " + (s.used_this_week ?? 0) + " times this week (as reported by the workspaces)." }),
      rows,
      reenter ? h("div", { class: "row" }, reenter) : null,
      again.node,
      h("div", { class: "row" }, h("h4", { class: "grow", text: "Who can read it" }),
        info("About who can read it", "A workspace you add here needs the value entered again. A workspace that already received the value keeps it.")),
      share.sel, share.list,
      h("button", { type: "button", text: "Save who can read it", onclick: () => { const p = share.read(); actions.share?.(s.name, p.shared, p.workspaces); } }));
  };
  const nameIn = h("input", { type: "text", autocomplete: "off", spellcheck: "false", "aria-label": "Name of the new secret", placeholder: "for example: crm-key", maxlength: "40" });
  const share = shareBox(workspaces, "all", []);
  const form = entryForm({ workspaces, nameIn, share, submitLabel: "Save secret", actions });
  return h("div", { class: "secrets" },
    h("div", { class: "row" }, h("h1", { class: "grow", text: "Company secrets" }),
      info("About company secrets", "A secret is a key your tools need, such as the sign-in key of your CRM. You type it here once. It is locked in your browser for each workspace you choose, and only those workspaces can read it. Agents never see it.")),
    h("p", { class: "banner warn", role: "note", text: "The hub could read this value if it were taken over." }),
    (data.secrets ?? []).length ? (data.secrets ?? []).map(card) : none(),
    h("section", { class: "card" }, h("h3", { text: "New secret" }),
      nameIn, form.node, h("h4", { text: "Who can read it" }), share.sel, share.list));
}

export async function secretsPage(d) {
  const finish = loading();
  d.pageLoading("secrets");
  try {
    if (!(await d.refresh())) return d.loginView();
    if (!d.state.user.is_admin) return d.home();
    if (d.state.user.needs_sign_in) return d.reauthView();
    await d.loadRail();
    const r = await d.api("GET", "/admin/secrets");
    if (r.status === 403 && r.body.error === "reauth_required") return d.reauthView();
    if (r.status !== 200) return d.shell("secrets", h("h1", { text: "Company secrets" }), h("p", { role: "alert", text: "Cannot read the company secrets. " + d.say(r.body.error) }));
    const say = (ok, x) => { d.state.notice = x.status === 200 ? d.banner("ok", ok, null) : d.banner("bad", "Not changed", d.say(x.body.error ?? "unavailable")); };
    const save = async (f) => {
      const out = await saveSecret({ api: d.api, ...f });
      if (out.ok) { d.state.notice = d.banner("ok", "Saved", out.without_key.length ? "Some workspaces have no key yet and need the value again later." : null); await secretsPage(d); }
      return out;
    };
    const remove = async (s) => { say("Removed", await d.api("DELETE", "/admin/secrets/" + s.name)); await secretsPage(d); };
    const share = async (name, shared, workspaces) => { say("Saved", await d.api("PUT", "/admin/secrets/" + name + "/share", { shared, workspaces })); await secretsPage(d); };
    d.shell("secrets", d.state.notice, secretsView({ ...r.body, workspaces: withNames(r.body.workspaces, d.state.rail?.rows ?? []) }, { save, remove, share }));
    d.state.notice = null;
  } finally { finish(); }
}

// The registry entry: the console's router, tabs and file list are built from this one export (no other file names a page).
export const page = { path: "secrets", nav: { label: "Secrets", admin: true, needsWorkspace: true, after: "mcp" }, render: secretsPage };
