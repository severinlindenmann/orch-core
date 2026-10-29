// The Hub settings page (M4, #1460 #1461): what the hub is called, the addresses it advertises, how people sign in.
// General and Address are editable (one Save per card, plain clicks, no typed confirm word). The page signs nothing: the
// hub checks every value again, writes it to its settings file and records the change in its audit log.
import { loading } from "../feel.js";
import { chip, h, text, when } from "../ui.js";

// The words for an address kind (owner rule: never a product name for the private network).
const KINDS = [
  ["private", "Private network (VPN)"],
  ["server", "On this server"],
  ["office", "Office network"],
  ["public", "Public address"],
];
const KIND_WORD = new Map(KINDS);

// Hub error codes this page can say better than the console's generic table.
const WHY = {
  malformed: "Check the values: a name is 1 to 60 characters, and an address must be one of this server's own.",
  phone_address_locked: "The first address is the one phones pair with. It cannot be changed here.",
  unavailable: "This hub cannot do that now.",
  bad_credentials: "The password or the code was not right. Each code works only once: wait for the next one.",
  insecure_origin: "The 24 words are not shown on this address. Run the command below on the server itself.",
  no_backup_key: "Set up backups first.",
  backup_not_found: "That backup is not on this server any more.",
  backup_failed: "The backup could not be made.",
  wrong_backup_key: "Those words do not open this backup.",
  words: "The 24 words are not right. Check each word.",
  backup_invalid: "This file is not a backup of this hub, or it was changed.",
  other_hub: "This backup belongs to another hub.",
  dev_mismatch: "This backup is of a different kind of hub (test or real).",
  dev_refused: "This backup is of a different kind of hub (test or real).",
  restore_busy: "Another big action is running on this hub (a restore, an update or a removal). Try again when it is done.",
  no_admin: "After this restore nobody could sign in as an admin, so it was not done.",
  restore_failed: "The restore could not be done. Nothing was changed.",
};

const row = (label, note, on) => h("div", { class: "list-row row" },
  h("div", { class: "grow" }, h("b", { text: text(label) }), note ? h("div", { class: "small muted", text: text(note) }) : null),
  on ? chip("On", "ok") : chip("Later"));

const WEB = new RegExp("^https?:" + "//(\\[[^\\]]+\\]|[^:/]+):\\d+$"); // built, so the console has no literal address
/** The host of an advertised web address (an IPv6 host loses its brackets). */
export const hostOf = (web) => {
  const m = WEB.exec(String(web));
  return m ? m[1].replace(/^\[|\]$/g, "") : "";
};
const schemeOf = (web) => (String(web).startsWith("https:") ? "https" : "http");

/**
 * The body of "change the addresses": the phone address as it is, then one entry per kind that has a choice.
 * `picks` maps a kind to the chosen host ("" = not used);
 */
export function addressBody(data, picks) {
  const phone = (data.addresses ?? [])[0];
  const out = [];
  if (phone) out.push({ kind: phone.kind, host: hostOf(phone.web), ...(phone.kind === "public" && schemeOf(phone.web) === "https" ? { scheme: "https" } : {}) });
  for (const [kind] of KINDS) {
    if (phone && kind === phone.kind) continue;
    const host = String(picks[kind] ?? "").trim();
    if (!host) continue;
    if (kind !== "public") out.push({ kind, host });
  }
  return { addresses: out };
}

function generalCard(data, actions) {
  const name = h("input", { type: "text", id: "hub-name", maxlength: "60", autocomplete: "off" });
  const company = h("input", { type: "text", id: "hub-company", maxlength: "60", autocomplete: "off" });
  name.value = data.name ?? ""; company.value = data.company ?? "";
  const save = h("button", { type: "button", class: "primary", text: "Save", onclick: () => actions.saveGeneral?.({ name: name.value ?? "", company: company.value ?? "" }) });
  return h("div", { class: "card", "data-card": "general" }, h("h2", { text: "General" }),
    h("div", { class: "field" }, h("label", { for: "hub-name", text: "Hub name" }), name),
    h("div", { class: "field" }, h("label", { for: "hub-company", text: "Company" }), company),
    h("p", { class: "small muted", text: "People see the name in the switcher. Connected workspaces show it too, in the hub's own words." }),
    save);
}

function addressCard(data, actions) {
  const byKind = new Map((data.addresses ?? []).map((a) => [a.kind, a.web]));
  const facts = KINDS.map(([kind, label]) => {
    const web = byKind.get(kind);
    return [h("dt", { text: label }), h("dd", {}, web ? h("span", { class: "mono", text: text(web) }) : h("span", { class: "muted", text: "Not set up" }),
      kind === "server" && web ? h("span", { class: "small muted", text: " (on the server itself, use this one)" }) : null,
      kind === data.phone_kind && web ? h("span", { class: "small muted", text: " (phones use this one)" }) : null)];
  }).flat();
  const card = h("div", { class: "card", "data-card": "address" }, h("h2", { text: "Address" }), h("dl", { class: "facts" }, facts));
  if (!data.can_change_addresses) {
    card.append(h("p", { class: "small muted", text: "Addresses can't be changed on this page. To add or change one, ask the person who installed this hub." }));
    return card;
  }
  const detected = actions.detected ?? [];
  const here = globalThis.location?.origin ?? "";
  const picks = {}; const fields = [];
  for (const [kind, label] of KINDS) {
    if (kind === data.phone_kind) { fields.push(h("div", { class: "field", "data-kind": kind }, h("label", { text: label }), h("span", { class: "small muted", text: "Phones use this one. It stays as it is." }))); continue; }
    const cur = byKind.get(kind);
    if (kind === "public") continue; // the public address is set up with the edge, not here; an installed one stays as it is
    const choices = detected.filter((d) => d.kind === kind);
    const select = h("select", { id: "addr-" + kind, "aria-label": label }, h("option", { value: "", text: "Not used" }),
      choices.map((d) => h("option", { value: d.ip, text: d.ip })));
    select.value = cur ? hostOf(cur) : ""; picks[kind] = select;
    const using = cur && here && cur === here;
    if (using) select.disabled = true;
    fields.push(h("div", { class: "field", "data-kind": kind }, h("label", { for: "addr-" + kind, text: label }), select,
      using ? h("span", { class: "small muted", text: "You are using this one now." }) : null));
  }
  const opened = detected.length > 0; // the choices arrived: the form was opened by "Change address"
  const form = h("div", { class: "address-form", hidden: !opened }, fields,
    h("p", { class: "small muted", text: "Workspaces pick up the new address the next time they connect. Nothing signed changes, so no connection breaks. The hub restarts for a moment." }),
    h("button", { type: "button", class: "primary", text: "Save", onclick: () => {
      const chosen = {}; for (const [k, el] of Object.entries(picks)) if (k !== "secure") chosen[k] = el.value ?? "";
      actions.saveAddresses?.(addressBody(data, chosen));
    } }));
  const open = h("button", { type: "button", text: "Change address", hidden: opened, onclick: () => { form.hidden = false; open.hidden = true; actions.loadDetected?.(); } });
  card.append(open, form);
  return card;
}


// --- Backup (M4 S5, #1462): the backup key (24 words, shown once), a backup now, the kept files, a download -----------------

const STEP_UP = "Type your password and a new 6-digit code from your authenticator app. Each code works only once.";
const NO_WORDS_HERE = "For safety the 24 words are not shown on this address, because it is not private. Run this on the server itself:";
const NAMES = new RegExp("^hub-[0-9a-f]{8}-\\d{8}T\\d{6}Z\\.orchbak$");

/** Is the console opened where the 24 words may be shown: this computer, https, or this hub's own private-network (VPN)
 *  address (`privateWebs` = the web addresses of kind private from the settings; not the whole 100.64/10 range)? */
export function wordsMayShow(loc = globalThis.location, privateWebs = []) {
  if (!loc) return false;
  if (loc.protocol === "https:") return true;
  if (loc.protocol !== "http:") return false;
  const host = String(loc.hostname ?? "").replace(/^\[|\]$/g, "");
  if (host === "localhost" || host === "::1" || /^127\.\d+\.\d+\.\d+$/.test(host)) return true;
  const origin = loc.origin ?? (loc.protocol + "//" + (loc.host ?? ""));
  return privateWebs.includes(origin);
}

/** Password + code, then one button. `onSubmit({password, totp})` resolves nothing; the page re-renders afterwards. */
function stepUpForm(id, label, onSubmit, withWords = false) {
  const wordsBox = withWords ? h("textarea", { id: id + "-words", rows: "3", autocomplete: "off", spellcheck: "false", "aria-label": "The 24 backup words" }) : null;
  const pw = h("input", { id: id + "-pw", type: "password", autocomplete: "current-password", "aria-label": "Password" });
  const code = h("input", { id: id + "-totp", inputmode: "numeric", autocomplete: "one-time-code", maxlength: "6", "aria-label": "6-digit code" });
  const go = h("button", { type: "button", class: "primary", text: label, onclick: async () => {
    const body = { password: pw.value ?? "", totp: String(code.value ?? "").trim() };
    if (wordsBox) { body.words = String(wordsBox.value ?? ""); wordsBox.value = ""; }
    pw.value = ""; code.value = "";
    await onSubmit(body);
  } });
  return h("div", { class: "stepup", "data-stepup": id },
    h("p", { class: "small muted", text: STEP_UP }),
    wordsBox ? h("div", { class: "field" }, h("label", { for: id + "-words", text: "The 24 backup words" }), wordsBox) : null,
    h("div", { class: "field" }, h("label", { for: id + "-pw", text: "Password" }), pw),
    h("div", { class: "field" }, h("label", { for: id + "-totp", text: "6-digit code" }), code), go);
}

/** The 24 words as a numbered grid. Shown once; "I have saved it" clears them. */
function wordsPanel(words, actions) {
  const list = String(words).split(" ");
  const grid = h("ol", { class: "words mono" }, list.map((w) => h("li", { text: w })));
  const copy = h("button", { type: "button", text: "Copy", onclick: async () => {
    let ok = false;
    try { ok = !!(await actions.copy?.(list.join(" "))); } catch { ok = false; }
    copy.textContent = ok ? "Copied" : "Select and copy";
  } });
  return h("div", { class: "banner ok", role: "status", "data-words": "1" },
    h("b", { text: "Write these 24 words down" }),
    h("p", { class: "small", text: "They are shown only now. Keep them somewhere safe, away from this server. Without them a backup cannot be opened, and nobody can get them back for you." }),
    grid, h("div", { class: "row" }, copy, h("button", { type: "button", class: "primary", text: "I have saved it", onclick: () => actions.wordsSaved?.() })));
}

function backupRows(b, actions) {
  if (!b.backups.length) return h("p", { class: "small muted", text: "No backup yet." });
  return h("ul", { class: "list backups" }, b.backups.filter((x) => NAMES.test(String(x.name))).map((x) => {
    const row = h("li", { class: "list-row row", "data-backup": x.name },
      h("div", { class: "grow" }, h("b", { text: text(when(x.made_ms)) }), h("div", { class: "small muted", text: Math.max(1, Math.round(x.size / 1024)) + " KB" })));
    const slot = h("div", { class: "stepup-slot" });
    row.append(h("button", { type: "button", text: "Download", onclick: () => slot.replaceChildren(stepUpForm("dl-" + x.name.slice(4, 12), "Download", (c) => actions.download?.(x.name, c))) }));
    if (actions.restore) row.append(h("button", { type: "button", text: "Restore", onclick: () => slot.replaceChildren(restorePanel(x, actions)) }));
    return h("div", {}, row, slot);
  }));
}

const RESTORE_WORDS = "This puts the hub back to how it was when this backup was made. Everything changed since then is lost. People who are not admins are switched off until you turn each one on again. Every phone and browser has to sign in or pair again. Workspaces keep working on their own.";
const NO_RESTORE_HERE = "For safety the 24 words are not typed on this address, because it is not private. Run this on the server itself:";

/** The restore step for one kept backup: the words, the password and a code, one red button. Plain words, no typed confirmation. */
function restorePanel(x, actions) {
  if (actions.wordsMayShow === false) return h("div", { class: "restore" }, h("p", { class: "small muted", text: NO_RESTORE_HERE }), h("code", { class: "mono cli-line", text: "orch-hub restore FILE" }));
  return h("div", { class: "restore", "data-restore": x.name },
    h("p", { class: "small", text: "Restore the backup of " + text(when(x.made_ms)) + "?" }),
    h("p", { class: "small muted", text: RESTORE_WORDS }),
    stepUpForm("rs-" + x.name.slice(4, 12), "Restore this backup", (c) => actions.restore?.(x.name, c), true));
}

/** The notice after a restore: what happened, what to check. */
function restoredNotice(n, actions) {
  return h("div", { class: "banner warn", role: "status", "data-restored": "1" },
    h("b", { text: "Restored from the backup of " + text(when(n.made_ms)) }),
    h("p", { class: "small", text: "Changes made after it are gone. Check People and Workspaces. " + (n.suspended ? text(n.suspended) + (n.suspended === 1 ? " person is" : " people are") + " switched off: turn each one on again in People." : "") }),
    h("div", { class: "row" }, h("button", { type: "button", text: "Got it", onclick: () => actions.dismissRestored?.() })));
}

const PENDING_WORD = { secret_share: "Company secret shared with a workspace", secret_all: "Company secret shared with all workspaces", skill_offer: "Company skill offered to a workspace", mcp_offer: "Company server offered to a workspace" };

/** What a restore on a new server switched off: each one is Off until an admin turns it on again with one click. */
function pendingList(items, actions) {
  return h("div", { class: "banner warn", "data-pending": "1" },
    h("b", { text: "Switched off by the restore" }),
    h("p", { class: "small", text: "These were shared before the backup was made. They stay off until you turn each one on again." }),
    h("ul", { class: "list" }, items.map((x) => h("li", { class: "list-row row", "data-pending-id": String(x.id) },
      h("div", { class: "grow" }, h("b", { text: text(x.label) }), h("div", { class: "small muted", text: text(PENDING_WORD[x.kind] ?? "Shared item") })),
      h("button", { type: "button", text: "Turn on", onclick: () => actions.confirmPending?.(x.id) })))));
}

function backupCard(b, actions) {
  const card = h("div", { class: "card", "data-card": "backup" }, h("h2", { text: "Backup" }));
  if (actions.words) card.append(wordsPanel(actions.words, actions));
  if (b.restored) card.append(restoredNotice(b.restored, actions));
  if (b.pending?.length) card.append(pendingList(b.pending, actions));
  const mayShow = actions.wordsMayShow !== false;
  const cli = h("code", { class: "mono cli-line", text: text(b.cli_line ?? "orch-hub backup-key") });
  if (!b.key_set) {
    card.append(h("p", { class: "muted", text: "A backup keeps your people, keys and settings safe. It is sealed with a key that only the 24 backup words open. Every workspace keeps working without the hub, with or without a backup." }));
    if (mayShow) card.append(h("div", { class: "setup" }, stepUpForm("bk-key", "Set up backups", (c) => actions.setKey?.(c))));
    else card.append(h("p", { class: "small muted", text: NO_WORDS_HERE }), cli);
    return card;
  }
  card.append(
    h("p", { class: "small muted", text: "A backup is made every day. The " + text(b.keep) + " newest are kept on this server. The files are sealed: only the 24 words open them." }),
    backupRows(b, actions),
    h("div", { class: "row" }, h("button", { type: "button", class: "primary", text: "Back up now", onclick: () => actions.makeBackup?.() })),
    h("p", { class: "small muted", text: "To keep a copy in another place, download a file and store it away from this server." }));
  const more = h("div", { class: "replace" });
  if (mayShow) {
    const open = h("button", { type: "button", text: "Replace the backup key", onclick: () => {
      more.replaceChildren(h("p", { class: "small muted", text: "New words make a new backup. Backups made with the old words are deleted once you have saved the new words." }), stepUpForm("bk-new", "Make new words", (c) => actions.setKey?.(c)));
    } });
    more.append(open);
  } else more.append(h("p", { class: "small muted", text: NO_WORDS_HERE }), h("code", { class: "mono cli-line", text: text(b.cli_line ?? "orch-hub backup-key") + " --replace" }));
  card.append(more);
  return card;
}

export function hubSettingsView(data, actions = {}) {
  if (!data.configured) {
    return h("div", { class: "hub-settings" }, h("h1", { text: "Hub settings" }),
      h("p", { class: "small muted", role: "status", text: data.problem ? text(data.problem) : "This hub was started by hand, so it has no settings file. Install it with orch install hub to see its settings here." }));
  }
  return h("div", { class: "hub-settings" },
    h("h1", { text: "Hub settings" }),
    h("p", { class: "lead muted", text: "How this hub is reached and how people sign in." }),
    generalCard({ ...data, name: text(data.name || "Hub"), company: text(data.company || "") }, actions),
    addressCard(data, actions),
    data.backup ? backupCard(data.backup, actions) : null,
    h("div", { class: "card", "data-card": "sign-in" }, h("h2", { text: "Sign-in" }),
      (data.sign_in ?? []).map((m) => row(m.label, m.on ? m.note : null, m.on))));
}

// Wait until the restarted hub answers again (it keeps its port), at most about 30 seconds.
async function comesBack(d) {
  const { tries = 30, pause = 1000 } = d.restartPoll ?? {};
  for (let i = 0; i < tries; i++) {
    await new Promise((r) => setTimeout(r, pause));
    try { const r = await d.api("GET", "/hub/card"); if (r.status === 200) return true; } catch { /* still restarting */ }
  }
  return false;
}

export async function hubSettingsPage(d, detected = [], words = null) {
  const finish = loading();
  d.pageLoading("hub-settings");
  try {
    if (!(await d.refresh())) return d.loginView();
    if (!d.state.user.is_admin) return d.home();
    if (d.state.user.needs_sign_in) return d.reauthView();
    await d.loadRail();
    const r = await d.api("GET", "/admin/settings");
    if (r.status === 403 && r.body.error === "reauth_required") return d.reauthView();
    if (r.status !== 200) return d.shell("hub-settings", h("h1", { text: "Hub settings" }), h("p", { role: "alert", text: "Cannot read the settings. " + d.say(r.body.error) }));
    const bk = await d.api("GET", "/admin/backup");
    if (bk.status === 200) r.body.backup = bk.body;
    const say = (ok, x) => { d.state.notice = x.status < 300 ? d.banner("ok", ok, null) : d.banner("bad", "Not changed", WHY[x.body.error] ?? d.say(x.body.error ?? "unavailable")); };
    const backupNote = (x, ok) => { d.state.notice = x.status < 300 ? d.banner("ok", ok, null) : d.banner("bad", "Not done", WHY[x.body.error] ?? d.say(x.body.error ?? "unavailable")); };
    const actions = {
      detected,
      words,
      wordsMayShow: wordsMayShow(globalThis.location, (r.body.addresses ?? []).filter((a) => a.kind === "private").map((a) => a.web)),
      copy: async (t) => { try { await globalThis.navigator?.clipboard?.writeText(t); return true; } catch { return false; } },
      wordsSaved: async () => { await d.api("POST", "/admin/backup/key/confirm", {}); await hubSettingsPage(d); },
      setKey: async (c) => {
        const x = await d.api("POST", "/admin/backup/key", c);
        if (x.status === 200) { d.state.notice = d.banner("ok", "Backups are set up", null); return hubSettingsPage(d, [], x.body.words); }
        backupNote(x, ""); return hubSettingsPage(d);
      },
      restore: async (name, c) => {
        const x = await d.api("POST", "/admin/backup/restore", { name, ...c });
        if (x.status === 202) { d.state.notice = d.banner("ok", "Restored. The hub is restarting: sign in again.", null); await comesBack(d); return d.refresh().then(() => hubSettingsPage(d)); }
        backupNote(x, "Restored"); await hubSettingsPage(d);
      },
      confirmPending: async (id) => { backupNote(await d.api("POST", "/admin/backup/restore/pending/confirm", { id }), "Turned on"); await hubSettingsPage(d); },
      dismissRestored: async () => { await d.api("POST", "/admin/backup/restored/dismiss", {}); await hubSettingsPage(d); },
      makeBackup: async () => { backupNote(await d.api("POST", "/admin/backups", {}), "Backup made"); await hubSettingsPage(d); },
      download: async (name, c) => {
        const x = await d.api("POST", "/admin/backups/download", { name, ...c });
        if (x.status !== 200) { backupNote(x, ""); return hubSettingsPage(d); }
        try {
          const bin = atob(String(x.body.data).replace(/-/g, "+").replace(/_/g, "/"));
          const bytes = Uint8Array.from(bin, (ch) => ch.charCodeAt(0));
          const a = h("a", { href: URL.createObjectURL(new Blob([bytes], { type: "application/octet-stream" })), download: x.body.name });
          a.click?.();
          backupNote({ status: 200 }, "Backup downloaded");
        } catch { backupNote({ status: 500, body: { error: "unavailable" } }, ""); }
        await hubSettingsPage(d);
      },
      saveGeneral: async (body) => { say("Saved", await d.api("PUT", "/admin/settings/general", body)); await hubSettingsPage(d); },
      loadDetected: async () => { const x = await d.api("GET", "/admin/settings/addresses/detected"); if (x.status === 200) await hubSettingsPage(d, x.body.addresses ?? []); },
      saveAddresses: async (body) => {
        const x = await d.api("PUT", "/admin/settings/addresses", body);
        if (x.status === 202) { d.state.notice = d.banner("ok", "Saved. The hub is restarting.", null); await comesBack(d); say("Saved", { status: 200 }); }
        else say("Saved", x);
        await hubSettingsPage(d);
      },
    };
    d.shell("hub-settings", d.state.notice, hubSettingsView(r.body, actions));
    d.state.notice = null;
  } finally { finish(); }
}

// The registry entry: the page's file name is its path (settings is a route of orch's own, so the file is hub-settings).
export const page = { path: "hub-settings", nav: { label: "Hub settings", admin: true, after: "admin" }, render: hubSettingsPage };
