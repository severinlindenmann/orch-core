// The Shared spaces page (M3, #1443 #1444 #1445 #1447): spaces the hub hosts, who may only read, archive, create one, add or
// remove a workspace, and read a space's history. The page signs nothing, puts nothing and restores nothing: the hub does
// what its service already did. Everything that came out of a space (names, text, who wrote it) is printed with textContent
// only; no link is ever made from it.
import { emptyState, loading } from "../feel.js";
import { chip, h, text, when, withNames } from "../ui.js";

export const STATEMENT = "The hub and its admins can read this space.";
export const NO_START = "A space needs a workspace that shares everything with the hub.";
export const LATER = "Can join later, once its owner accepts";
export const RESTORE_LINE = "To get an older version back, restore it from the workspace.";
const TYPES = [["wiki", "Wiki", "Pages people write together"], ["files", "Files", "Documents everyone starts from"], ["board", "Board", "Cards that move from planned to done"]];
export const HINT_PICK = "Tick a workspace that joins now.";
export const HINT_TICK = "Tick \u201cI understand\u201d to create the space.";
const typeName = (t) => (TYPES.find((x) => x[0] === t) ?? [t, ""])[1];

const info = (label, body) => h("details", { class: "info" }, h("summary", { "aria-label": label, text: "i" }), h("p", { class: "small muted", text: body }));
const nameOf = (w) => text(w?.name || "A workspace");
const kb = (n) => Math.max(1, Math.round((Number(n) || 0) / 1024)) + " KB";

/** The badge says who the WORKSPACE says wrote a version: a fixed table, never an id and never what the item says. */
export const BY_LABEL = { person: "person", agent: "agent" };
const byLabel = (by) => BY_LABEL[by] ?? "unknown";

/** One space's history, read only: names, versions newest first, removed ones. Item text is data, shown as plain text. */
export function historyView(data, now = Date.now()) {
  const names = data?.names ?? [], removed = data?.removed ?? [];
  const version = (v) => h("div", { class: "list-row" },
    h("div", { class: "row" },
      h("span", { class: "small muted grow", text: when(v.created_ms, now) + (v.author ? " · " + text(v.author) : "") }),
      h("span", { class: "chip by-" + byLabel(v.by), title: "As the workspace says", text: byLabel(v.by) }),
      v.restored ? chip("restored") : null),
    v.content?.text != null
      ? h("pre", { class: "space-text", text: text(v.content.text) + (v.content.cut ? "\n(cut here)" : "") })
      : h("p", { class: "small muted", text: "file (" + kb(v.content?.file) + ")" }));
  return h("div", { class: "space-history" },
    names.length || removed.length ? null : h("p", { class: "small muted", text: "Nothing has been put in this space yet." }),
    names.map((n) => h("section", {}, h("h4", { text: text(n.name) }), (n.versions ?? []).map(version))),
    removed.length ? h("section", {}, h("h4", { text: "Removed" }),
      removed.map((r) => h("div", { class: "list-row row" }, chip("removed"), h("span", { class: "small muted", text: r.author ? "put by " + text(r.author) : "" })))) : null,
    names.length || removed.length ? h("p", { class: "small muted", text: "person / agent is what the workspace says about who made the change; the hub cannot check it." }) : null,
    h("p", { class: "small", text: RESTORE_LINE }));
}

/** The 3-step "New shared space" dialog. `done(form)` gets {type, name, purpose, members, understood: true}. */
export function newSpaceDialog(workspaces, done, close) {
  // A kind is always chosen (Wiki, the most common), and the first workspace that joins now is already ticked.
  const first = workspaces.find((w) => w.mode === "full");
  const form = { type: TYPES[0][0], name: "", purpose: "", members: new Set(first ? [first.workspace_id] : []), understood: false };
  const body = h("div", { class: "card", role: "dialog", "aria-label": "New shared space" });
  let step = 1;
  const nameIn = h("input", { type: "text", maxlength: "64", "aria-label": "Name" });
  const purposeIn = h("input", { type: "text", maxlength: "200", "aria-label": "What it is for" });
  const tick = h("input", { type: "checkbox", id: "space-understood", "aria-label": "I understand" });
  const create = h("button", { type: "button", class: "pri", text: "Create" });
  const next = h("button", { type: "button", class: "pri", text: "Next" });
  // The hub adds only 'full' workspaces now; a 'selective' one waits for its owner and cannot be ticked.
  const joinsNow = () => workspaces.some((w) => w.mode === "full" && form.members.has(w.workspace_id));
  const hint = h("p", { class: "small muted space-hint", id: "space-hint", role: "note" });
  const ready = () => {
    create.disabled = !(form.understood === true && joinsNow());
    // What Create is waiting for, in one line.
    hint.textContent = !create.disabled ? "" : joinsNow() ? HINT_TICK : HINT_PICK;
    hint.hidden = !hint.textContent;
    if (create.disabled) create.setAttribute("aria-describedby", "space-hint");
    else create.removeAttribute("aria-describedby");
  };
  tick.addEventListener("change", () => { form.understood = tick.checked === true; ready(); });
  nameIn.addEventListener("input", () => { form.name = nameIn.value; });
  purposeIn.addEventListener("input", () => { form.purpose = purposeIn.value; });
  const back = () => { step = Math.max(1, step - 1); draw(); };
  next.addEventListener("click", () => {
    if (step === 1 && !form.type) return;
    if (step === 2) { form.name = nameIn.value ?? ""; form.purpose = purposeIn.value ?? ""; if (!form.name.trim()) return; }
    step += 1; draw();
  });
  create.addEventListener("click", () => {
    if (form.understood !== true || !joinsNow()) return; // the tick is required, here and at the hub
    done({ type: form.type, name: form.name.trim(), purpose: form.purpose.trim(), members: [...form.members], understood: true });
  });
  const cancel = h("button", { type: "button", text: "Cancel", onclick: close });
  function draw() {
    const steps = {
      1: [h("p", { text: "What kind of space is it?" }), ...TYPES.map(([t, label, hint]) => {
        const b = h("button", { type: "button", class: "space-type" + (form.type === t ? " on" : ""), "data-type": t, "aria-pressed": String(form.type === t) },
          h("b", { text: label }), h("span", { class: "small muted", text: " " + hint }));
        b.addEventListener("click", () => { form.type = t; draw(); });
        return h("div", { class: "list-row" }, b, t === TYPES[0][0] ? h("span", { class: "space-tag", text: "Most common" }) : null);
      })],
      2: [h("label", {}, "Name", nameIn), h("label", {}, "What is it for? (optional)", purposeIn)],
      3: [h("p", { text: "Which workspaces use it?" }),
        ...(workspaces.length ? workspaces.map((w) => {
          const joins = w.mode === "full";
          const box = h("input", { type: "checkbox", "aria-label": nameOf(w) });
          if (joins) {
            box.checked = form.members.has(w.workspace_id);
            box.addEventListener("change", () => { box.checked ? form.members.add(w.workspace_id) : form.members.delete(w.workspace_id); ready(); });
          } else {
            box.disabled = true;
          }
          return h("label", { class: "check" + (joins ? "" : " disabled") }, box, h("span", { class: "grow", text: nameOf(w) }),
            h("span", { class: "small muted", text: joins ? "joins now" : LATER }));
        }) : [h("p", { class: "small muted", text: "No workspace is connected yet." })]),
        h("label", { class: "check" }, tick, h("span", { text: "I understand: " + STATEMENT }))],
    };
    body.replaceChildren(h("h1", { text: "New shared space (step " + step + " of 3)" }), ...steps[step],
      h("div", { class: "row" }, cancel, step > 1 ? h("button", { type: "button", text: "Back", onclick: back }) : null, step < 3 ? next : create),
      step === 3 ? hint : null);
    if (step === 3) ready();
  }
  draw();
  return h("div", { class: "dialog" }, body);
}

export function spacesView(data, actions = {}, now = Date.now()) {
  const all = data.spaces ?? [];
  const about = data.about ?? {};
  const candidates = (data.workspaces ?? []).filter((w) => w.mode != null && w.status !== "unbound");
  const canStart = candidates.some((w) => w.mode === "full");
  const archived = all.filter((s) => s.archived_ms != null);
  let showArchived = false;
  const list = h("div", { id: "spaces-list" });
  const dialogHost = h("div", { id: "space-dialog" });
  const toggle = archived.length ? h("button", { type: "button", id: "toggle-archived", text: "Show archived (" + archived.length + ")" }) : null;
  const draw = () => {
    const shown = all.filter((s) => showArchived || s.archived_ms == null);
    list.replaceChildren(...(shown.length ? shown.map(card) : [emptyState("spaces")]));
    if (toggle) { toggle.textContent = (showArchived ? "Hide archived (" : "Show archived (") + archived.length + ")"; }
  };
  const card = (s) => {
    let sure = false, sureRemove = null;
    const meta = about[s.space_id] ?? {};
    const archive = h("button", { type: "button", text: s.archived_ms == null ? "Archive" : "Bring back", onclick: () => {
      if (s.archived_ms == null && !sure) { sure = true; archive.textContent = "Sure? Archive it"; return; }
      actions.archive?.(s, s.archived_ms == null);
    } });
    const panel = h("div", { class: "space-panel" });
    const historyBtn = h("button", { type: "button", class: "space-history-btn", text: "History" });
    let open = false;
    historyBtn.addEventListener("click", async () => {
      open = !open;
      if (!open) { panel.replaceChildren(); historyBtn.textContent = "History"; return; }
      historyBtn.textContent = "Hide history";
      panel.replaceChildren(h("p", { class: "small muted", text: "Loading" }));
      const got = actions.history ? await actions.history(s) : null;
      panel.replaceChildren(got ? historyView(got, now) : h("p", { role: "alert", text: "The history could not be read." }));
    });
    const memberIds = new Set((s.members ?? []).map((m) => m.workspace_id));
    const free = candidates.filter((w) => !memberIds.has(w.workspace_id));
    const pick = h("select", { "aria-label": "Add a workspace" }, h("option", { value: "", text: "Add a workspace" }),
      free.map((w) => h("option", { value: w.workspace_id, text: nameOf(w) + (w.mode === "selective" ? " (shares only selected items; its owner accepts first)" : "") })));
    const add = h("button", { type: "button", text: "Add", onclick: () => { if (pick.value) actions.addMember?.(s, pick.value); } });
    return h("article", { class: "card space", "data-space": s.space_id },
      h("div", { class: "row" }, h("h3", { class: "grow", text: text(s.name) || "Space" }), meta.type ? chip(typeName(meta.type)) : null, s.archived_ms != null ? chip("Archived") : null,
        h("span", { class: "small muted", text: s.items + (s.items === 1 ? " item" : " items") }), historyBtn, archive),
      meta.purpose ? h("p", { class: "small muted", text: text(meta.purpose) }) : null,
      s.archived_ms != null ? h("p", { class: "small muted", text: "Archived " + when(s.archived_ms, now) + ". Nobody can add to it; it can still be read." }) : null,
      (s.members ?? []).map((m) => {
        const select = h("select", { "aria-label": "What " + nameOf(m) + " may do" },
          h("option", { value: "edits", text: "Reads and edits" }), h("option", { value: "reads", text: "Reads only" }));
        select.value = m.mode;
        select.addEventListener("change", () => actions.setMode?.(s, m, select.value));
        const remove = h("button", { type: "button", class: "space-remove", text: "Remove" });
        remove.addEventListener("click", () => {
          if (sureRemove !== m.workspace_id) { sureRemove = m.workspace_id; remove.textContent = "Sure? Remove"; return; }
          actions.removeMember?.(s, m);
        });
        return h("div", { class: "list-row row" }, h("b", { class: "grow", text: nameOf(m) }), select, remove);
      }),
      s.archived_ms == null && free.length ? h("div", { class: "list-row row" }, pick, add) : null,
      panel);
  };
  const open = () => {
    if (!canStart) return;
    const close = () => dialogHost.replaceChildren();
    dialogHost.replaceChildren(newSpaceDialog(candidates, async (form) => { close(); await actions.create?.(form); }, close));
  };
  const newSpace = h("button", { type: "button", id: "new-space", class: "pri", text: "New shared space", onclick: open });
  newSpace.disabled = !canStart;
  const noStart = !canStart && candidates.length ? h("div", { class: "banner warn", id: "space-no-start", role: "note" },
    h("b", { text: "No space can be started yet." }),
    h("p", { text: NO_START }),
    candidates.map((w) => h("p", { text: nameOf(w) + " shares only what you choose." })),
    h("details", {},
      h("summary", { text: "Show me how" }),
      h("ol", {},
        h("li", { text: "Open " + candidates.map(nameOf).join(" or ") + " on its own computer." }),
        h("li", { text: "Go to Settings > Hub." }),
        h("li", {}, "Choose ", h("b", { text: "Share everything" }), ". Then come back here.")))) : null;
  if (noStart) newSpace.setAttribute("aria-describedby", "space-no-start");
  const root = h("div", { class: "spaces" },
    h("div", { class: "row" }, h("h1", { class: "grow", text: "Shared spaces" }),
      newSpace,
      info("About shared spaces", "Workspaces put documents into a shared space and read each other's. A workspace set to Reads only can read but not add or remove. Archiving stops new additions; reading goes on.")),
    noStart,
    all.length ? h("p", { class: "banner warn", role: "note", text: STATEMENT }) : null,
    toggle, list, dialogHost);
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
    const [r, about, ws] = await Promise.all([d.api("GET", "/admin/spaces"), d.api("GET", "/admin/spaces/about"), d.api("GET", "/admin/workspaces")]);
    if (r.status === 403 && r.body.error === "reauth_required") return d.reauthView();
    if (r.status !== 200) return d.shell("spaces", h("h1", { text: "Shared spaces" }), h("p", { role: "alert", text: "Cannot read the shared spaces. " + d.say(r.body.error) }));
    const rows = d.state.rail?.rows ?? [];
    const say = (ok, bad, x) => { d.state.notice = x.status === 200 ? d.banner("ok", ok, null) : d.banner("bad", "Not changed", d.say(x.body.error ?? bad)); };
    const wait = (x) => (x.status === 200 && x.body.waiting?.length ? " Waiting for the owner of " + x.body.waiting.map((w) => text(w.name || "a workspace")).join(", ") + " to accept." : "");
    const setMode = async (s, m, mode) => { say("Saved", "unavailable", await d.api("PUT", "/admin/spaces/" + s.space_id + "/members/" + m.workspace_id + "/mode", { mode })); await spacesPage(d); };
    const archive = async (s, archived) => { say(archived ? "Archived" : "Brought back", "unavailable", await d.api("POST", "/admin/spaces/" + s.space_id + "/archive", { archived })); await spacesPage(d); };
    const create = async (form) => { const x = await d.api("POST", "/admin/spaces", form); say("Created." + wait(x), "unavailable", x); await spacesPage(d); };
    const addMember = async (s, id) => { const x = await d.api("POST", "/admin/spaces/" + s.space_id + "/members", { workspace_id: id }); say(x.body?.added === false ? "Waiting for its owner to accept." : "Added", "unavailable", x); await spacesPage(d); };
    const removeMember = async (s, m) => { say("Removed", "unavailable", await d.api("POST", "/admin/spaces/" + s.space_id + "/members/remove", { workspace_id: m.workspace_id })); await spacesPage(d); };
    const history = async (s) => { const x = await d.api("GET", "/admin/spaces/" + s.space_id + "/history"); return x.status === 200 ? x.body : null; };
    d.shell("spaces", d.state.notice, spacesView({
      ...r.body,
      about: about.status === 200 ? about.body.about : {},
      workspaces: ws.status === 200 ? withNames(ws.body.workspaces, rows) : [],
      spaces: (r.body.spaces ?? []).map((s) => ({ ...s, members: withNames(s.members, rows) })),
    }, { setMode, archive, create, addMember, removeMember, history }));
    d.state.notice = null;
  } finally { finish(); }
}

// The registry entry: the console's router, tabs and file list are built from this one export (no other file names a page).
export const page = { path: "spaces", nav: { group: "Sharing & apps", label: "Shared folders", admin: true, needsWorkspace: true, after: "mcp" }, render: spacesPage };
