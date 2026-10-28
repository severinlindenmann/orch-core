// The approvals list only opens the existing review flow; it never signs.
import { h, text, chip, when } from "./ui.js";

const kindOf = (it) => ["gate", "verdict", "question"].includes(it.kind) ? it.kind : it.type;
const words = (it) => kindOf(it) === "gate" ? "Approve the " + text(it.gate || "requested") + " gate" : kindOf(it) === "verdict" ? "Give your verdict" : kindOf(it) === "question" ? "Answer a question" : "Needs you";
const sentWords = (type) => ({ "gate.approved": "Gate approval", "gate.changes_requested": "Request for changes", "verdict.given": "Verdict", "question.answered": "Answer" })[type] || "Signed decision";

export function reviewMeaning(it, workspace, gate) {
  const verb = it.kind === "gate" ? "You approve the " + text(gate || it.gate || "requested") + " gate of " : it.kind === "verdict" ? "You give your verdict on " : "You answer a question on ";
  return verb + text(it.key || it.log || "this ticket") + " in " + text(workspace) + ". The hub signs it for you with this browser's key.";
}

export function signedMeaning(sentence, workspace) {
  return sentence.replace(/^I /, "You ").replace(/\.$/, "") + " in " + text(workspace) + ". The hub signs it for you with this browser's key.";
}

export function inboxView(data, actions, now = Date.now()) {
  const workspaces = data.workspaces ?? [], filter = data.filter ?? {};
  const name = (id) => text(workspaces.find((w) => w.workspace_id === id)?.name || "Workspace");
  const selected = (id) => !filter.workspace || filter.workspace === "all" || filter.workspace === id;
  const items = (data.items ?? []).filter((it) => selected(it.workspace_id) && (!filter.kind || filter.kind === "all" || kindOf(it) === filter.kind));
  const select = (label, key, options) => h("label", {}, label, h("select", { "aria-label": label, onchange: (e) => actions.filter({ ...filter, [key]: e.target.value }) }, options.map(([value, label]) => h("option", { value, selected: (filter[key] || "all") === value, text: label }))));
  const phone = (data.channels ?? []).find((c) => c.kind === "iphone" && c.status === "active");
  const out = [h("h1", { text: "Approvals" }), h("p", { class: "lead", text: "Everything waiting for your signature, across your workspaces." }),
    h("p", { class: "small muted", text: phone ? "Phone: " + text(phone.label) + " can approve with Face ID" : "You can also approve on your iPhone: pair it in Home > Where you can sign." }),
    h("div", { class: "row" }, select("Workspace", "workspace", [["all", "All workspaces"], ...workspaces.map((w) => [w.workspace_id, text(w.name)])]), select("Kind", "kind", [["all", "All"], ["gate", "Gate"], ["verdict", "Verdict"], ["question", "Question"]]))];
  if (!items.length) out.push(h("p", { text: "Nothing is waiting for you." }));
  for (const id of new Set(items.map((it) => it.workspace_id))) {
    const rows = items.filter((it) => it.workspace_id === id);
    out.push(h("section", { class: "card" }, h("h2", { text: name(id) + " (" + rows.length + ")" }), rows.map((it) => {
      const summary = it.summary_only || it.signable === false;
      return h("div", { class: "list-row row" }, h("div", { class: "grow" }, h("strong", { text: words(it) }),
        h("p", { text: [text(it.key), text(it.title)].filter(Boolean).join(" · ") }), h("p", { class: "small muted", text: name(id) }),
        it.deadline != null ? h("p", { class: "small", text: "Due " + (typeof it.deadline === "number" ? when(it.deadline, now) : text(it.deadline)) }) : null,
        summary ? h("p", { text: "Summary only: answer it in " + name(id) }) : null),
        h("button", { text: summary ? "Open" : "Review", onclick: () => summary ? actions.open({ id, name: workspaces.find((w) => w.workspace_id === id)?.name || "Workspace" }) : actions.review(it) }));
    })));
  }
  for (const id of (data.offline ?? []).filter(selected)) out.push(h("div", { class: "list-row row" }, h("p", { text: name(id) + " is offline. Items for it are delivered when it is back." }), chip("Waiting")));
  for (const id of (data.unavailable ?? []).filter(selected)) out.push(h("p", { text: "Items for " + name(id) + " cannot be read right now." }));
  const queued = (data.queued ?? []).filter((it) => selected(it.workspace_id));
  if (queued.length) out.push(h("section", { class: "card" }, h("h2", { text: "Sent, waiting for the workspace" }), queued.map((it) => {
    const ws = workspaces.find((w) => w.workspace_id === it.workspace_id), n = name(it.workspace_id);
    return h("p", { text: sentWords(it.type) + " for " + n + ": " + (ws?.online ? "delivering" : "delivered when " + n + " is back") });
  })));
  return out;
}
