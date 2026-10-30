import { emptyState } from "./feel.js";
import { chip, h, text, when } from "./ui.js";

const nameOf = (w) => text(w?.name || "Another workspace");
const button = (label, onclick, props = {}) => h("button", { type: "button", text: label, onclick, ...props });

export function statusOf(row, now = Date.now()) {
  if (row.status === "delivered") return { label: "Delivered", kind: "ok" };
  return { label: "Waiting for " + nameOf(row.to) + (row.to?.online ? " to take it" : " to come online"),
    kind: "warn", since: "since " + when(row.sent_ms, now) };
}

const info = (label, body) => h("details", { class: "info" }, h("summary", { "aria-label": label, text: "i" }), h("p", { class: "small muted", text: body }));

export function sendHandoffView(workspaces, actions = {}) {
  const sources = workspaces.filter((w) => w.can_start || w.online);
  const from = h("select", { "aria-label": "From" }, sources.map((w) => h("option", { value: w.workspace_id, text: nameOf(w) })));
  const error = h("p", { role: "alert" });
  const selected = () => sources.find((w) => w.workspace_id === from.value);
  const open = button("Open", async () => {
    if (open.disabled) return;
    open.disabled = true;
    error.textContent = "";
    try { await actions.open?.(selected()); }
    catch { error.textContent = "Could not open the workspace. Please try again."; }
    finally { update(); }
  }, { class: "pri" });
  function update() {
    const source = selected();
    open.disabled = !source;
    open.textContent = source ? "Open " + nameOf(source) : "Open";
  }
  from.value = sources[0]?.workspace_id ?? "";
  from.addEventListener("change", update);
  update();
  return [h("h2", { text: "Send a hand-off" }),
    h("div", { class: "row" }, h("p", { class: "grow", text: sources.length ? "Choose where it comes from, then use Hand off there." : "No workspace is available to send from." }),
      info("How hand-offs are sent", "A hand-off is sent from the workspace that holds the item. Open that workspace and choose Hand off on the item.")),
    h("label", {}, "From", from), error,
    h("div", { class: "row" }, button("Cancel", () => actions.close?.()), open)];
}

// The chosen filters live here so a redraw with fresh data (poll, refresh) keeps them.
const chosen = { workspace: "", state: "all" };

export function handoffsView(data, actions = {}, now = Date.now()) {
  const handoffs = data.handoffs ?? [], workspaces = data.workspaces ?? [];
  const known = new Map(workspaces.map((w) => [w.workspace_id, w]));
  for (const row of handoffs) for (const w of [row.from, row.to]) if (w?.workspace_id && !known.has(w.workspace_id)) known.set(w.workspace_id, w);
  const workspace = h("select", { "aria-label": "Workspace" }, h("option", { value: "", text: "All workspaces" }),
    [...known.values()].map((w) => h("option", { value: w.workspace_id, text: nameOf(w) })));
  if (!known.has(chosen.workspace)) chosen.workspace = "";
  workspace.value = chosen.workspace;
  const results = h("section", { class: "card", "aria-label": "Hand-offs" });
  const states = ["All", "Waiting", "Delivered"].map((label) => button(label, () => { chosen.state = label.toLowerCase(); draw(); }));
  function draw() {
    states.forEach((b) => b.setAttribute("aria-pressed", String(b.textContent.toLowerCase() === chosen.state)));
    const rows = handoffs.filter((r) => (chosen.state === "all" || r.status === chosen.state) &&
      (!chosen.workspace || r.from?.workspace_id === chosen.workspace || r.to?.workspace_id === chosen.workspace));
    results.textContent = "";
    if (!rows.length) {
      results.append(handoffs.length ? h("p", { class: "empty", text: "No hand-offs match these filters." }) : emptyState("handoffs"));
      return;
    }
    results.append(h("div", { class: "scroll" }, h("table", { class: "tbl" },
      h("thead", {}, h("tr", {}, ["From → to", "Sent", "State"].map((label) => h("th", { scope: "col", text: label })))),
      h("tbody", {}, rows.map((r) => {
        const st = statusOf(r, now);
        return h("tr", { "data-status": r.status },
          h("td", {}, h("span", { class: "pair" }, nameOf(r.from), h("span", { class: "ar", "aria-label": "to", text: "→" }), nameOf(r.to))),
          h("td", { text: when(r.sent_ms, now) }), h("td", {}, chip(st.label, st.kind)));
      })))));
  }
  workspace.addEventListener("change", () => { chosen.workspace = workspace.value; draw(); });
  draw();
  return [h("div", { class: "row" }, h("h1", { class: "grow", text: "Hand-offs" }),
    button("Send a hand-off", () => actions.dialog?.(...sendHandoffView(workspaces, actions)), { class: "pri" })),
    h("div", { class: "row" }, h("p", { class: "grow", text: "Which workspace sent something to which, and whether it arrived." }),
      info("What the hub knows", "The hub only knows who sent it, to whom, when, and whether it arrived. What is inside stays private. Delivered means it reached the workspace, not that someone accepted it.")),
    h("div", { class: "row" }, h("label", {}, "Workspace", workspace), h("div", { class: "ws-segment", role: "group", "aria-label": "State" }, states)), results];
}
