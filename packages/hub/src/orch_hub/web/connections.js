import { h, text } from "./ui.js";
import { emptyState, loading } from "./feel.js";

function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  node.append(...children);
  return node;
}
const label = (w) => text(w.name || "Unnamed workspace");

export function connectionsView(data, actions = {}) {
  const workspaces = data.workspaces ?? [], edges = data.edges ?? [];
  const has = (a, b) => edges.some((e) => e.from === a && e.to === b);
  const root = h("div", { class: "connections" }, h("h1", { text: "Connections" }),
    h("p", { class: "lead", text: "Shows who may send hand-offs to whom right now, from people's access. Changing connections comes later." }));
  if (!workspaces.length) { root.append(emptyState("workspaces")); return root; }
  const details = h("div", { class: "connection-details", "aria-live": "polite", text: "Choose a workspace to see who it can reach and who can reach it." });
  const map = svg("svg", { viewBox: "0 0 640 480", "aria-label": "Connections between workspaces" });
  const positions = new Map(workspaces.map((w, i) => {
    const angle = -Math.PI / 2 + i * 2 * Math.PI / workspaces.length;
    return [w.workspace_id, [320 + 180 * Math.cos(angle), 225 + 180 * Math.sin(angle)]];
  }));
  const lines = [], nodes = [];
  for (const edge of edges) {
    const from = positions.get(edge.from), to = positions.get(edge.to);
    if (!from || !to) continue;
    const group = svg("g", { class: "connection-edge", "data-from": edge.from, "data-to": edge.to });
    if (edge.from === edge.to) {
      const [x, y] = from;
      group.append(svg("path", { d: `M ${x + 22} ${y - 15} C ${x + 78} ${y - 70}, ${x + 78} ${y + 70}, ${x + 24} ${y + 16}`, fill: "none", stroke: "currentColor", "stroke-width": 2 }),
        svg("path", { d: `M ${x + 24} ${y + 16} l 10 -1 l -5 9 Z`, fill: "currentColor" }));
    } else {
      const dx = to[0] - from[0], dy = to[1] - from[1], length = Math.hypot(dx, dy), ux = dx / length, uy = dy / length;
      const offset = has(edge.to, edge.from) ? 7 : 0;
      const x1 = from[0] + ux * 29 - uy * offset, y1 = from[1] + uy * 29 + ux * offset;
      const x2 = to[0] - ux * 33 - uy * offset, y2 = to[1] - uy * 33 + ux * offset;
      group.append(svg("line", { x1, y1, x2, y2, stroke: "currentColor", "stroke-width": 2 }),
        svg("path", { d: `M ${x2} ${y2} L ${x2 - ux * 11 - uy * 5} ${y2 - uy * 11 + ux * 5} L ${x2 - ux * 11 + uy * 5} ${y2 - uy * 11 - ux * 5} Z`, fill: "currentColor" }));
    }
    lines.push({ edge, group }); map.append(group);
  }
  function select(w) {
    for (const { edge, group } of lines) group.setAttribute("class", "connection-edge " + (edge.from === w.workspace_id ? "outgoing" : edge.to === w.workspace_id ? "incoming" : "dim"));
    for (const { workspace, node } of nodes) node.setAttribute("aria-pressed", String(workspace === w));
    const names = (outgoing) => workspaces.filter((other) => outgoing ? has(w.workspace_id, other.workspace_id) : has(other.workspace_id, w.workspace_id)).map(label).join(", ") || "Nobody";
    details.replaceChildren(h("b", { text: label(w) }), h("p", { class: "outgoing", text: "Can reach: " + names(true) }), h("p", { class: "incoming", text: "Reached by: " + names(false) }));
    actions.select?.(w.workspace_id);
  }
  for (const w of workspaces) {
    const [x, y] = positions.get(w.workspace_id);
    const node = svg("g", { class: "connection-node " + (w.online ? "online" : "offline"), role: "button", tabindex: 0, "aria-pressed": "false", "aria-label": label(w) + (w.online ? " · online" : " · offline") });
    const name = svg("text", { x, y: y + 47, "text-anchor": "middle" }); name.textContent = label(w) + (w.online ? "" : " · offline");
    const initial = svg("text", { x, y: y + 6, "text-anchor": "middle" }); initial.textContent = label(w).slice(0, 2);
    node.append(svg("circle", { cx: x, cy: y, r: 28 }), initial, name);
    node.addEventListener("click", () => select(w));
    node.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(w); } });
    nodes.push({ workspace: w, node }); map.append(node);
  }
  root.append(h("section", { class: "card connections-map" }, h("h2", { text: "Map" }),
    h("p", { text: "An arrow A → B means A may reach B. A return arrow means B may reach A too. Green workspaces are online; grey ones are offline. Choose a workspace to highlight its connections." }), map, details));
  root.append(h("section", { class: "card connection-matrix" }, h("h2", { text: "Who may reach whom" }),
    h("table", {}, h("caption", { text: "Read from a row to a column." }),
      h("thead", {}, h("tr", {}, h("th", { scope: "col", text: "From / to" }), workspaces.map((w) => h("th", { scope: "col", text: label(w) })))),
      h("tbody", {}, workspaces.map((a) => h("tr", {}, h("th", { scope: "row", text: label(a) }), workspaces.map((b) => h("td", { text: has(a.workspace_id, b.workspace_id) ? "allowed" : "no" }))))))));
  return root;
}

export async function connectionsPage(d) {
  const finish = loading();
  d.pageLoading("connections");
  try {
    if (!(await d.refresh())) return d.loginView();
    if (!d.state.user.is_admin) return d.home();
    if (d.state.user.needs_sign_in) return d.reauthView();
    await d.loadRail();
    const response = await d.api("GET", "/admin/connections");
    if (response.status === 403 && response.body.error === "reauth_required") return d.reauthView();
    if (response.status !== 200) return d.shell("connections", h("h1", { text: "Connections" }), h("p", { role: "alert", text: "Cannot read connections. " + d.say(response.body.error) }));
    d.shell("connections", connectionsView(response.body));
  } finally { finish(); }
}
