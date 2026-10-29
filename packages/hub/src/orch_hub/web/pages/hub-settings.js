// The Hub settings page (M4, #1460 #1461): what the hub is called, the addresses it advertises, how people sign in.
// General and Address are editable (one Save per card, plain clicks, no typed confirm word). The page signs nothing: the
// hub checks every value again, writes it to its settings file and records the change in its audit log.
import { loading } from "../feel.js";
import { chip, h, text } from "../ui.js";

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
 * `picks` maps a kind to the chosen host ("" = not used); `secure` says the public address is https.
 */
export function addressBody(data, picks, secure = false) {
  const phone = (data.addresses ?? [])[0];
  const out = [];
  if (phone) out.push({ kind: phone.kind, host: hostOf(phone.web), ...(phone.kind === "public" && schemeOf(phone.web) === "https" ? { scheme: "https" } : {}) });
  for (const [kind] of KINDS) {
    if (phone && kind === phone.kind) continue;
    const host = String(picks[kind] ?? "").trim();
    if (!host) continue;
    out.push(kind === "public" ? { kind, host, ...(secure ? { scheme: "https" } : {}) } : { kind, host });
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
    card.append(h("p", { class: "small muted", text: "This hub only answers on this server. To use another address, install the hub again with that address." }));
    return card;
  }
  const detected = actions.detected ?? [];
  const here = globalThis.location?.origin ?? "";
  const picks = {}; const fields = [];
  for (const [kind, label] of KINDS) {
    if (kind === data.phone_kind) { fields.push(h("div", { class: "field", "data-kind": kind }, h("label", { text: label }), h("span", { class: "small muted", text: "Phones use this one. It stays as it is." }))); continue; }
    const cur = byKind.get(kind);
    if (kind === "public") {
      const input = h("input", { type: "text", id: "addr-public", maxlength: "100", placeholder: "hub.example.com", autocomplete: "off" });
      input.value = cur ? hostOf(cur) : ""; picks.public = input;
      const secure = h("input", { type: "checkbox", id: "addr-secure" });
      secure.checked = !!cur && schemeOf(cur) === "https"; picks.secure = secure;
      fields.push(h("div", { class: "field", "data-kind": kind }, h("label", { for: "addr-public", text: label }), input,
        h("label", { class: "small" }, secure, " Reached with https")));
      continue;
    }
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
      actions.saveAddresses?.(addressBody(data, chosen, !!picks.secure?.checked));
    } }));
  const open = h("button", { type: "button", text: "Change address", hidden: opened, onclick: () => { form.hidden = false; open.hidden = true; actions.loadDetected?.(); } });
  card.append(open, form);
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

export async function hubSettingsPage(d, detected = []) {
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
    const say = (ok, x) => { d.state.notice = x.status < 300 ? d.banner("ok", ok, null) : d.banner("bad", "Not changed", WHY[x.body.error] ?? d.say(x.body.error ?? "unavailable")); };
    const actions = {
      detected,
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
