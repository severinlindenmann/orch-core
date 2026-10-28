import { h, text } from "./ui.js";
import { emptyState, loading } from "./feel.js";

const level = (role) => ({ read: "Read", edit: "Edit", admin: "Admin", owner: "Owner" }[role] ?? "No access");

export function personReachView(d, people) {
  let generation = 0;
  const result = h("div", { "aria-live": "polite" });
  const person = h("select", { onchange: check, disabled: !people.length },
    h("option", { value: "", text: "Choose a person" }),
    people.map((u) => h("option", { value: u.user_id, text: text(u.name) })));
  async function check() {
    const current = ++generation;
    if (!person.value) { result.replaceChildren(); return; }
    const done = loading();
    result.replaceChildren(h("p", { role: "status", text: "Checking access…" }));
    try {
      const r = await d.api("GET", "/admin/people/" + encodeURIComponent(person.value) + "/reach");
      if (current !== generation) return;
      if (r.status !== 200) throw new Error(r.body?.error ?? "unavailable");
      const rows = r.body.workspaces;
      result.replaceChildren(rows.length ? h("div", { class: "scroll", tabindex: "0", role: "region", "aria-label": "Person’s workspace access" },
        h("table", { class: "grid" }, h("caption", { text: "Workspace access for " + text(r.body.user.name) }),
          h("thead", {}, h("tr", {}, ["Workspace", "Level", "Because of"].map((s) => h("th", { scope: "col", text: s })))),
          h("tbody", {}, rows.map((w) => {
            const reasons = w.via.map((v) => v.kind === "group" ? "member of group " + text(v.group) + " (" + level(v.role) + ")"
              : v.kind === "hub-admin" ? "hub administrator" : "owner");
            if (w.mode === "selective") reasons.push("Personal: only " + text(w.owner?.name || "its owner"));
            return h("tr", {}, h("th", { scope: "row", text: text(w.name || "Unnamed workspace") }),
              h("td", { text: level(w.role) }), h("td", { text: reasons.join("; ") || "No access" }));
          })))) : emptyState("workspaces"));
    } catch (e) {
      if (current === generation) result.replaceChildren(h("p", { role: "alert", text: d.say(e.message) }),
        h("button", { type: "button", text: "Try again", onclick: check }));
    } finally { done(); }
  }
  return h("section", { class: "card", "aria-label": "Check a person" }, h("h3", { text: "Check a person" }),
    h("label", {}, "Person", person), !people.length ? h("p", { text: "No people yet." }) : null, result);
}
