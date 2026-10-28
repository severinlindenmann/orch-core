// The Phones & browsers page (#1427 #1428 #1429): every phone and browser that can approve for a person, Remove, "Pair a
// phone" from the console, "A new phone was added" notices and the amber "Update needed" chip. Plain words, no terminal
// wording. createElement and textContent only (tests/js/lint.test.mjs); every string that came from a phone or a person
// (its name, its app version) goes through renderText first and is never used as markup.
import { chip, h, text, when } from "./ui.js";

export const REMOVE_WARNING = "It can no longer approve. This cannot be undone.";
export const PAIR_STEPS = "Open the orch app, choose Add hub, enter the address and this code.";
const small = (label, cls = "muted") => h("p", { class: "small " + cls, text: label });
const KIND = { phone: "Phone", browser: "Browser" };
const kindWord = (c) => KIND[c.kind] ?? "Device";
const byStatus = (a, b) => (a.status === "active" ? 0 : 1) - (b.status === "active" ? 0 : 1) || (a.created_ms ?? 0) - (b.created_ms ?? 0);

/** "Time left: 4 min 05 s", or the expired sentence. Pure, so it can be tested without a clock. */
export function remainingText(expiresMs, now = Date.now()) {
  const s = Math.ceil((expiresMs - now) / 1000);
  if (!Number.isFinite(s) || s <= 0) return "This code has expired. Make a new one.";
  return "Time left: " + Math.floor(s / 60) + " min " + String(s % 60).padStart(2, "0") + " s";
}

/** "A new phone was added: <name> at <time>. Not you? Remove it." with Remove and Dismiss. Shown on the Phones page and on
 *  the page about the person's account. `list` is GET /me/channels `new_channels`; the hub sends it to the browser console
 *  only, and only to that person. */
export function newChannelsNotice(list, actions = {}, now = Date.now()) {
  return (list ?? []).filter((c) => c && c.seen !== true).map((c) => {
    const error = h("p", { role: "alert", class: "small" });
    const remove = h("button", { class: "danger", text: "Remove it", onclick: async () => {
      remove.disabled = true;
      error.textContent = "";
      try { await actions.remove?.(c); } catch { error.textContent = "Could not remove it. Please try again."; remove.disabled = false; }
    } });
    return h("div", { class: "banner warn new-channel", role: "status", "data-channel": c.channel_id },
      h("b", { text: "A new " + (c.kind === "browser" ? "browser" : "phone") + " was added: " + text(c.label || kindWord(c)) + " at " + when(c.created_ms, now) + ". Not you? Remove it." }),
      h("div", { class: "row" }, remove, h("button", { text: "That was me", onclick: () => actions.dismiss?.() })), error);
  });
}

function rowView(c, data, actions, now) {
  const box = h("article", { class: "list-row row phone-row", "data-channel": c.channel_id, "data-status": c.status });
  const status = c.status === "active" ? chip("Active", "ok") : c.status === "revoked" ? chip("Removed") : chip("Expired", "warn");
  const mineHere = data.thisChannelId && data.thisChannelId === c.channel_id;
  const info = h("div", { class: "grow" },
    h("b", { text: text(c.label || kindWord(c)) + (mineHere ? " (this browser)" : "") }),
    small(kindWord(c) + " · Added " + when(c.created_ms, now)),
    c.app_version ? small("App version " + text(c.app_version)) : null,
    c.update_needed === true ? h("div", { class: "row" }, chip("Update needed", "warn"), small("This phone runs an old orch app. Update the app on the phone.")) : null);
  const idle = () => {
    const keep = [info, status];
    if (c.status === "active") keep.push(h("button", { text: "Remove", onclick: ask }));
    box.replaceChildren(...keep);
  };
  const ask = () => {
    const error = h("p", { role: "alert", class: "small" });
    const yes = h("button", { class: "danger", text: "Remove", onclick: async () => {
      yes.disabled = true;
      error.textContent = "";
      try { await actions.remove?.(c); } catch { error.textContent = "Could not remove it. Please try again."; yes.disabled = false; }
    } });
    box.replaceChildren(h("div", { class: "grow" }, h("b", { text: text(c.label || kindWord(c)) }), small(REMOVE_WARNING, ""), error),
      h("button", { text: "Keep it", onclick: idle }), yes);
  };
  idle();
  return box;
}

/** The card that makes a one-time pairing code for the signed-in person's own phone. `actions.pair({password, totp})`
 *  resolves {ok: true, code, expires_ms, link?, address?} or {ok: false, error: "plain words"}; `actions.copy(text)`;
 *  `actions.tick(fn)` (default: once a second) returns a stop function. */
export function pairCard(actions = {}, now = Date.now()) {
  const pw = h("input", { id: "pair-pw", type: "password", autocomplete: "current-password", required: true, "aria-label": "Password" });
  const code = h("input", { id: "pair-totp", inputmode: "numeric", autocomplete: "one-time-code", maxlength: 6, required: true, "aria-label": "6-digit code" });
  const out = h("div", { "aria-live": "polite" });
  let stop = null;
  const clock = () => (actions.now ?? Date.now)();
  const copyButton = (label, value, aria) => {
    const b = h("button", { type: "button", class: "copy", text: label, "aria-label": aria, onclick: async () => {
      let ok = false;
      try { ok = !!(await actions.copy?.(value)); } catch { ok = false; }
      b.textContent = ok ? "Copied" : "Select and copy";
    } });
    return b;
  };
  const result = (r) => {
    const left = h("p", { class: "pair-left", role: "timer", text: remainingText(r.expires_ms, clock()) });
    const rows = [
      h("p", { class: "muted", text: "Your pairing code. It works once, only for you." }),
      h("div", { class: "row" }, h("code", { class: "pair-code fp", id: "pair-code-text", text: text(r.code) }), copyButton("Copy", r.code, "Copy the pairing code")),
      left,
      h("p", { class: "pair-steps", text: PAIR_STEPS }),
      r.address ? h("p", { class: "pair-address", text: "Address: " + text(r.address) }) : null,
    ].filter(Boolean);
    if (r.link) rows.push(h("p", { class: "small muted", text: "Or open this pairing link on your phone:" }), h("div", { class: "row" }, h("code", { class: "pair-link fp", text: text(r.link) }), copyButton("Copy link", r.link, "Copy the pairing link")));
    rows.push(small("A QR code is not shown here yet. Your IT person can show one for you."));
    out.replaceChildren(...rows);
    stop?.();
    stop = (actions.tick ?? ((fn) => { const t = setInterval(fn, 1000); return () => clearInterval(t); }))(() => {
      left.textContent = remainingText(r.expires_ms, clock());
      if (clock() >= r.expires_ms || left.isConnected === false) { stop?.(); stop = null; }
    });
  };
  const form = h("form", { class: "pair-form", onsubmit: async (ev) => {
    ev.preventDefault();
    const button = form.querySelector?.("[type=submit]");
    if (button) button.disabled = true;
    let r;
    try { r = await actions.pair({ password: pw.value, totp: code.value.trim() }); } catch { r = { ok: false, error: "The hub cannot be reached." }; }
    pw.value = "";
    code.value = "";
    if (button) button.disabled = false;
    if (!r?.ok) {
      stop?.(); stop = null;
      return out.replaceChildren(h("div", { class: "banner bad", role: "alert" }, h("b", { text: "No code was made" }), h("div", { class: "small", text: text(r?.error ?? "Please try again.") })));
    }
    result(r);
  } },
    h("p", { class: "muted small", text: "Type your password and a new 6-digit code from your authenticator app. Each code works only once, so wait for the next one if you just signed in." }),
    h("div", {}, h("label", { for: "pair-pw", text: "Password" }), pw),
    h("div", {}, h("label", { for: "pair-totp", text: "6-digit code" }), code),
    h("div", { class: "row top" }, h("button", { class: "pri", type: "submit", text: "Show a pairing code" })));
  return h("section", { class: "card pair-card", "aria-labelledby": "pair-h" }, h("h2", { id: "pair-h", text: "Pair a phone" }),
    h("p", { class: "muted", text: "Add your own iPhone so you can approve there. A new code replaces the last one." }), form, out);
}

function listSection(title, rows, data, actions, now, key) {
  return h("section", { class: "card phones-group", "data-person": key, "aria-label": title },
    h("h2", { text: title }),
    rows.length ? [...rows].sort(byStatus).map((c) => rowView(c, data, actions, now)) : small("No phones or browsers yet."));
}

/** data: {admin, user_id, channels, users?, newChannels?, thisChannelId?}. An admin sees everyone, grouped by person; any
 *  other person sees only their own, on the same page (the hub scopes that by the session, not by anything sent here).
 *  actions: {remove(channel), dismiss(), pair({password, totp}), copy(text), tick?(fn)}. */
export function phonesView(data, actions = {}, now = Date.now()) {
  const channels = data.channels ?? [];
  const intro = h("p", { class: "muted", text: data.admin
    ? "Every phone and browser that can approve for someone on this hub. Remove one and it can no longer approve."
    : "The phones and browsers that can approve for you. Remove any you do not use or do not recognise." });
  let groups;
  if (data.admin) {
    const users = [...(data.users ?? [])].sort((a, b) => (a.user_id === data.user_id ? -1 : b.user_id === data.user_id ? 1 : String(a.name ?? "").localeCompare(String(b.name ?? ""))));
    const known = new Set(users.map((u) => u.user_id));
    groups = users.map((u) => listSection(text(u.name ?? u.user_id) + (u.user_id === data.user_id ? " (you)" : ""), channels.filter((c) => c.user_id === u.user_id), data, actions, now, u.user_id));
    const strays = channels.filter((c) => !known.has(c.user_id));
    if (strays.length) groups.push(listSection("Someone no longer on this hub", strays, data, actions, now, "other"));
  } else {
    groups = [listSection("Your phones and browsers", channels, data, actions, now, data.user_id ?? "me")];
  }
  return [h("h1", { text: "Phones & browsers" }), intro, ...newChannelsNotice(data.newChannels, actions, now), pairCard(actions, now), ...groups];
}
