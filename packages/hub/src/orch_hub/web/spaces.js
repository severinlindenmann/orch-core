// The Shared spaces page (M3, #1443 #1444 #1447): spaces the hub hosts, who may only read, archive. The page signs nothing:
// the hub does what its service already did; creating a space and its history come in a later slice.
import { emptyState, loading } from "./feel.js";
import { chip, h, text, when } from "./ui.js";

const info = (label, body) => h("details", { class: "info" }, h("summary", { "aria-label": label, text: "i" }), h("p", { class: "small muted", text: body }));
const nameOf = (w) => text(w?.name || (w?.workspace_id ? "Workspace " + w.workspace_id.slice(0, 8) : "A workspace"));

export function spacesView(data, actions = {}, now = Date.now()) {
  const all = data.spaces ?? [];
  const archived = all.filter((s) => s.archived_ms != null);
  let showArchived = false;
  const list = h("div", { id: "spaces-list" });
  const toggle = archived.length ? h("button", { type: "button", id: "toggle-archived", text: "Show archived (" + archived.length + ")" }) : null;
  const draw = () => {
    const shown = all.filter((s) => showArchived || s.archived_ms == null);
    list.replaceChildren(...(shown.length ? shown.map(card) : [emptyState("spaces")]));
    if (toggle) { toggle.textContent = (showArchived ? "Hide archived (" : "Show archived (") + archived.length + ")"; }
  };
  const card = (s) => {
    let sure = false;
    const archive = h("button", { type: "button", text: s.archived_ms == null ? "Archive" : "Bring back", onclick: () => {
      if (s.archived_ms == null && !sure) { sure = true; archive.textContent = "Sure? Archive it"; return; }
      actions.archive?.(s, s.archived_ms == null);
    } });
    return h("article", { class: "card space", "data-space": s.space_id },
      h("div", { class: "row" }, h("h3", { class: "grow", text: text(s.name) || "Space" }), s.archived_ms != null ? chip("Archived") : null,
        h("span", { class: "small muted", text: s.items + (s.items === 1 ? " item" : " items") }), archive),
      s.archived_ms != null ? h("p", { class: "small muted", text: "Archived " + when(s.archived_ms, now) + ". Nobody can add to it; it can still be read." }) : null,
      (s.members ?? []).map((m) => {
        const select = h("select", { "aria-label": "What " + nameOf(m) + " may do" },
          h("option", { value: "edits", text: "Reads and edits" }), h("option", { value: "reads", text: "Reads only" }));
        select.value = m.mode;
        select.addEventListener("change", () => actions.setMode?.(s, m, select.value));
        return h("div", { class: "list-row row" }, h("b", { class: "grow", text: nameOf(m) }), select);
      }));
  };
  const root = h("div", { class: "spaces" },
    h("div", { class: "row" }, h("h1", { class: "grow", text: "Shared spaces" }),
      info("About shared spaces", "Workspaces put documents into a shared space and read each other's. A workspace set to Reads only can read but not add or remove. Archiving stops new additions; reading goes on.")),
    h("p", { class: "banner warn", role: "note", text: "The hub and its admins can read what is in a shared space." }),
    toggle, list);
  if (toggle) toggle.addEventListener("click", () => { showArchived = !showArchived; draw(); });
  draw();
  return root;
}

export async function spacesPage(d) {
  const finish = loading();
  d.pageLoading("spaces");
  try {
    if (!(await d.refresh())) return d.loginView();
    if (!d.state.user.is_admin) return d.home();
    if (d.state.user.needs_sign_in) return d.reauthView();
    await d.loadRail();
    const r = await d.api("GET", "/admin/spaces");
    if (r.status === 403 && r.body.error === "reauth_required") return d.reauthView();
    if (r.status !== 200) return d.shell("spaces", h("h1", { text: "Shared spaces" }), h("p", { role: "alert", text: "Cannot read the shared spaces. " + d.say(r.body.error) }));
    const say = (ok, bad, x) => { d.state.notice = x.status === 200 ? d.banner("ok", ok, null) : d.banner("bad", "Not changed", d.say(x.body.error ?? bad)); };
    const setMode = async (s, m, mode) => { say("Saved", "unavailable", await d.api("PUT", "/admin/spaces/" + s.space_id + "/members/" + m.workspace_id + "/mode", { mode })); await spacesPage(d); };
    const archive = async (s, archived) => { say(archived ? "Archived" : "Brought back", "unavailable", await d.api("POST", "/admin/spaces/" + s.space_id + "/archive", { archived })); await spacesPage(d); };
    d.shell("spaces", d.state.notice, spacesView(r.body, { setMode, archive }));
    d.state.notice = null;
  } finally { finish(); }
}
