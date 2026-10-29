// The Hub settings page (M4, #1460): what the hub is called, the addresses fixed at install, how people sign in.
// Read only: the page shows what already works and nothing else. The page signs nothing and changes nothing.
import { loading } from "../feel.js";
import { chip, h, text } from "../ui.js";

// The words for an address kind (owner rule: never a product name for the private network).
const KINDS = [
  ["private", "Private network (VPN)"],
  ["server", "On this server"],
  ["office", "Office network"],
  ["public", "Public address"],
];

const row = (label, note, on) => h("div", { class: "list-row row" },
  h("div", { class: "grow" }, h("b", { text: text(label) }), note ? h("div", { class: "small muted", text: text(note) }) : null),
  on ? chip("On", "ok") : chip("Later"));

export function hubSettingsView(data) {
  if (!data.configured) {
    return h("div", { class: "hub-settings" }, h("h1", { text: "Hub settings" }),
      h("p", { class: "small muted", role: "status", text: data.problem ? text(data.problem) : "This hub was started by hand, so it has no settings file. Install it with orch install hub to see its settings here." }));
  }
  const facts = [["Hub name", data.name || "Hub"], ["Company", data.company || "Not set"]];
  const byKind = new Map((data.addresses ?? []).map((a) => [a.kind, a.web]));
  const addr = KINDS.map(([kind, label]) => {
    const web = byKind.get(kind);
    return [h("dt", { text: label }), h("dd", {}, web ? h("span", { class: "mono", text: text(web) }) : h("span", { class: "muted", text: "Not set up" }),
      kind === "server" && web ? h("span", { class: "small muted", text: " (on the server itself, use this one)" }) : null)];
  });
  return h("div", { class: "hub-settings" },
    h("h1", { text: "Hub settings" }),
    h("p", { class: "lead muted", text: "How this hub is reached and how people sign in." }),
    h("div", { class: "card", "data-card": "general" }, h("h2", { text: "General" }),
      h("dl", { class: "facts" }, facts.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: text(v) })]))),
    h("div", { class: "card", "data-card": "address" }, h("h2", { text: "Address" }),
      h("dl", { class: "facts" }, addr.flat()),
      h("p", { class: "small muted", text: "These addresses were chosen when the hub was installed. To use another one, install the hub again with that address." })),
    h("div", { class: "card", "data-card": "sign-in" }, h("h2", { text: "Sign-in" }),
      (data.sign_in ?? []).map((m) => row(m.label, m.on ? m.note : null, m.on))));
}

export async function hubSettingsPage(d) {
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
    d.shell("hub-settings", hubSettingsView(r.body));
  } finally { finish(); }
}

// The registry entry: the page's file name is its path (settings is a route of orch's own, so the file is hub-settings).
export const page = { path: "hub-settings", nav: { label: "Hub settings", admin: true, after: "admin" }, render: hubSettingsPage };
