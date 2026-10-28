// The Add-ons page (M3, #1448-#1450): which add-ons each workspace may install, and the requests waiting for a decision.
// The hub only RESTRICTS: allowing an add-on here never installs it; the workspace owner still decides, in the workspace.
import { emptyState, loading } from "./feel.js";
import { chip, h, text, when } from "./ui.js";

const info = (label, body) => h("details", { class: "info" }, h("summary", { "aria-label": label, text: "i" }), h("p", { class: "small muted", text: body }));
const nameOf = (w) => text(w?.name || (w?.workspace_id ? "Workspace " + w.workspace_id.slice(0, 8) : "A workspace"));

/** Parse "a, b c" into add-on ids: lower-case words with dashes. Returns {ids, bad} so the page can say what it did not understand. */
export function parseIds(value) {
  const words = String(value ?? "").split(/[\s,]+/).filter(Boolean);
  const ids = [...new Set(words.filter((w) => /^[a-z][a-z0-9-]{0,39}$/.test(w)))].sort();
  return { ids, bad: words.filter((w) => !/^[a-z][a-z0-9-]{0,39}$/.test(w)) };
}

export function addonsView(data, actions = {}, now = Date.now()) {
  const workspaces = data.workspaces ?? [];
  const everything = data.allow == null;
  const root = h("div", { class: "addons" },
    h("div", { class: "row" }, h("h1", { class: "grow", text: "Add-ons" }),
      info("About add-on rules", "Installing is always decided by the workspace owner. What you set here only decides what a workspace is allowed to install; it never installs anything. The official marketplace is built in and add-ons start Off.")),
    h("p", { text: "Which add-ons each workspace may install." }));
  root.append(h("section", { class: "card" }, h("h2", { text: "Allowed everywhere" }),
    everything ? h("p", { text: "Anything from an allowed marketplace." })
      : h("div", { class: "chips" }, (data.allow.length ? data.allow : []).map((id) => chip(text(id))), data.allow.length ? null : h("p", { class: "small muted", text: "Nothing yet." })),
    (data.block ?? []).length ? h("p", { class: "small muted", text: "Never allowed: " + data.block.map(text).join(", ") }) : null,
    h("p", { class: "small muted", text: "Change the common rules in Admin > Marketplaces." })));
  const requests = data.requests ?? [];
  const list = h("section", { class: "card", id: "addon-requests" }, h("h2", { text: "Requests" }),
    requests.length ? requests.map((r) => {
      const allow = h("button", { type: "button", class: "pri", text: everything ? "Allow" : "Allow for everyone", onclick: () => actions.decide?.(r, true) });
      const refuse = h("button", { type: "button", text: "Turn down", onclick: () => actions.decide?.(r, false) });
      return h("div", { class: "list-row row" }, h("div", { class: "grow" },
        h("strong", { text: (r.actor === "agent" ? "An agent in " : "") + nameOf({ name: r.workspace_name }) + " asked for " + text(r.name) + (r.version ? " " + text(r.version) : "") }),
        h("p", { class: "small muted", text: (r.count > 1 ? r.count + " times, last " : "") + when(r.last_ms, now) })), allow, refuse);
    }) : emptyState("addon-requests"));
  root.append(list);
  root.append(h("section", { class: "card" }, h("h2", { text: "Per workspace" }),
    h("p", { class: "small muted", text: "A workspace can be limited to fewer add-ons than the common list. It can never be given more." }),
    workspaces.length ? workspaces.map((w) => {
      const limited = Array.isArray(w.allow);
      const check = h("input", { type: "checkbox", checked: limited, "aria-label": "Limit " + nameOf(w) });
      const input = h("input", { type: "text", value: (w.allow ?? []).join(", "), "aria-label": "Add-ons " + nameOf(w) + " may install" });
      const note = h("p", { role: "status", class: "small" });
      const save = h("button", { type: "button", text: "Save", onclick: async () => {
        const { ids, bad } = parseIds(input.value);
        if (check.checked && bad.length) { note.textContent = "Not understood: " + bad.map(text).join(", ") + ". Use lower-case names separated by commas."; return; }
        save.disabled = true;
        try { await actions.save?.(w, check.checked ? ids : null); note.textContent = "Saved"; } catch { note.textContent = "Not saved. Please try again."; } finally { save.disabled = false; }
      } });
      return h("div", { class: "list-row" }, h("div", { class: "row" }, h("b", { class: "grow", text: nameOf(w) }), h("label", {}, check, " Limit"), input, save),
        h("p", { class: "small muted", text: limited ? "Only the add-ons listed (that the common list also allows)." : "No limit of its own: the common list applies." }), note);
    }) : emptyState("workspaces")));
  return root;
}

export async function addonsPage(d) {
  const finish = loading();
  d.pageLoading("addons");
  try {
    if (!(await d.refresh())) return d.loginView();
    if (!d.state.user.is_admin) return d.home();
    if (d.state.user.needs_sign_in) return d.reauthView();
    await d.loadRail();
    const r = await d.api("GET", "/admin/addons");
    if (r.status === 403 && r.body.error === "reauth_required") return d.reauthView();
    if (r.status !== 200) return d.shell("addons", h("h1", { text: "Add-ons" }), h("p", { role: "alert", text: "Cannot read the add-on rules. " + d.say(r.body.error) }));
    const reload = () => addonsPage(d);
    const save = async (w, ids) => { const x = await d.api("PUT", "/admin/addons/" + encodeURIComponent(w.workspace_id), { allow: ids }); if (x.status !== 200) throw new Error("save"); };
    const decide = async (req, approve) => {
      const x = await d.api("POST", "/admin/addon-requests", { workspace_id: req.workspace_id, name: req.name, approve });
      d.state.notice = x.status === 200 ? d.banner("ok", approve ? "Allowed" : "Turned down", approve ? "The owner can now choose to install it in the workspace." : null) : d.banner("bad", "Not changed", d.say(x.body.error));
      await reload();
    };
    d.shell("addons", d.state.notice, addonsView(r.body, { save, decide }));
    d.state.notice = null;
  } finally { finish(); }
}
