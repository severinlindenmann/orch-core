// The Hub settings page (M4, #1460 #1461): what the hub is called, the addresses it advertises, how people sign in.
// General and Address are editable (one Save per card, plain clicks, no typed confirm word). The page signs nothing: the
// hub checks every value again, writes it to its settings file and records the change in its audit log.
import { loading } from "../feel.js";
import { hubRemoveDigest, signProof } from "../channel.js";
import { hex } from "../cj.js";
import { chip, h, text, when } from "../ui.js";
import { WHY as COMPANY_WHY, begin as beginCompany, companyCard } from "../oidc.js";

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
  confirm_denied: "Only the session that made the new words can confirm them. Make new words again from this session.",
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
  rate_limited: "A backup was made a moment ago. Try again in a few minutes.",
  restore_failed: "The restore could not be done. Nothing was changed.",
};

void COMPANY_WHY;
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
  const name = h("input", { type: "text", id: "hub-name", maxlength: "60", autocomplete: "off", placeholder: "for example Acme headquarters" });
  const company = h("input", { type: "text", id: "hub-company", maxlength: "60", autocomplete: "off", placeholder: "Your company name, for example Acme Ltd" });
  name.value = data.name ?? ""; company.value = data.company ?? "";
  const save = h("button", { type: "button", class: "primary", text: "Save", onclick: () => actions.saveGeneral?.({ name: name.value ?? "", company: company.value ?? "" }) });
  return h("div", { class: "card", "data-card": "general" }, h("h2", { text: "General" }),
    h("div", { class: "field" }, h("label", { for: "hub-name", text: "Hub name" }), name),
    h("div", { class: "field" }, h("label", { for: "hub-company" }, "Company ", h("span", { class: "optional", text: "(optional)" })), company),
    h("p", { class: "small muted", text: "People see the name in the switcher. Connected workspaces show it too, in the hub's own words." }),
    save);
}

function addressCard(data, actions) {
  const byKind = new Map((data.addresses ?? []).map((a) => [a.kind, a.web]));
  const facts = KINDS.map(([kind, label]) => {
    const web = byKind.get(kind);
    return [h("dt", { text: label }), h("dd", {}, web ? h("span", { class: "mono", text: text(web) }) : h("span", { class: "muted", text: "Not set up" }),
      kind === "server" && web ? h("span", { class: "small muted", text: " (this computer only)" }) : null,
      kind === data.phone_kind && kind !== "server" && web ? h("span", { class: "small muted", text: " (phones use this one)" }) : null)];
  }).flat();
  const card = h("div", { class: "card", "data-card": "address" }, h("h2", { text: "Address" }), h("dl", { class: "facts" }, facts));
  const server = data.phone_kind === "server";
  if (server) card.append(h("p", { class: "small muted", id: "phones-cannot", text: "Phones can't reach this hub yet." }));
  if (!data.can_change_addresses) {
    card.append(h("p", { class: "small muted", text: "This hub was installed without address changes." }),
      h("details", { class: "small muted" }, h("summary", { text: "Details" }),
        h("p", { class: "small muted", text: "To add or change an address, ask the person who installed this hub." })));
    return card;
  }
  const detected = actions.detected ?? [];
  const here = globalThis.location?.origin ?? "";
  const picks = {}; const fixed = {}; const fields = [];
  for (const [kind, label] of KINDS) {
    if (kind === data.phone_kind && kind !== "server") { fields.push(h("div", { class: "field", "data-kind": kind }, h("label", { text: label }), h("span", { class: "small muted", text: "Phones use this one. It stays as it is." }))); continue; }
    const cur = byKind.get(kind);
    if (kind === "public") continue; // the public address is set up with the edge, not here; an installed one stays as it is
    const choices = detected.filter((d) => d.kind === kind);
    const curHost = cur ? hostOf(cur) : "";
    // the current host always has its own option: a real <select> turns a value that is not among its options into "" and Save would drop the address
    const select = h("select", { id: "addr-" + kind, "aria-label": label }, h("option", { value: "", text: "Not used" }),
      curHost && !choices.some((d) => d.ip === curHost) ? h("option", { value: curHost, text: curHost }) : null,
      choices.map((d) => h("option", { value: d.ip, text: d.iface ? d.ip + " (" + text(d.iface) + ")" : d.ip })));
    select.value = curHost; picks[kind] = select;
    if (!cur && server && kind === "office" && choices.length > 0) select.value = choices[0].ip; // "Make phones reach this hub": the office network is preselected; Save is still the admin's click
    const using = cur && here && cur === here;
    const exposes = kind === "office" ? h("span", { class: "small muted", "data-exposes": "office", hidden: !select.value || using, text: "Anyone on this office network can open the hub's sign-in page." }) : null;
    if (exposes) select.addEventListener("change", () => { exposes.hidden = !select.value; });
    if (using) { select.disabled = true; fixed[kind] = curHost; }
    fields.push(h("div", { class: "field", "data-kind": kind }, h("label", { for: "addr-" + kind, text: label }), select, exposes,
      using ? h("span", { class: "small muted", text: "You are using this one now." }) : null));
  }
  const opened = actions.opened === true; // set by the button: the form is open only after the admin asked for it
  const form = h("div", { class: "address-form", hidden: !opened }, fields,
    h("p", { class: "small muted", text: "Workspaces pick up the new address the next time they connect. Nothing signed changes, so no connection breaks. The hub restarts for a moment." }),
    h("button", { type: "button", class: "primary", text: "Save", onclick: () => {
      const chosen = {}; for (const [k, el] of Object.entries(picks)) if (k !== "secure") chosen[k] = fixed[k] ?? el.value ?? "";
      actions.saveAddresses?.(addressBody(data, chosen));
    } }));
  const open = h("button", { type: "button", class: server ? "primary" : "", text: server ? "Make phones reach this hub" : "Change address", hidden: opened, onclick: () => { form.hidden = false; open.hidden = true; note.hidden = true; actions.loadDetected?.(); } });
  const note = h("span", { class: "small muted", hidden: opened || !server, text: " Uses your office network. Workspaces reconnect by themselves." });
  card.append(h("div", { class: "row" }, open, note), form);
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

const MAX_WORDS_FILE = 4096;  // the 24 words are well under 300 bytes; anything bigger is not the words file

/** The 24 words from text that came from a file or the clipboard: exactly 24 words, joined by single spaces, else null.
 *  Only a shape check for a plain message; the hub still opens the backup with the words or refuses. */
function wordsFrom(raw) {
  const list = String(raw ?? "").trim().split(/\s+/).filter(Boolean);
  return list.length === 24 && list.every((w) => /^\S{1,32}$/.test(w)) ? list.join(" ") : null;
}

/** The words input of the restore form: Choose the file / Paste (read in this page, never stored, never sent anywhere but
 *  the restore request), with the old text box behind "Type them instead". `get()` returns the words and forgets them. */
function wordsInput(id) {
  let fromFile = "";
  const status = h("span", { class: "small muted", role: "status" });
  const area = h("textarea", { id: id + "-words", rows: "3", autocomplete: "off", spellcheck: "false", hidden: true, placeholder: "Type the 24 words, separated by spaces", "aria-label": "The 24 backup words" });
  const take = (raw, label) => {
    const w = wordsFrom(raw);
    fromFile = w ?? "";
    status.textContent = w ? label + " ✓" : "That is not the 24 words. Choose the words file again, or type them.";
  };
  const file = h("input", { type: "file", accept: ".txt,text/plain", hidden: true, "aria-label": "The words file", onchange: async (e) => {
    const f = e?.target?.files?.[0];
    if (!f || !(f.size <= MAX_WORDS_FILE)) return take("", "");
    let t = "";
    try { t = await f.text(); } catch { t = ""; }
    take(t, "Words file chosen: " + text(f.name ?? "file"));
  } });
  const choose = h("button", { type: "button", text: "Choose the file", onclick: () => file.click?.() });
  const paste = h("button", { type: "button", text: "Paste", onclick: async () => {
    let t = "";
    try { t = await globalThis.navigator?.clipboard?.readText(); } catch { t = ""; }
    take(t, "Words pasted");
  } });
  const typed = h("button", { type: "button", class: "link", "data-words-details": "1", title: "Type the words instead", text: "Details", onclick: () => { area.removeAttribute("hidden"); typed.setAttribute("hidden", ""); } });
  const node = h("div", { class: "field", "data-words-input": "1" },
    h("label", { text: "The 24 backup words" }),
    h("div", { class: "row" }, choose, paste, status), file, area, typed);
  return { node, get: () => {
    const typedWords = String(area.value ?? "").trim();
    const w = typedWords || fromFile;
    area.value = ""; file.value = ""; fromFile = ""; status.textContent = "";
    return w;
  } };
}

/** Password + code, then one button. `onSubmit({password, totp})` resolves nothing; the page re-renders afterwards. */
function stepUpForm(id, label, onSubmit, withWords = false, onCancel = null, cls = "primary") {
  const words = withWords ? wordsInput(id) : null;
  const pw = h("input", { id: id + "-pw", type: "password", autocomplete: "current-password", "aria-label": "Password" });
  const code = h("input", { id: id + "-totp", inputmode: "numeric", autocomplete: "one-time-code", maxlength: "6", "aria-label": "6-digit code" });
  const go = h("button", { type: "button", class: cls, text: label, onclick: async () => {
    const body = { password: pw.value ?? "", totp: String(code.value ?? "").trim() };
    if (words) body.words = words.get();
    pw.value = ""; code.value = "";
    await onSubmit(body);
  } });
  return h("div", { class: "stepup", "data-stepup": id },
    h("p", { class: "small muted", text: STEP_UP }),
    words ? words.node : null,
    h("div", { class: "field" }, h("label", { for: id + "-pw", text: "Password" }), pw),
    h("div", { class: "field" }, h("label", { for: id + "-totp", text: "6-digit code" }), code),
    onCancel ? h("div", { class: "row" }, go, h("button", { type: "button", text: "Cancel", onclick: onCancel })) : go);
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
  const save = h("button", { type: "button", text: "Save as a file", onclick: () => {
    try {
      const url = URL.createObjectURL(new Blob([list.join(" ") + "\n"], { type: "text/plain" }));
      h("a", { href: url, download: "hub-backup-words.txt" }).click?.();
      setTimeout(() => URL.revokeObjectURL?.(url), 0);
      save.textContent = "Saved";
    } catch { save.textContent = "Could not save"; }
  } });
  const host = String(globalThis.location?.hostname ?? "").replace(/^\[|\]$/g, "");
  const onServer = host === "localhost" || host === "::1" || /^127\.\d+\.\d+\.\d+$/.test(host);   // the browser runs on the hub machine: a file would land on the server
  return h("div", { class: "banner ok", role: "status", "data-words": "1" },
    h("b", { text: "Write these 24 words down" }),
    h("p", { class: "small", text: "They are shown only now. Keep them somewhere safe, away from this server. Without them a backup cannot be opened, and nobody can get them back for you." }),
    onServer ? null : h("p", { class: "small", text: "Keep this file somewhere safe and offline. Anyone with it and your password can restore your hub." }),
    grid, h("div", { class: "row" }, copy, onServer ? null : save, h("button", { type: "button", class: "primary", text: "I have saved it", onclick: () => actions.wordsSaved?.() })));
}

function backupRows(b, actions) {
  if (!b.backups.length) return h("p", { class: "small muted", text: "No backup yet." });
  return h("ul", { class: "list backups" }, b.backups.filter((x) => NAMES.test(String(x.name))).map((x) => {
    const row = h("li", { class: "list-row row", "data-backup": x.name },
      h("div", { class: "grow" }, h("b", { text: text(when(x.made_ms)) }), h("div", { class: "small muted", text: Math.max(1, Math.round(x.size / 1024)) + " KB" }),
        b.note?.downloaded === x.name ? h("div", { class: "small", "data-downloaded": "1", text: "Downloaded" }) : null));
    const slot = h("div", { class: "stepup-slot" });
    row.append(h("button", { type: "button", text: "Download", onclick: () => slot.replaceChildren(h("div", { class: "dl-form" },
      h("b", { text: "Download the backup of " + text(when(x.made_ms)) }),
      stepUpForm("dl-" + x.name.slice(4, 12), "Download this backup", (c) => actions.download?.(x.name, c), false, () => slot.replaceChildren()))) }));
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
export function restoredNotice(n, actions) {
  return h("div", { class: "banner warn", role: "status", "data-restored": "1" },
    h("b", { text: "Restored from the backup of " + text(when(n.made_ms)) }),
    h("p", { class: "small", text: "Changes made after it are gone. Check that your workspaces show Online. " + (n.suspended ? text(n.suspended) + (n.suspended === 1 ? " person is" : " people are") + " switched off: turn them on again in People." : "") }),
    h("div", { class: "row" }, h("button", { type: "button", text: "Got it", onclick: () => actions.dismissRestored?.() })));
}

const PENDING_WORD = { secret_share: "Company secret shared with a workspace", secret_all: "Company secret shared with all workspaces", skill_all: "Company skill offered to all workspaces", skill_offer: "Company skill offered to a workspace", mcp_all: "Company server offered to all workspaces", mcp_offer: "Company server offered to a workspace", market_company: "Company add-on markets", addon_request: "An add-on request" };

/** What a restore on a new server switched off: each one is Off until an admin turns it on again with one click. */
function pendingList(items, actions) {
  return h("div", { class: "banner warn", "data-restore-pending": "1" },
    h("b", { text: "Switched off by the restore" }),
    h("p", { class: "small", text: "These were shared before the backup was made. They stay off until you turn each one on again." }),
    h("ul", { class: "list" }, items.map((x) => h("li", { class: "list-row row", "data-pending-id": String(x.id) },
      h("div", { class: "grow" }, h("b", { text: text(x.label) }), h("div", { class: "small muted", text: text(PENDING_WORD[x.kind] ?? "Shared item") })),
      h("button", { type: "button", text: "Turn on", onclick: () => actions.confirmPending?.(x.id) })))));
}

// --- P07 (QA run 12): the backup card shows the result of its own buttons, right under them ---------------------------------
/** The wait before another "Back up now" (the hub allows one in 10 minutes), in plain words; null when it may be made. */
function waitWord(b, now = Date.now()) {
  const left = Number(b.next_manual_ms ?? 0) - now;
  if (!(left > 0)) return null;
  const min = Math.max(1, Math.ceil(left / 60000));
  return "Backup made just now. The next one can be made in " + min + (min === 1 ? " minute." : " minutes.");
}
function backupNow(b, actions) {
  const wait = waitWord(b);
  return h("div", { class: "row backup-now" },
    h("button", { type: "button", class: "primary", text: "Back up now", disabled: wait ? "disabled" : null, onclick: () => actions.makeBackup?.() }),
    wait ? h("span", { class: "small muted", "data-backup-wait": "1", text: wait }) : null);
}
/** The message slot: what the last backup action did. Always in the card, never at the top of the page. */
function backupNoteView(n) {
  if (!n?.title) return null;
  return h("div", { class: "banner backup-note " + (n.kind === "bad" ? "bad" : "ok"), role: n.kind === "bad" ? "alert" : "status", "data-backup-note": n.kind },
    h("b", { text: text(n.title) }), n.detail ? h("div", { class: "small", text: text(n.detail) }) : null);
}

function backupCard(b, actions) {
  const card = h("div", { class: "card", "data-card": "backup" }, h("h2", { text: "Backup" }));
  if (actions.words) card.append(wordsPanel(actions.words, actions));
  if (b.restored) card.append(restoredNotice(b.restored, actions));
  if (b.pending?.length) card.append(pendingList(b.pending, actions));
  if (b.key_pending) {  // after a reload: the words are gone, but the older backups are still waiting for "saved"
    card.append(h("div", { class: "banner", role: "status", "data-pending": "1" },
      h("b", { text: "New backup words are not confirmed yet" }),
      h("p", { class: "small", text: "Backups made with the older words are kept until you confirm that you saved the new words." }),
      h("button", { type: "button", class: "primary", text: "I have saved it", onclick: () => actions.wordsSaved?.() })));
  }
  const mayShow = actions.wordsMayShow !== false;
  const cli = h("code", { class: "mono cli-line", text: text(b.cli_line ?? "orch-hub backup-key") });
  if (!b.key_set) {
    card.append(h("p", { class: "muted", text: "A backup keeps your people, keys and settings safe. It is sealed with a key that only the 24 backup words open. Every workspace keeps working without the hub, with or without a backup." }));
    if (mayShow) card.append(h("div", { class: "setup" }, stepUpForm("bk-key", "Set up backups", (c) => actions.setKey?.(c))));
    else card.append(h("p", { class: "small muted", text: NO_WORDS_HERE }), cli);
    return card;
  }
  // append() writes the text "null" for a null child (the note slot is empty most of the time): keep only real nodes.
  card.append(...[
    h("p", { class: "small muted", text: "A backup is made every day. The " + text(b.keep) + " newest are kept on this server. The files are sealed: only the 24 words open them." }),
    backupRows(b, actions),
    backupNow(b, actions),
    backupNoteView(b.note),
    h("p", { class: "small muted", text: "To keep a copy in another place, download a file and store it away from this server." })].filter(Boolean));
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

// --- Danger zone: Remove hub (#1463). One sheet, the step-up fields, one red button, no typed word. -----------------------
const RM_WHY = {
  hub_closing: "The hub is already being removed.",
  bad_credentials: WHY.bad_credentials,
  bad_channel_proof: "The signature was not accepted.",
  factor_too_weak: "Sign in again with your password and a new code, then try once more.",
  nonce_invalid: "This request ran out. Try again.",
  nonce_used: "This request was already used. Try again.",
  no_backup_key: "Set up the backup words first.",
  removal_failed: "The hub could not be removed. Nothing was changed.",
  preflight_failed: "The hub is not ready to be removed. Nothing was changed.",
  rate_limited: "Too many tries. Wait a moment and try again.",
  forbidden_role: "Only an admin can remove the hub.",
  cancel_refused: "The hub has already withdrawn company content. Finish removing it, or restore the last backup.",
};
const RM_STEP = {
  closing: "Stopping changes",
  preflight: "Checking everything is ready",
  backed_up: "Last backup made",
  withdrawn: "Company secrets, skills and tools withdrawn",
  notified: "Workspaces told",
  ordered: "Taking this hub down",
  stalled: "Removing stopped part way",
  failed: "Removing failed",
};

/** The facts of the sheet, in plain words, from the preview. */
export function removeFacts(pv) {
  const n = Number(pv.workspaces) || 0;
  const out = [(n === 1 ? "1 workspace is" : n + " workspaces are") + " connected. They keep working on their own and are asked to leave. Published links and shared spaces stop. Company secrets, skills and tools are withdrawn.",
    "A last backup is kept in your home folder" + (pv.backup_folder_hint ? " (" + text(pv.backup_folder_hint) + ")" : "") + ". You need the 24 backup words to restore it."];
  if (pv.relay || pv.publish) out.push("The relay and publish service on this server are removed too.");
  return out;
}

function removeStatusLines(st) {
  const lines = [h("p", { role: "status", "data-step": st.state, text: text(st.plain || RM_STEP[st.state] || "Removing") })];
  if (Number(st.total) > 0 && st.state === "notified") lines.push(h("p", { class: "small muted", text: Number(st.told) + " of " + Number(st.total) + " workspaces told" }));
  return lines;
}

/** The Danger zone card. `rm` is {preview, status}; `ac` is actions.remove ({start, status, cancel, poll}). */
export function removeCard(rm, ac = {}) {
  const slot = h("div", { class: "remove-sheet", "data-sheet": "remove" });
  const poll = { pause: 1000, goneMs: 10000, ...(ac.poll ?? {}) };
  const say = (code, plain) => text(RM_WHY[code] ?? plain ?? "This did not work. Nothing was changed.");
  const close = () => slot.replaceChildren();
  async function watch() {
    let misses = 0;
    for (;;) {
      let st = null;
      try { st = await ac.status?.(); } catch { st = null; }
      if (!st) {
        misses++;
        if (misses * poll.pause >= poll.goneMs) { slot.replaceChildren(h("p", { role: "status", "data-gone": "", text: "The hub is gone." })); return "gone"; }
      } else {
        misses = 0;
        const kids = removeStatusLines(st);
        if (st.state === "stalled") kids.push(h("p", { class: "small muted", text: "Removing stopped part way. Sign in again to finish." }), startForm("Finish removing"));
        if (st.state === "failed" || st.result?.ok === false) kids.push(h("p", { role: "alert", text: say(st.result?.code, st.result?.plain) }));
        if (st.cancel_allowed) kids.push(h("button", { type: "button", "data-act": "cancel", text: "Cancel", onclick: async () => {
          const x = await ac.cancel?.();
          if (x?.ok) close(); else slot.append(h("p", { role: "alert", text: say(x?.error, x?.plain) }));
        } }));
        slot.replaceChildren(...kids);
        if (st.state === "stalled" || st.state === "failed") return st.state;
        if (st.result?.ok === true) return "done";
      }
      await new Promise((r) => setTimeout(r, poll.pause));
    }
  }
  function startForm(label) {
    return stepUpForm("rm", label, async (c) => {
      const x = await ac.start?.(c);
      if (x?.ok) { slot.replaceChildren(h("p", { role: "status", text: "Removing. This page will stop answering." })); return watch(); }
      if (x?.reauth) return ac.reauth?.();
      slot.append(h("p", { role: "alert", "data-why": "", text: say(x?.error, x?.plain) }));
    }, false, close, "primary danger");
  }
  function sheet() {
    const pv = rm.preview ?? {};
    const kids = [h("h3", { text: "Remove this hub" }), ...removeFacts(pv).map((t) => h("p", { text: t }))];
    if (pv.refusal) kids.push(h("p", { role: "alert", text: text(pv.refusal) }));
    if (pv.backup_key_ok === false) kids.push(h("p", { role: "alert", text: "Set up the backup words first." }), h("button", { type: "button", text: "Close", onclick: close }));
    else if (!pv.refusal) kids.push(startForm("Remove this hub"));
    slot.replaceChildren(...kids);
  }
  const node = h("div", { class: "card", "data-card": "danger" }, h("h2", { text: "Danger zone" }),
    h("p", { class: "small muted", text: "Removing the hub does not delete your workspaces. They keep working on their own." }),
    h("button", { type: "button", "data-act": "remove-hub", text: "Remove hub", onclick: async () => {
      if (rm.preview?.state && rm.preview.state !== "open") return watch();
      sheet();
    } }), slot);
  if (rm.preview?.state && rm.preview.state !== "open") void watch();
  return node;
}

/** The actions behind the Danger zone card: prepare, check the digest here, sign with this browser's key, confirm. */
export function removeActions(d) {
  const err = (x) => ({ ok: false, error: x.body?.error, plain: x.body?.plain });
  return {
    poll: d.removePoll,
    reauth: () => d.reauthView(),
    status: async () => { const x = await d.api("GET", "/admin/hub/remove/status"); return x.status === 200 ? x.body : null; },
    cancel: async () => { const x = await d.api("POST", "/admin/hub/remove/cancel", {}); return x.status < 300 ? { ok: true } : err(x); },
    start: async (c) => {
      const me = d.state.me;
      if (!me) return { ok: false, plain: "Register this browser first." };
      const p = await d.api("POST", "/admin/hub/remove/prepare", { channel_id: me.channelId });
      if (p.status === 403 && p.body?.error === "reauth_required") return { reauth: true };
      if (p.status !== 200) return err(p);
      let digest;
      try {
        digest = await hubRemoveDigest(p.body.ctx);
        if (p.body.ctx.kind !== "orch.hub_remove.v1" || p.body.ctx.nonce !== p.body.nonce || p.body.ctx.hub_id !== me.hubId || p.body.ctx.user_id !== me.userId || hex(digest) !== p.body.digest) throw new Error("mismatch");
      } catch { return { ok: false, plain: "The request could not be checked, so nothing was signed. Nothing was changed." }; }
      let proof;
      try { proof = await signProof(me.pair.privateKey, digest); } catch { return { ok: false, plain: "This browser could not sign. Nothing was changed." }; }
      const x = await d.api("POST", "/admin/hub/remove/confirm", { nonce: p.body.nonce, proof, password: c.password, totp: c.totp });
      if (x.status === 403 && x.body?.error === "reauth_required") return { reauth: true };
      return x.status === 202 ? { ok: true } : err(x);
    },
  };
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
      (data.sign_in ?? []).map((m) => row(m.label, m.on ? m.note : null, m.on)),
      h("p", { class: "small muted", text: "Always on: the password and the code make the key that approves things, so they cannot be turned off." }),
      data.company_login ? companyCard(data.company_login, actions.company ?? {}, { inside: true }) : null),
    data.remove ? removeCard(data.remove, actions.remove ?? {}) : null);
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

/** After a restore the hub restarts and every session is gone: show progress at once, wait for the hub, then open the sign-in page
 *  with a note that says the restore worked. `state.restarting` pauses the "No connection" bar meanwhile. */
async function afterRestore(d, madeMs) {
  d.state.restarting = true;
  try {
    const wait = h("div", { class: "card restoring", role: "status", "data-restoring": "1" },
      h("div", { class: "spin", "aria-hidden": "true" }),
      h("div", {}, h("b", { text: "Restoring your hub…" }), h("div", { class: "small muted", text: "This takes a few seconds. Please keep this page open." })));
    d.shell("hub-settings", null, h("div", { class: "hub-settings" }, h("h1", { text: "Hub settings" }), wait));
    while (!(await comesBack(d))) {
      await new Promise((resolve) => {
        wait.replaceChildren(h("div", {}, h("b", { text: "The hub is taking longer than usual." }), h("div", { class: "small muted", text: "Try again in a moment." }),
          h("button", { type: "button", class: "primary", text: "Try now", onclick: () => { wait.replaceChildren(h("div", { class: "spin", "aria-hidden": "true" }), h("b", { text: "Restoring your hub…" })); resolve(); } })));
      });
    }
  } finally { d.state.restarting = false; }
  const at = madeMs ? "Restored from the backup of " + text(when(madeMs)) + ". Sign in again." : "Restored. Sign in again.";
  return d.loginView(null, { title: "Sign in to your hub", ok: at });
}

export async function hubSettingsPage(d, detected = [], words = null, opened = false) {
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
    const co = await d.api("GET", "/admin/oidc");
    if (co.status === 200 && typeof co.body?.has_secret === "boolean") r.body.company_login = co.body; // the company login card (its own key: `company` is the company name), when the hub has the routes
    const bk = await d.api("GET", "/admin/backup");
    if (bk.status === 200) r.body.backup = bk.body;
    const rv = await d.api("GET", "/admin/hub/remove/preview");
    if (rv.status === 200) r.body.remove = { preview: rv.body };
    const say = (ok, x) => { d.state.notice = x.status < 300 ? d.banner("ok", ok, null) : d.banner("bad", "Not changed", WHY[x.body.error] ?? d.say(x.body.error ?? "unavailable")); };
    const backupNote = (x, ok) => { d.state.notice = x.status < 300 ? d.banner("ok", ok, null) : d.banner("bad", "Not done", WHY[x.body.error] ?? d.say(x.body.error ?? "unavailable")); };
    // P07: the result of Back up now and Download shows in the Backup card, not at the top of the page. It lives for one render.
    const slotNote = (x, ok, extra = {}) => {
      d.state.backupNote = x.status < 300 ? { kind: "ok", title: ok, ...extra }
        : { kind: "bad", title: "Not done", detail: WHY[x.body.error] ?? d.say(x.body.error ?? "unavailable") };
    };
    if (r.body.backup && d.state.backupNote) { r.body.backup.note = d.state.backupNote; d.state.backupNote = null; }
    const actions = {
      detected,
      opened, // the admin pressed the address button (not "the list happens to be non-empty")
      words,
      remove: removeActions(d),
      wordsMayShow: wordsMayShow(globalThis.location, (r.body.addresses ?? []).filter((a) => a.kind === "private").map((a) => a.web)),
      copy: async (t) => { try { await globalThis.navigator?.clipboard?.writeText(t); return true; } catch { return false; } },
      wordsSaved: async () => {
        const x = await d.api("POST", "/admin/backup/key/confirm", {});
        if (x.status !== 200) backupNote(x, "");
        await hubSettingsPage(d);
      },
      setKey: async (c) => {
        const x = await d.api("POST", "/admin/backup/key", c);
        if (x.status === 200) { d.state.notice = d.banner("ok", "Backups are set up", null); return hubSettingsPage(d, [], x.body.words); }
        backupNote(x, ""); return hubSettingsPage(d);
      },
      restore: async (name, c) => {
        const x = await d.api("POST", "/admin/backup/restore", { name, ...c });
        if (x.status === 202) return afterRestore(d, (bk.body?.backups ?? []).find((b) => b.name === name)?.made_ms);
        backupNote(x, "Restored"); await hubSettingsPage(d);
      },
      confirmPending: async (id) => { backupNote(await d.api("POST", "/admin/backup/restore/pending/confirm", { id }), "Turned on"); await hubSettingsPage(d); },
      dismissRestored: async () => { await d.api("POST", "/admin/backup/restored/dismiss", {}); await hubSettingsPage(d); },
      makeBackup: async () => { slotNote(await d.api("POST", "/admin/backups", {}), "Backup made"); await hubSettingsPage(d); },
      download: async (name, c) => {
        const x = await d.api("POST", "/admin/backups/download", { name, ...c });
        if (x.status !== 200) { slotNote(x, ""); return hubSettingsPage(d); }
        try {
          const bin = atob(String(x.body.data).replace(/-/g, "+").replace(/_/g, "/"));
          const bytes = Uint8Array.from(bin, (ch) => ch.charCodeAt(0));
          const a = h("a", { href: URL.createObjectURL(new Blob([bytes], { type: "application/octet-stream" })), download: x.body.name });
          a.click?.();
          slotNote({ status: 200 }, "Backup downloaded", { downloaded: name });
        } catch { slotNote({ status: 500, body: { error: "unavailable" } }, ""); }
        await hubSettingsPage(d);
      },
      company: {
        save: async (body) => { const x = await d.api("PUT", "/admin/oidc", body); if (x.status === 200) setTimeout(() => hubSettingsPage(d), 400); return x; },
        test: () => beginCompany(d.api, "test"),
        copy: async (t) => { try { await globalThis.navigator?.clipboard?.writeText(t); return true; } catch { return false; } },
      },
      saveGeneral: async (body) => { say("Saved", await d.api("PUT", "/admin/settings/general", body)); await hubSettingsPage(d); },
      loadDetected: async () => { const x = await d.api("GET", "/admin/settings/addresses/detected"); if (x.status === 200) await hubSettingsPage(d, x.body.addresses ?? [], null, true); },
      saveAddresses: async (body) => {
        const x = await d.api("PUT", "/admin/settings/addresses", body);
        if (x.status === 202) { d.state.notice = d.banner("ok", "Saved. The hub is restarting.", null); await comesBack(d); say("Saved", { status: 200 }); }
        else say("Saved", x);
        await hubSettingsPage(d);
      },
    };
    d.shell("hub-settings", d.state.notice, hubSettingsView(r.body, actions));
    globalThis.document?.querySelector?.("[data-backup-note]")?.scrollIntoView?.({ block: "nearest" });
    d.state.notice = null;
  } finally { finish(); }
}

// The registry entry: the page's file name is its path (settings is a route of orch's own, so the file is hub-settings).
export const page = { path: "hub-settings", nav: { label: "Hub settings", admin: true, after: "admin" }, render: hubSettingsPage };
