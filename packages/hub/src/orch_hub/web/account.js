import { h, text, chip, when } from "./ui.js";
import { busyButton, emptyState } from "./feel.js";

export function accountView(data, actions) {
  const channels = (data.channels ?? []).filter((c) => c.kind === "phone" || c.kind === "browser");
  const rows = channels.map((c) => {
    const kind = c.kind === "phone" ? "Phone" : "Browser";
    const status = ({ active: "Active", revoked: "Removed", expired: "Expired" })[c.status] ?? "Unavailable";
    const dates = [["Added", c.created_ms], ["Last used", c.last_used_ms]]
      .filter(([, ms]) => Number.isFinite(ms)).map(([label, ms]) => label + " " + when(ms)).join(" · ");
    const remove = c.status === "active" ? h("button", { text: "Remove", "aria-label": "Remove " + text(c.label || kind),
      onclick: () => busyButton(remove, () => actions.remove(c.channel_id)) }) : null;
    return h("div", { class: "list-row row" },
      h("div", { class: "grow" }, h("b", { text: text(c.label || kind) }),
        h("div", { class: "small muted", text: kind + " · " + status }),
        dates ? h("div", { class: "small muted", text: dates }) : null,
        c.status === "active" ? h("div", { class: "small", text: "After removal: It can no longer approve for you." }) : null),
      c.kind === "browser" && c.channel_id === data.thisBrowserId ? chip("This browser") : null, remove);
  });
  return [h("h1", { text: "Account" }),
    h("p", { text: "Signed in as " + text(data.user?.name ?? "") }),
    h("section", { class: "card" }, h("h2", { text: "My phones and browsers" }),
      rows.length ? rows : emptyState("account"),
      rows.length ? h("p", { class: "small muted", text: "To add a phone, open the phone app and choose Pair a phone." }) : null)];
}
