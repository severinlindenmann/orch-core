// The Add-ons page (M3, #1448-#1450): which add-ons each workspace may install, and the requests waiting for a decision.
// The hub only RESTRICTS: allowing an add-on here never installs it; the workspace owner still decides, in the workspace.
import { emptyState, loading } from "../feel.js";
import { chip, h, text, when, withNames } from "../ui.js";

const info = (label, body) => h("details", { class: "info" }, h("summary", { "aria-label": label, text: "i" }), h("p", { class: "small muted", text: body }));
const nameOf = (w) => text(w?.name || "A workspace");

/** The add-ons one workspace can be offered: what the common list names (when it is a list), what the workspace already has
 *  and what it asked for, minus the never-allowed ones. The hub has no catalogue, so with no common list this is only what
 *  is known from requests; nobody types a name. */
export function pickable(data, w) {
  const block = new Set(data.block ?? []);
  const asked = (data.requests ?? []).filter((r) => r.workspace_id === w.workspace_id).map((r) => r.name);
  return [...new Set([...(Array.isArray(data.allow) ? data.allow : []), ...(w.allow ?? []), ...asked])].filter((id) => !block.has(id)).sort();
}

/** True when approving a request would change nothing: no common list and no limit on that workspace. */
export function alreadyAllowed(data, workspaceId) {
  const w = (data.workspaces ?? []).find((x) => x.workspace_id === workspaceId);
  return data.allow == null && (w ? w.allow == null : true);
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
      const options = pickable(data, w);
      const check = h("input", { type: "checkbox", "aria-label": "Limit " + nameOf(w) });
      check.checked = limited;
      const boxes = options.map((id) => { const box = h("input", { type: "checkbox", "data-addon": id }); box.checked = (w.allow ?? []).includes(id); return { id, box }; });
      const picker = h("div", { class: "picker" }, options.length
        ? boxes.map(({ id, box }) => h("label", { class: "list-row row" }, box, h("span", { class: "grow", text: text(id) })))
        : h("p", { class: "small muted", text: "Nothing to choose yet. Add-ons show up here once the common list names them or this workspace asks for one." }));
      picker.hidden = !limited;
      check.addEventListener("change", () => { picker.hidden = !check.checked; });
      const note = h("p", { role: "status", class: "small" });
      const save = h("button", { type: "button", text: "Save", onclick: async () => {
        save.disabled = true;
        try { await actions.save?.(w, check.checked ? boxes.filter((x) => x.box.checked).map((x) => x.id) : null); note.textContent = "Saved"; } catch { note.textContent = "Not saved. Please try again."; } finally { save.disabled = false; }
      } });
      return h("div", { class: "list-row" }, h("div", { class: "row" }, h("b", { class: "grow", text: nameOf(w) }), h("label", {}, check, " Limit"), save),
        h("p", { class: "small muted", text: limited ? "Only the ticked add-ons (that the common list also allows)." : "No limit of its own: the common list applies." }), picker, note);
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
      const same = approve && alreadyAllowed(r.body, req.workspace_id);
      const x = await d.api("POST", "/admin/addon-requests", { workspace_id: req.workspace_id, name: req.name, approve });
      d.state.notice = x.status === 200 ? d.banner("ok", same ? "Already allowed" : approve ? "Allowed" : "Turned down", same ? "Nothing needed changing. The owner can choose to install it in the workspace." : approve ? "The owner can now choose to install it in the workspace." : null) : d.banner("bad", "Not changed", d.say(x.body.error));
      await reload();
    };
    d.shell("addons", d.state.notice, addonsView({ ...r.body, workspaces: withNames(r.body.workspaces, d.state.rail?.rows ?? []), requests: (r.body.requests ?? []).map((q) => ({ ...q, workspace_name: withNames([{ workspace_id: q.workspace_id, name: q.workspace_name }], d.state.rail?.rows ?? [])[0].name })) }, { save, decide }));
    d.state.notice = null;
  } finally { finish(); }
}

// The registry entry: the console's router, tabs and file list are built from this one export (no other file names a page).
export const page = { path: "addons", nav: { label: "Add-ons", admin: true, after: "services" }, render: addonsPage };
