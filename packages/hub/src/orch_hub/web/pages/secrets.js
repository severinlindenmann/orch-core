// The Company secrets page (M3, #1431-#1433): a value an admin types here is locked in THIS browser, once for every
// workspace that may read it, and only the locked copies are sent. The hub never sees the value. The value lives in one local
// variable while it is locked; it is never kept in a page-wide variable, in the address, in browser storage or in a log, and
// there is no automatic retry: after any outcome the box is empty and the person types it again.
import { sealOrgSecret } from "../seal.js";
import { loading } from "../feel.js";
import { attentionList, h, text, withNames } from "../ui.js";

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
function expiryBox(key) {
  const sel = h("select", { "aria-label": "Valid for", name: key + "-expiry" },
    h("option", { value: "7", text: "7 days" }), h("option", { value: "30", text: "30 days" }), h("option", { value: "90", text: "90 days" }), h("option", { value: "365", text: "1 year" }));
  sel.value = "90";
  return sel;
}
const HUB_CAN_READ = "The hub could read this value if it were taken over.";
// `fresh` is the new-secret form: it starts on "Only chosen workspaces" with the only workspace already ticked, and warns
// on the form only while "All workspaces" is picked.
// `onChange` runs after the select or a box changes (the Manage panel uses it to show Save only when something differs).
function shareBox(workspaces, current, chosen, fresh = false, onChange = null, key = "new") {
  // Stable names keep the shell from restoring an expiry value into this select when cards are added.
  const sel = h("select", { "aria-label": "Who can read it", name: key + "-share" }, h("option", { value: "all", text: "All workspaces" }), h("option", { value: "chosen", text: "Only chosen workspaces" }));
  sel.value = current === "all" ? "all" : "chosen"; // never a value that matches no option: the box would look empty
  const boxes = workspaces.map((w) => {
    const box = h("input", { type: "checkbox", "data-ws": w.workspace_id, name: key + "-workspace-" + w.workspace_id });
    box.checked = chosen.includes(w.workspace_id);
    return { w, box };
  });
  const list = h("div", { class: "share-list" }, boxes.map(({ w, box }) => h("label", { class: "list-row row" }, box, h("span", { class: "grow", text: wsName(w) }))));
  const warn = h("p", { class: "small muted", role: "note", text: HUB_CAN_READ });
  const read = () => ({ shared: sel.value === "chosen" ? "chosen" : "all", workspaces: sel.value === "chosen" ? boxes.filter((x) => x.box.checked).map((x) => x.w.workspace_id) : [] });
  const saved = JSON.stringify(read());
  const sync = () => { warn.hidden = sel.value !== "all"; };
  const changed = () => { sync(); onChange?.(JSON.stringify(read()) !== saved); };
  sel.addEventListener("change", changed);
  for (const { box } of boxes) box.addEventListener("change", changed);
  sync();
  const node = fresh ? h("div", { class: "share-box" }, h("label", { class: "small", text: "Who can read it" }), sel,
    warn, list) : null;
  return { sel, list, node, warn, read };
}

// One form that takes a value: used for a new secret and for "Enter the value again". The box is emptied before anything is sent
// and again when the outcome is known, whatever it is.
function entryForm({ name, nameIn, share, submitLabel, actions }) {
  const input = valueBox("Value of the secret");
  input.setAttribute("name", (name ? "secret-" + name : "new") + "-value");
  const days = expiryBox(name ? "secret-" + name : "new");
  const note = h("p", { class: "small", role: "alert" });
  const go = h("button", { type: "button", class: "primary", text: submitLabel, onclick: async () => {
    const nm = name ?? nameIn.value ?? "";
    const bad = nameProblem(nm) ?? valueProblem(input.value);
    if (bad) { note.textContent = bad; return; }
    if (share.node && share.read().shared === "chosen" && !share.read().workspaces.length) { note.textContent = "Pick at least one workspace."; return; }
    const value = input.value;
    input.value = "";
    go.disabled = true;
    note.textContent = "";
    let out;
    try {
      const pick = share.read();
      // `clear` lets the page empty the name before it re-draws itself after a good save (the new-secret form only).
      out = await actions.save?.({ name: nm, value, days: Number(days.value), ...pick, ...(nameIn ? { clear: () => { nameIn.value = ""; } } : {}) });
    } finally {
      input.value = "";
      go.disabled = false;
    }
    if (out && !out.ok) note.textContent = SAY[out.code] ?? SAY.failed;
    if (out?.ok && nameIn) nameIn.value = "";
  } });
  return { node: h("div", { class: "secret-entry" },
    h("label", { class: "small", text: "Value" }), input,
    share.node ?? null,
    h("label", { class: "small", text: "Valid for" }), days,
    h("p", { class: "small muted", text: "Recommended: 90 days. Pick 7 days for something temporary." }), note, go), input };
}

export function secretsView(data, actions = {}) {
  const workspaces = data.workspaces ?? [];
  const wsById = Object.fromEntries(workspaces.map((w) => [w.workspace_id, w]));
  const managers = [];
  const card = (s) => {
    const names = s.shared === "all" ? "All workspaces" : (s.workspaces ?? []).map((id) => wsName(wsById[id])).join(", ") || "No workspace";
    const needs = Object.values(s.state ?? {}).some((x) => x.state !== "ready");
    // The per-workspace rows (and their Available chips) show only while a workspace still needs the value.
    const rows = needs ? Object.entries(s.state ?? {}).map(([id, x]) => h("div", { class: "list-row row" },
      h("span", { class: "grow" }, wsName(wsById[id]),
        x.state === "ready" ? null : h("div", { class: "small muted", text: "Waiting for you to type the value again for this workspace." })),
      h("span", { class: "chip " + (x.state === "ready" ? "ok" : "warn"), text: x.state === "ready" ? "\u2713 Available" : "Needs the value" }))) : null;
    // Save is hidden until the select or a box differs from what is saved; "No changes yet" stands in meanwhile.
    const save = h("button", { type: "button", text: "Save who can read it", onclick: () => { const p = share.read(); actions.share?.(s.name, p.shared, p.workspaces); } });
    save.hidden = true;
    const noChange = h("span", { class: "small muted", "data-nochange": s.name, text: "No changes yet" });
    const addedNote = h("p", { class: "small", text: "Enter the value again for the workspace you added." });
    addedNote.hidden = true;
    const share = shareBox(workspaces, s.shared, s.workspaces ?? [], false, (dirty) => {
      save.hidden = !dirty; noChange.hidden = dirty;
      const pick = share.read();
      const added = s.shared !== "all" && (pick.shared === "all" ? workspaces.map((w) => w.workspace_id) : pick.workspaces).some((id) => !(s.workspaces ?? []).includes(id));
      addedNote.hidden = !added;
      if (added) again.node.hidden = false;
    }, "secret-" + s.name);
    const again = entryForm({ name: s.name, share: { read: share.read }, submitLabel: "Save the value", actions });
    again.node.hidden = true;
    let sure = false;
    const remove = h("button", { type: "button", text: "Remove", onclick: () => {
      if (!sure) { sure = true; remove.textContent = "Sure? Remove it"; return; }
      actions.remove?.(s);
    } });
    // Per-workspace use numbers (reported by the workspaces), one small click away.
    const useLine = "Used this week: " + (Object.keys(s.state ?? {}).length
      ? Object.entries(s.state).map(([id, x]) => wsName(wsById[id]) + " " + (x.used_this_week ?? 0)).join(", ")
      : "no workspace yet");
    const useNote = h("p", { class: "small muted", "data-use": s.name, text: useLine });
    useNote.hidden = true;
    const useBtn = h("button", { type: "button", class: "link small", "data-use-toggle": s.name, text: "Who used it", onclick: () => { useNote.hidden = !useNote.hidden; } });
    const reenter = h("button", { type: "button", text: "Replace the value", onclick: () => { again.node.hidden = !again.node.hidden; } });
    const panel = h("div", { class: "manage-panel", "data-manage": s.name },
      h("p", { class: "small muted", text: "Valid until " + plainDate(s.expires_ms) }),
      h("h4", { text: "Who can read it" }), share.sel, share.list, share.warn,
      h("p", { class: "small muted", text: "A workspace that already received the value keeps it." }),
      h("div", { class: "row" }, noChange, save), rows, addedNote, reenter, again.node,
      h("div", { class: "row" }, useBtn, remove), useNote);
    panel.hidden = true;
    const summary = h("div", { class: "small muted", text: "Shared with: " + names });
    const closeManage = () => { panel.hidden = true; summary.hidden = false; again.input.value = ""; manage.setAttribute("aria-expanded", "false"); };
    const manage = h("button", { type: "button", text: "Manage", "aria-expanded": "false", onclick: () => {
      const opening = panel.hidden;
      managers.forEach((close) => close());
      closeAdd();
      panel.hidden = !opening; summary.hidden = opening;
      manage.setAttribute("aria-expanded", String(opening));
    } });
    managers.push(closeManage);
    return h("article", { class: "card secret secret-row", "data-secret": s.name },
      h("div", { class: "row" },
        h("div", { class: "grow" }, h("h3", { text: s.name }),
          h("div", { class: "small muted", text: s.expired ? "Expired" : needs ? "Needs the value" : (s.used_this_week ?? 0) ? "Used this week" : "Not used yet" }), summary),
        manage), panel);
  };
  const nameIn = h("input", { type: "text", autocomplete: "off", spellcheck: "false", "aria-label": "Name of the new secret", placeholder: "for example: crm-key", maxlength: "40" });
  const share = shareBox(workspaces, "chosen", workspaces.length === 1 ? [workspaces[0].workspace_id] : [], true);
  const form = entryForm({ nameIn, share, submitLabel: "Save secret", actions });
  const has = (data.secrets ?? []).length > 0;
  const section = h("section", { class: "card", "data-new-secret": true }, h("h3", { text: "New secret" }), nameIn, form.node, h("button", { type: "button", text: "Cancel", onclick: () => { closeAdd(); addButton.focus?.(); } }));
  const closeAdd = () => { section.hidden = true; add.hidden = false; form.input.value = ""; nameIn.value = ""; };
  const addButton = h("button", { type: "button", class: "primary pri", text: "Add a secret", onclick: () => {
    managers.forEach((close) => close()); section.hidden = false; add.hidden = true; nameIn.focus?.();
  } });
  const add = h("div", { class: "row" }, addButton);
  section.hidden = has;
  add.hidden = !has;
  const confirmation = data.confirmation ? h("span", { class: "row small", role: "status" },
    h("span", { text: data.confirmation }), h("button", { type: "button", class: "link small", text: "Dismiss", onclick: () => { confirmation.hidden = true; } })) : null;
  return h("div", { class: "secrets" },
    h("div", { class: "row" }, h("h1", { class: "grow", text: "Company secrets" }), confirmation,
      info("About company secrets", "A secret is a key your tools need, such as the sign-in key of your CRM. You type it here once. It is locked in your browser for each workspace you choose, and only those workspaces can read it. Agents never see it. " + HUB_CAN_READ)),
    attentionList(data.attention),
    has ? data.secrets.map(card) : none(),
    // With secrets on the page the form is folded behind "Add a secret"; with none it stays open (the first-run path).
    section,
    add);
}

export async function secretsPage(d, confirmation = null) {
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
    const att = await d.api("GET", "/admin/needs-attention");
    const say = (ok, x) => { d.state.notice = x.status === 200 ? d.banner("ok", ok, null) : d.banner("bad", "Not changed", d.say(x.body.error ?? "unavailable")); };
    const save = async (f) => {
      const out = await saveSecret({ api: d.api, ...f });
      if (out.ok) { f.clear?.(); await secretsPage(d, out.without_key.length ? "Saved. Some workspaces have no key yet and need the value again later." : "Saved"); }
      return out;
    };
    const remove = async (s) => { say("Removed", await d.api("DELETE", "/admin/secrets/" + s.name)); await secretsPage(d); };
    const share = async (name, shared, workspaces) => { const result = await d.api("PUT", "/admin/secrets/" + name + "/share", { shared, workspaces }); if (result.status !== 200) say("Saved", result); await secretsPage(d, result.status === 200 ? "Saved" : null); };
    d.shell("secrets", d.state.notice, secretsView({ ...r.body, confirmation, attention: att.status === 200 ? att.body.items : [], workspaces: withNames(r.body.workspaces, d.state.rail?.rows ?? []) }, { save, remove, share }));
    d.state.notice = null;
  } finally { finish(); }
}

// The registry entry: the console's router, tabs and file list are built from this one export (no other file names a page).
export const page = { path: "secrets", nav: { group: "Sharing & apps", label: "Secrets", admin: true, needsWorkspace: true, after: "mcp" }, render: secretsPage };
