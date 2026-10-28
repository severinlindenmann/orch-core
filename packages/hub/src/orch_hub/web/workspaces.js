import { emptyState } from "./feel.js";
// Connected workspaces and their availability.
import { ago, chip, h, text } from "./ui.js";

const nameOf = (end) => (end && end.name ? text(end.name) : end && end.workspace_id ? "Workspace " + end.workspace_id.slice(0, 8) : "Another workspace");

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
      h("dt", { text: "Owner" }), h("dd", { text: personName(roles?.owner ?? owner) }),
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

/** The suggestion is text only: the link is copied unchanged. */
export function connectWorkspaceView(link, actions = {}) {
  const suggestion = h("p", { class: "small muted", "aria-live": "polite" });
  const choices = ["Only what I choose", "Everything"].map((mode) => button(mode, () => select(mode)));
  function select(mode) {
    suggestion.textContent = "Suggested: " + mode + ". The workspace owner confirms the mode with one click in Mission Control.";
    choices.forEach((b) => b.setAttribute("aria-pressed", String(b.textContent === mode)));
  }
  select("Only what I choose");
  const copy = button("Copy", async () => {
    try { copy.textContent = await actions.copy?.(link.link) ? "Copied" : "Could not copy. Select the link to copy it."; }
    catch { copy.textContent = "Could not copy. Select the link to copy it."; }
  });
  const expires = new Date(link.expires_ms);
  const time = String(expires.getHours()).padStart(2, "0") + ":" + String(expires.getMinutes()).padStart(2, "0");
  return h("section", { class: "card ws-connect", "aria-label": "Connect a workspace" },
    h("h2", { text: "Connect a workspace" }),
    h("p", { text: "Open this link in the workspace’s Mission Control, under Settings > Hub > Connect." }),
    h("div", { class: "row" }, h("input", { class: "grow", readonly: true, value: link.link, "aria-label": "Connect link" }), copy),
    h("p", { text: "Works once, for 30 minutes (until " + time + ")" }),
    h("div", { class: "ws-segment", role: "group", "aria-label": "Suggested mode" }, small("Suggested mode"), ...choices), suggestion);
}

/** data: {workspaces, handoffs, admin: rows|null, hub, actions}. */
export function workspacesView(data, now = Date.now()) {
  const actions = data.actions ?? {};
  const connectBox = h("div");
  const connect = button("Connect a workspace", async () => {
    connect.disabled = true;
    connectBox.textContent = "";
    try { connectBox.append(connectWorkspaceView(await actions.connect(), actions)); }
    catch { connectBox.append(h("p", { role: "alert", text: "Could not create a link. Please try again." })); }
    finally { connect.disabled = false; }
  }, { class: "pri" });
  const waitingFor = new Map();
  for (const r of data.handoffs) if (r.status === "waiting" && r.to.workspace_id) waitingFor.set(r.to.workspace_id, (waitingFor.get(r.to.workspace_id) ?? 0) + 1);
  const list = data.workspaces.length ? h("div", { class: "scroll" }, h("table", { class: "tbl ws-table" },
    h("thead", {}, h("tr", {}, ["Workspace", "Status", "orch version", "Runs on", "Actions"].map((label) => h("th", { scope: "col", text: label })))),
    h("tbody", {}, data.workspaces.map((w) => {
      const n = waitingFor.get(w.workspace_id) ?? 0;
      return h("tr", { "data-workspace": w.workspace_id },
        h("td", {}, h("b", { text: nameOf(w) }), h("div", {}, chip(modeName(w), w.mode === "full" ? "full" : "sel")),
          n ? small(n + (n === 1 ? " hand-off is" : " hand-offs are") + " waiting for this workspace") : null),
        h("td", {}, statusView(w, now)), h("td", {}, versionView(w, data.hub)), h("td", {}, hostView(w)),
        h("td", {}, h("div", { class: "ws-actions" }, button("Details", () => actions.details?.(w)), button("Open", () => actions.open?.(w)))));
    })))) : emptyState("workspaces");
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
