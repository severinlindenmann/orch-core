import { emptyState } from "./feel.js";
import { chip, h, text, when } from "./ui.js";

const nameOf = (w) => text(w?.name || (w?.workspace_id ? "Workspace " + w.workspace_id.slice(0, 8) : "Another workspace"));
const button = (label, onclick, props = {}) => h("button", { type: "button", text: label, onclick, ...props });

export function statusOf(row, now = Date.now()) {
  if (row.status === "delivered") return { label: "Delivered", kind: "ok" };
  return { label: "Waiting for " + nameOf(row.to) + (row.to?.online ? " to take it" : " to come online"),
    kind: "warn", since: "since " + when(row.sent_ms, now) };
}

export function sendHandoffView(workspaces, actions = {}) {
  const sources = workspaces.filter((w) => w.can_start || w.online);
  const from = h("select", { "aria-label": "From" }, sources.map((w) => h("option", { value: w.workspace_id, text: nameOf(w) })));
  const to = h("select", { "aria-label": "To" });
  const instruction = h("p", { "aria-live": "polite" });
  const error = h("p", { role: "alert" });
  const selected = () => sources.find((w) => w.workspace_id === from.value);
  const open = button("Open workspace to hand off", async () => {
    if (open.disabled) return;
    open.disabled = true;
    error.textContent = "";
    try { await actions.open?.(selected()); }
    catch { error.textContent = "Could not open the workspace. Please try again."; }
    finally { update(); }
  }, { class: "pri" });
  function update() {
    const source = selected(), target = workspaces.find((w) => w.workspace_id === to.value);
    open.disabled = !source || !target;
    open.textContent = source ? "Open " + nameOf(source) + " to hand off" : "Open workspace to hand off";
    instruction.textContent = source && target ? "In " + nameOf(source) + ", open the ticket and choose Hand off to " + nameOf(target) + "."
      : !source ? "No workspace is available to send from." : "Connect another workspace to send a hand-off.";
  }
  function destinations() {
    const previous = to.value;
    const others = workspaces.filter((w) => w.workspace_id !== from.value);
    to.textContent = "";
    to.append(...others.map((w) => h("option", { value: w.workspace_id, text: nameOf(w) })));
    to.value = others.some((w) => w.workspace_id === previous) ? previous : others[0]?.workspace_id ?? "";
    update();
  }
  from.value = sources[0]?.workspace_id ?? "";
  from.addEventListener("change", destinations);
  to.addEventListener("change", update);
  destinations();
  return [h("h2", { text: "Send a hand-off" }), h("p", { text: "The hub cannot send sealed tickets itself. Open a workspace to hand one off." }),
    h("label", {}, "From", from), h("label", {}, "To", to), instruction, error,
    h("div", { class: "row" }, button("Cancel", () => actions.close?.()), open)];
}

export function handoffsView(data, actions = {}, now = Date.now()) {
  const handoffs = data.handoffs ?? [], workspaces = data.workspaces ?? [];
  const known = new Map(workspaces.map((w) => [w.workspace_id, w]));
  for (const row of handoffs) for (const w of [row.from, row.to]) if (w?.workspace_id && !known.has(w.workspace_id)) known.set(w.workspace_id, w);
  const workspace = h("select", { "aria-label": "Workspace" }, h("option", { value: "", text: "All workspaces" }),
    [...known.values()].map((w) => h("option", { value: w.workspace_id, text: nameOf(w) })));
  workspace.value = "";
  let state = "all";
  const results = h("section", { class: "card", "aria-label": "Hand-offs" });
  const states = ["All", "Waiting", "Delivered"].map((label) => button(label, () => { state = label.toLowerCase(); draw(); }));
  function draw() {
    states.forEach((b) => b.setAttribute("aria-pressed", String(b.textContent.toLowerCase() === state)));
    const rows = handoffs.filter((r) => (state === "all" || r.status === state) &&
      (!workspace.value || r.from?.workspace_id === workspace.value || r.to?.workspace_id === workspace.value));
    results.textContent = "";
    if (!rows.length) {
      results.append(handoffs.length ? emptyState("search") : emptyState("handoffs"));
      return;
    }
    results.append(h("div", { class: "scroll" }, h("table", { class: "tbl" },
      h("thead", {}, h("tr", {}, ["From → to", "Sent", "State"].map((label) => h("th", { scope: "col", text: label })))),
      h("tbody", {}, rows.map((r) => {
        const st = statusOf(r, now);
        return h("tr", { "data-status": r.status },
          h("td", {}, h("span", { class: "pair" }, nameOf(r.from), h("span", { class: "ar", "aria-label": "to", text: "→" }), nameOf(r.to))),
          h("td", { text: when(r.sent_ms, now) }), h("td", {}, chip(st.label, st.kind),
            st.since ? h("div", { class: "small muted", text: st.since }) : null));
      })))));
  }
  workspace.addEventListener("change", draw);
  draw();
  return [h("div", { class: "row" }, h("h1", { class: "grow", text: "Hand-offs" }),
    button("Send a hand-off", () => actions.dialog?.(...sendHandoffView(workspaces, actions)), { class: "pri" })),
    h("p", { text: "The hub only knows which workspace sent it to which, when, and whether it arrived. What is inside stays sealed. Delivered means it reached the workspace, not that someone accepted it." }),
    h("div", { class: "row" }, h("label", {}, "Workspace", workspace), h("div", { class: "ws-segment", role: "group", "aria-label": "State" }, states)), results];
}
