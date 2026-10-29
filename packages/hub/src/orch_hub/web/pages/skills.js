// The Company skills page (M3, #1430): instructions a hub admin offers to workspaces. Offering is not enabling: a
// workspace's owner reads the full text and turns a skill on at their own host. The hub never reaches an agent from here.
import { emptyState, loading } from "../feel.js";
import { h, text } from "../ui.js";

const info = (label, body) => h("details", { class: "info" }, h("summary", { "aria-label": label, text: "i" }), h("p", { class: "small muted", text: body }));
const wsName = (w) => text(w?.name || (w?.workspace_id ? "Workspace " + w.workspace_id.slice(0, 8) : "A workspace"));
const NAME = /^[a-z][a-z0-9-]{1,39}$/;

// Mirrors the hub's own check so a typo is caught before sending; the hub decides in the end.
export function skillProblem(name, body, max = 2400) {
  if (!NAME.test(name)) return "Use a short name of lower-case letters, numbers and dashes.";
  if (name.startsWith("orch")) return "Names starting with orch belong to orch's own skills.";
  if (!body.trim()) return "Write what the skill should say.";
  if (body.length > max) return "Keep it under " + max + " characters.";
  if (body.trimStart().startsWith("---")) return "Start with plain text, not a header block.";
  return null;
}

export function skillsView(data, actions = {}) {
  const workspaces = data.workspaces ?? [];
  const max = data.max_chars ?? 2400;
  const card = (s) => {
    const area = h("textarea", { rows: 6, "aria-label": "Text of " + s.name, maxlength: String(max) });
    area.value = s.text;
    const note = h("p", { class: "small", role: "status" });
    const boxes = workspaces.map((w) => {
      const box = h("input", { type: "checkbox", "data-ws": w.workspace_id });
      box.checked = (s.offered_to ?? []).includes(w.workspace_id);
      return { w, box };
    });
    let sure = false;
    const remove = h("button", { type: "button", text: "Remove", onclick: () => {
      if (!sure) { sure = true; remove.textContent = "Sure? Remove it"; return; }
      actions.remove?.(s);
    } });
    return h("article", { class: "card skill", "data-skill": s.name },
      h("div", { class: "row" }, h("h3", { class: "grow", text: s.name }), remove),
      area, note,
      h("div", { class: "row" }, h("button", { type: "button", class: "primary", text: "Save text", onclick: () => {
        const bad = skillProblem(s.name, area.value, max);
        if (bad) { note.textContent = bad; return; }
        actions.save?.(s.name, area.value);
      } }), h("span", { class: "small muted", text: "Saving changes the skill, so each workspace owner has to accept it again." })),
      h("h4", { text: "Offer to" }),
      workspaces.length ? boxes.map(({ w, box }) => h("label", { class: "list-row row" }, box, h("span", { class: "grow", text: wsName(w) }))) : h("p", { class: "small muted", text: "No workspaces are connected yet." }),
      workspaces.length ? h("button", { type: "button", text: "Save who gets it", onclick: () => actions.offer?.(s.name, boxes.filter((x) => x.box.checked).map((x) => x.w.workspace_id)) }) : null);
  };
  const nameIn = h("input", { type: "text", "aria-label": "Name of the new skill", placeholder: "for example: how-we-write-invoices", maxlength: "40" });
  const bodyIn = h("textarea", { rows: 5, "aria-label": "Text of the new skill", maxlength: String(max) });
  const err = h("p", { class: "small", role: "alert" });
  const add = h("button", { type: "button", class: "primary", text: "Add skill", onclick: () => {
    const bad = skillProblem(nameIn.value ?? "", bodyIn.value ?? "", max);
    if (bad) { err.textContent = bad; return; }
    actions.save?.(nameIn.value, bodyIn.value);
  } });
  return h("div", { class: "skills" },
    h("div", { class: "row" }, h("h1", { class: "grow", text: "Company skills" }),
      info("About company skills", "A skill is advice your agents read, such as how your company writes invoices. Each workspace owner reads it and turns it on. Skills stay below orch's own rules and cannot widen what an agent may do.")),
    h("p", { class: "banner warn", role: "note", text: "Nothing is switched on for anyone until their workspace owner accepts it." }),
    (data.skills ?? []).length ? (data.skills ?? []).map(card) : emptyState("skills"),
    h("section", { class: "card" }, h("h3", { text: "New skill" }), nameIn, bodyIn, err, add));
}

export async function skillsPage(d) {
  const finish = loading();
  d.pageLoading("skills");
  try {
    if (!(await d.refresh())) return d.loginView();
    if (!d.state.user.is_admin) return d.home();
    if (d.state.user.needs_sign_in) return d.reauthView();
    await d.loadRail();
    const r = await d.api("GET", "/admin/skills");
    if (r.status === 403 && r.body.error === "reauth_required") return d.reauthView();
    if (r.status !== 200) return d.shell("skills", h("h1", { text: "Company skills" }), h("p", { role: "alert", text: "Cannot read the company skills. " + d.say(r.body.error) }));
    const say = (ok, x) => { d.state.notice = x.status === 200 ? d.banner("ok", ok, null) : d.banner("bad", "Not changed", d.say(x.body.error ?? "unavailable")); };
    const save = async (name, body) => { say("Saved", await d.api("PUT", "/admin/skills/" + name, { text: body })); await skillsPage(d); };
    const remove = async (s) => { say("Removed", await d.api("DELETE", "/admin/skills/" + s.name)); await skillsPage(d); };
    const offer = async (name, workspaces) => { say("Saved", await d.api("PUT", "/admin/skills/" + name + "/offers", { workspaces })); await skillsPage(d); };
    d.shell("skills", d.state.notice, skillsView(r.body, { save, remove, offer }));
    d.state.notice = null;
  } finally { finish(); }
}

// The registry entry: the console's router, tabs and file list are built from this one export (no other file names a page).
export const page = { path: "skills", nav: { label: "Skills", admin: true, after: "addons" }, render: skillsPage };
