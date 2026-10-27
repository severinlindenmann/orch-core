// The Workspaces page (run-11 feedback 13): connected workspaces and the tickets sent between them. The hub passes
// hand-offs on sealed, so it knows who sent, to whom, when, and whether the receiver has taken it, never what it says.
import { ago, chip, dot, h, text, when } from "./ui.js";

const nameOf = (end) => (end && end.name ? text(end.name) : end && end.workspace_id ? "Workspace " + end.workspace_id.slice(0, 8) : "Another workspace");

/** The status cell: Delivered, or Waiting with who it waits for and since when. */
export function statusOf(row, now = Date.now()) {
  if (row.status === "delivered") return { label: "Delivered", kind: "ok" };
  const to = nameOf(row.to);
  return { label: row.to.online ? "Waiting for " + to + " to take it" : "Waiting for " + to + " to come online", kind: "warn", since: "since " + when(row.sent_ms, now) };
}

/** data: {workspaces: GET /me/workspaces rows, handoffs: GET /me/handoffs rows}. */
export function workspacesView(data, now = Date.now()) {
  const waitingFor = new Map();
  for (const r of data.handoffs) if (r.status === "waiting" && r.to.workspace_id) waitingFor.set(r.to.workspace_id, (waitingFor.get(r.to.workspace_id) ?? 0) + 1);
  const list = data.workspaces.length ? data.workspaces.map((w) => {
    const n = waitingFor.get(w.workspace_id) ?? 0;
    return h("article", { class: "list-row", "data-workspace": w.workspace_id },
      h("div", { class: "row" },
        h("h3", { class: "grow" }, dot(w.online ? "up" : ""), text(w.name ?? "Workspace " + w.workspace_id.slice(0, 8))),
        chip(w.online ? "Online" : "Offline", w.online ? "ok" : "")),
      h("p", { class: "small muted", text: w.mode === "full" ? "Hub can read everything" : "Hub can read shared items and summaries" }),
      n ? h("p", { class: "small", text: n + (n === 1 ? " hand-off is" : " hand-offs are") + " waiting for this workspace" }) : null);
  }) : h("p", { class: "empty", text: "No workspaces are connected yet." });
  const rows = data.handoffs.map((r) => {
    const st = statusOf(r, now);
    return h("tr", { "data-status": r.status },
      h("td", {}, h("span", { class: "pair" }, nameOf(r.from), h("span", { class: "ar", "aria-label": "to", text: "→" }), nameOf(r.to))),
      h("td", { text: when(r.sent_ms, now) }),
      h("td", {}, chip(st.label, st.kind), st.since ? h("span", { class: "small muted", text: " " + st.since }) : null));
  });
  const table = rows.length
    ? h("div", { class: "scroll" }, h("table", { class: "tbl", id: "handoffs" }, h("thead", {}, h("tr", {}, h("th", { text: "From → to" }), h("th", { text: "Sent" }), h("th", { text: "Status" }))), h("tbody", {}, rows)))
    : h("p", { class: "empty", id: "handoffs-empty", text: "No tickets have been sent between workspaces yet." });
  return [
    h("h1", { text: "Workspaces" }),
    h("div", { class: "work-grid" },
      h("section", { class: "card", "aria-labelledby": "ws-h" }, h("h2", { id: "ws-h", text: "Connected workspaces" }), list),
      h("section", { class: "card", "aria-labelledby": "ho-h" },
        h("h2", { id: "ho-h", text: "Hand-offs between workspaces" }),
        h("p", { class: "small muted", text: "Tickets one workspace sent to another. The hub passes them on sealed, so it shows who sent one, to whom and when, not what it says. Delivered means it reached the workspace, not that someone accepted it." }),
        table)),
  ];
}
