import { emptyState } from "./feel.js";
// Connected workspaces and their availability.
import { ago, chip, h, text } from "./ui.js";

const nameOf = (end) => (end && end.name ? text(end.name) : end && end.owner_name ? text(end.owner_name + "'s workspace") : "Another workspace");

const modeName = (w) => w.mode === "full" ? "Everything" : "Only what I choose";
const personName = (p) => text(typeof p === "string" ? p : p?.name ?? p?.alias ?? p?.user_id ?? "unknown");
const button = (label, onclick, props = {}) => h("button", { type: "button", text: label, onclick, ...props });
const small = (value) => h("div", { class: "small muted", text: value });
const statusView = (w, now) => h("div", {}, chip(w.online ? "Online" : "Offline", w.online ? "ok" : ""),
  !w.online && Number.isFinite(w.last_seen_ms) ? small("since " + ago(w.last_seen_ms, now)) : null);
const hostView = (w) => h("div", {}, text(w.host ?? "unknown"), small(text(w.address ?? "unknown")));
const versionView = (w, hub) => h("div", {},
  h("div", { text: text(w.version ?? "unknown") + (w.commit ? " · " + text(w.commit.slice(0, 7)) : "") }),
  w.version_state === "unsupported" ? chip("not supported any more", "warn") :
    w.version_state === "older" && hub?.version ? chip("update to " + text(hub.version), "warn") :
      w.version_state === "newest" ? chip("newest", "ok") : null);

/** Pure details view; roles is null when the caller cannot read access. */
export function workspaceDetailsView(w, { roles = null, owner = null, hub, actions = {} } = {}, now = Date.now()) {
  return [h("h2", { text: nameOf(w) }),
    h("dl", { class: "facts" },
      h("dt", { text: "Status" }), h("dd", {}, statusView(w, now)),
      h("dt", { text: "Owner" }), h("dd", { text: personName(roles?.owner ?? owner ?? w.owner_name) }),
      h("dt", { text: "Runs on" }), h("dd", {}, hostView(w)),
      h("dt", { text: "orch version" }), h("dd", {}, versionView(w, hub)),
      h("dt", { text: "Mode" }), h("dd", {}, chip(modeName(w)), small(w.mode === "full"
        ? "The hub can read everything and help across workspaces."
        : "The hub sees only what this workspace chooses to share."))),
    roles ? h("section", {}, h("h3", { text: "Who has access" }),
      h("p", { text: "Owner: " + personName(roles.owner) }),
      roles.groups?.length ? h("ul", {}, roles.groups.map((g) => h("li", {},
        text(g.group) + " · " + text(g.role), small((g.members ?? []).map(personName).join(", "))))) : small("Only the owner.")) : null,
    roles?.profile ? h("section", {}, h("h3", { text: "Profile (untrusted display data)" }),
      h("dl", {}, Object.entries(roles.profile).flatMap(([key, value]) => [
        h("dt", { text: key }), h("dd", { text: JSON.stringify(value) })]))) : null,
    h("h3", { text: "Change mode" }),
    h("p", { text: "To change what this workspace shares, open it and use Settings > Hub. The owner confirms there." }),
    button("Open " + nameOf(w), () => actions.open?.(w), { class: "pri" })].filter(Boolean);
}

/** The link is copied unchanged; after Copy the page says what to do next. */
export function connectWorkspaceView(link, actions = {}, { people = [], selected = null } = {}) {
  const next = h("p", { class: "small muted", "aria-live": "polite" });
  const NEXT = "Now open your workspace, go to Settings › Hub › Connect a hub, and paste.";
  const copy = button("Copy", async () => {
    let ok = false;
    try { ok = !!(await actions.copy?.(link.link)); } catch { ok = false; }
    copy.textContent = ok ? "Copied ✓" : "Could not copy. Select the link to copy it.";
    next.textContent = ok ? NEXT : "";
  });
  const clock = (ms) => { const d = new Date(ms); return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0"); };
  const input = h("input", { class: "grow", readonly: true, value: link.link, "aria-label": "Connect link" });
  const until = h("p", { text: "Works once, for 30 minutes (until " + clock(link.expires_ms) + ")" });
  const problem = h("p", { role: "alert" });
  // Only people the hub knows are offered, and the hub rejects anyone else (the workspace host never picks its owner).
  const choices2 = people.filter((p) => p && typeof p.user_id === "string");
  const who = choices2.length ? h("div", {},
    h("label", { for: "ws-owner", text: "Whose workspace is this?" }),
    h("select", { id: "ws-owner", onchange: async (e) => {
      problem.textContent = "";
      const select = e.target;
      select.disabled = true;
      try {
        const next = await actions.connect?.(select.value);
        if (next) { input.value = next.link; until.textContent = "Works once, for 30 minutes (until " + clock(next.expires_ms) + ")"; }
      } catch { problem.textContent = "Could not create a link for this person. Please try again."; }
      finally { select.disabled = false; }
    } }, choices2.map((p) => h("option", { value: p.user_id, selected: p.user_id === selected ? true : null, text: text(p.name ?? "Unnamed") }))),
    small("You open the hub from it as this person.")) : null;
  return h("section", { class: "card ws-connect", "aria-label": "Connect a workspace" },
    h("h2", { text: "Connect a workspace" }),
    who,
    h("div", { class: "row" }, input, copy),
    next, until, problem);
}

/** Workspaces in `rows` whose id is not in `known` (a Set of ids). */
export const newWorkspaces = (known, rows) => (rows ?? []).filter((w) => w.workspace_id && !known.has(w.workspace_id));

/** The green message that replaces the used link box once a workspace has joined. */
export function joinedBannerView(row, actions = {}) {
  return h("div", { class: "banner ok ws-joined", role: "status", "data-joined": row.workspace_id },
    h("div", { class: "row" },
      h("div", { class: "grow" }, h("b", { text: "✓ " + nameOf(row) + " joined" }), small("It is online now. You can open it from here.")),
      button("Open " + nameOf(row), () => actions.open?.(row), { class: "pri" })));
}

/** data: {workspaces, handoffs, admin: rows|null, hub, actions, joined?: rows that joined while the page was open}. */
export function workspacesView(data, now = Date.now()) {
  const actions = data.actions ?? {};
  const connectBox = h("div");
  const joined = data.joined ?? [];
  for (const row of joined) connectBox.append(joinedBannerView(row, actions));
  const connect = button("Connect a workspace", async () => {
    connect.disabled = true;
    connectBox.textContent = "";
    try {
      const people = (await actions.people?.()) ?? [];
      const mine = people.find((x) => x.user_id === data.self)?.user_id ?? people[0]?.user_id ?? null;
      connectBox.append(connectWorkspaceView(await actions.connect(mine), actions, { people, selected: mine }));
    }
    catch { connectBox.append(h("p", { role: "alert", text: "Could not create a link. Please try again." })); }
    finally { connect.disabled = false; }
  }, { class: "pri" });
  const waitingFor = new Map();
  for (const r of data.handoffs) if (r.status === "waiting" && r.to.workspace_id) waitingFor.set(r.to.workspace_id, (waitingFor.get(r.to.workspace_id) ?? 0) + 1);
  const list = data.workspaces.length ? h("div", { class: "scroll" }, h("table", { class: "tbl ws-table" },
    h("thead", {}, h("tr", {}, ["Workspace", "Status", "orch version", "Runs on", "Actions"].map((label) => h("th", { scope: "col", text: label })))),
    h("tbody", {}, data.workspaces.map((w) => {
      const n = waitingFor.get(w.workspace_id) ?? 0;
      return h("tr", { "data-workspace": w.workspace_id, class: joined.some((j) => j.workspace_id === w.workspace_id) ? "row-new" : "" },
        h("td", {}, h("b", { text: nameOf(w) }), h("div", {}, chip(modeName(w), w.mode === "full" ? "full" : "sel")),
          w.owner_name ? small("Owner: " + text(w.owner_name)) : null,
          w.mode === "selective" ? small("Personal: only its owner") : null,
          n ? small(n + (n === 1 ? " hand-off is" : " hand-offs are") + " waiting for this workspace") : null),
        h("td", {}, statusView(w, now)), h("td", {}, versionView(w, data.hub)), h("td", {}, hostView(w)),
        h("td", {}, h("div", { class: "ws-actions" }, button("Details", () => actions.details?.(w)), button("Open", () => actions.open?.(w)))));
    })))) : emptyState(data.admin != null ? "workspaces" : "workspaces-member");
  const older = data.workspaces.filter((w) => ["older", "unsupported"].includes(w.version_state));
  const left = (data.admin ?? []).filter((w) => w.status === "unbound");
  const leftView = data.admin != null ? h("details", {}, h("summary", { text: "Show left workspaces (" + left.length + ")" }),
    ...left.map((w) => {
      const error = h("p", { role: "alert" });
      const remove = button("Remove", async () => {
        remove.disabled = true;
        error.textContent = "";
        try { await actions.remove(w); }
        catch { error.textContent = "Could not remove this workspace. Please try again."; }
        finally { remove.disabled = false; }
      }, { class: "danger" });
      return h("article", { class: "list-row" }, h("div", { class: "row" }, h("b", { class: "grow", text: nameOf(w) }), chip("Left"), remove),
        small(Number.isFinite(w.left_ms) ? "Left " + new Date(w.left_ms).toLocaleDateString() : "Left on an unknown date"), error);
    })) : null;
  return [
    h("div", { class: "row" }, h("h1", { class: "grow", text: "Workspaces" }), data.admin != null ? connect : null),
    connectBox,
    older.length ? h("div", { class: "banner warn" }, h("b", { text: older.length + " workspaces run an older orch" }),
      small(older.map(nameOf).join(", ")), data.hub?.min_supported ? small("The hub still works with " + text(data.hub.min_supported) + " and newer.") : null) : null,
    h("section", { class: "card", "aria-labelledby": "ws-h" }, h("h2", { id: "ws-h", text: "Connected workspaces" }), list, leftView),
  ].filter(Boolean);
}
