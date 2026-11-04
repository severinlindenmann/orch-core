import { emptyState } from "./feel.js";
// The Services page (run-11 feedback 12): hub, relay and publish, how they connect, whether they answer, and how to restart
// them. The hub cannot start or stop other services, so it offers "Check now" and the exact command with a Copy button.
import { ago, chip, dot, h, text } from "./ui.js";

const WHAT = {
  hub: "This server. Your workspaces, people and approvals live here.",
  relay: "Sends phone alerts so you can approve on your phone.",
  edge: "Edge: puts the hub on a public address.",
  publish: "Shows shared pages to people outside the company.",
};
export const NAME = { hub: "Hub", relay: "Relay", publish: "Publish", edge: "Edge" };
const COMMAND = { hub: "orch install hub", relay: "orch install relay", publish: "orch install publish" };

/** One row's state in plain words: label, chip kind, dot kind. */
export function stateOf(svc) {
  if (svc.state === "up") return { label: ["hub", "edge"].includes(svc.name) ? "Running" : "Responding", kind: "ok", dot: "up" };
  if (svc.state === "not_set_up") return { label: "Not set up", kind: "", dot: "" };
  return { label: "Not answering", kind: "warn", dot: "warn" };
}

/** The banner above the list. Never green while a service that is set up does not answer. */
export function summary(data) {
  const bad = ["hub", "relay", "publish", "edge"].filter((n) => data[n] && data[n].state === "down").map((n) => NAME[n]);
  if (bad.length) return { kind: "warn", text: bad.length === 1 ? bad[0] + " is not answering" : bad.join(" and ") + " are not answering" };
  const off = ["relay", "publish"].filter((n) => data[n].state === "not_set_up").map((n) => NAME[n]);
  return { kind: "", text: off.length ? "Everything that is set up is working. " + off.join(" and ") + (off.length === 1 ? " is" : " are") + " not set up." : "All services are working" };
}

function detail(name, svc, now) {
  const bits = [];
  if (name === "hub") bits.push(ago(svc.started_ms, now) === "just now" ? "Just started" : "Running for " + ago(svc.started_ms, now).replace(" ago", ""));
  else if (svc.state === "not_set_up") bits.push("Not set up on this hub");
  else if (name === "edge" && svc.state === "up" && !svc.checked_ms) bits.push("Hub runs behind it");
  else if (svc.state === "up") bits.push("Answered " + ago(svc.checked_ms ?? now, now));
  else bits.push(svc.last_ok_ms ? "Last answered " + ago(svc.last_ok_ms, now) : "Has not answered yet");
  const addr = svc.url || (name === "hub" ? globalThis.location?.origin : null);
  if (addr) bits.push("Address " + text(addr));
  else if (svc.state !== "not_set_up") bits.push("No address");
  return bits.join(" · ");
}

/** Start / Stop / Restart buttons when the helper runs on this machine and manages the part; else null. */
function controls(name, data, actions) {
  const helper = data.helper;
  if (!actions.service || helper?.state !== "up" || !(helper.parts ?? []).some((p) => p.name === name)) return null;
  const off = !!helper.hub_busy;
  const b = (verb, cls = "") => h("button", { class: cls, type: "button", disabled: off, "data-svc": name + "|" + verb, text: verb === "start" ? "Start" : verb === "stop" ? "Stop" : "Restart", onclick: () => actions.service(name, verb) });
  const down = data[name]?.state === "down";
  return h("div", { class: "svc-ctl row" }, down ? b("start", "pri") : null, b("restart"), name === "hub" ? null : b("stop"), actions.logs ? h("button", { type: "button", "data-logs": name, text: "Logs", onclick: () => actions.logs(name) }) : null);
}

function helperNote(data) {
  const st = data.helper?.state;
  const unit = data.helper?.helper_unit;
  if (st === "down" || (st === "not_set_up" && unit)) {
    const ask = "The service helper is not running, so the buttons are off. Ask your IT person to start it on the server";
    if (!unit) return h("p", { class: "small muted", id: "helper-note", text: ask + ": orch-hub helper." });
    return h("p", { class: "small muted", id: "helper-note" }, ask + ": ", h("code", { text: "systemctl --user start " + text(unit) }), ".");
  }
  return null;
}

function restart(name, copy) {
  const where = name === "hub" ? "on this server" : "on the computer that hosts " + NAME[name];
  const cmd = COMMAND[name];
  const btn = h("button", { class: "copy", type: "button", text: "Copy", "aria-label": "Copy the command for " + NAME[name], onclick: async () => { const ok = await copy(cmd); btn.textContent = ok ? "Copied" : "Select and copy"; btn.setAttribute("aria-label", btn.textContent); } });
  return h("details", { class: "how", id: "how-" + name },
    h("summary", { text: "How to restart" }),
    h("p", { class: "small muted", text: "The hub cannot restart this itself. Ask your IT person to run this " + where + (name === "hub" ? ". Your workspaces reconnect on their own." : ".") }),
    h("div", { class: "cmd" }, h("code", { text: cmd }), btn));
}

function connections(data) {
  const node = (name, status, kind) => h("li", { class: "node " + kind },
    h("b", { text: text(name) }), h("span", { class: "small", text: status }));
  const serviceNode = (name) => {
    const svc = data[name] ?? { state: "not_set_up" };
    const st = stateOf({ ...svc, name });
    return node(NAME[name], svc.state === "up" ? "Running" : st.label, st.kind || "muted");
  };
  const ws = data.workspaces ?? { count: 0, online: 0 };
  const list = data.workspaces_list;
  const workspaces = Array.isArray(list) && list.length ? list.slice(0, 8).map((w) =>
    node(w.name || "A workspace", w.online ? "online" : "offline", w.online ? "ok" : "muted")) :
    [node("Workspaces", Array.isArray(list) ? "0 workspaces, 0 online" : ws.count + " workspaces, " + ws.online + " online", "muted")];
  if (list?.length > 8) workspaces.push(h("li", { class: "small muted", text: "+" + (list.length - 8) + " more" }));
  const arrow = (label) => h("div", { class: "arrow" }, label,
    h("span", { class: "flow-direction", "aria-hidden": "true" }));
  return h("section", { class: "card svc-connections", "aria-labelledby": "connections-title" },
    h("h2", { id: "connections-title", text: "How it connects" }),
    h("div", { class: "flow" },
      h("ul", { class: "flow-column", "aria-label": "Workspaces" }, workspaces), arrow("link"),
      h("ul", { class: "flow-column", "aria-label": "Hub" }, serviceNode("hub")), arrow(""),
      h("ul", { class: "flow-column", "aria-label": "Connected services" }, ["relay", "publish", "edge"].map(serviceNode))));
}

/** data: GET /admin/services. actions: {check, open, copy, service(name, verb), logs(name)}. */
export function servicesView(data, actions, now = Date.now()) {
  if (!["hub", "relay", "publish"].some((name) => data[name])) return [h("h1", { text: "Services" }), emptyState("services")];
  const sum = summary(data);
  const rows = ["hub", "relay", "publish", "edge"].map((name) => {
    const svc = data[name] ?? { state: "not_set_up", url: null }, st = stateOf({ ...svc, name });
    return h("article", { class: "svc-row", id: "svc-" + name, "data-state": svc.state },
      h("div", { class: "svc-main" },
        h("h3", {}, dot(st.dot), NAME[name]),
        h("p", { class: "small muted", text: WHAT[name] }),
        h("p", { class: "small muted", text: detail(name, svc, now) })),
      h("div", { class: "svc-side" },
        chip(st.label, st.kind),
        name !== "edge" ? h("span", { class: "small muted", text: svc.version ? "Version " + text(svc.version) : "Version -" }) : null,
        ["relay", "publish"].includes(name) ? h("button", { class: svc.state === "down" ? "pri" : "", text: "Check now", onclick: () => actions.check() }) : null),
      name !== "edge" ? controls(name, data, actions) : null,
      name !== "edge" && !controls(name, data, actions) ? restart(name, actions.copy) : null);
  });
  const ws = data.workspaces ?? { count: 0, online: 0 };
  return [
    h("h1", { text: "Services" }),
    h("div", { class: "overall " + sum.kind, id: "services-summary", role: "status" }, dot(sum.kind ? "warn" : "up"), sum.text),
    h("p", { class: "muted", text: "Workspaces connect to this hub. The hub sends phone alerts through Relay and outside pages through Publish." }),
    helperNote(data),
    connections(data),
    h("section", { class: "card svc-list", "aria-label": "Services" }, rows),
    h("section", { class: "card row" },
      h("div", { class: "grow" }, h("h3", { text: "Workspaces" }), h("p", { class: "small muted", id: "services-ws", text: ws.count + (ws.count === 1 ? " workspace" : " workspaces") + ", " + ws.online + " online" })),
      h("button", { text: "See workspaces and hand-offs", onclick: () => actions.open() })),
  ].filter(Boolean);
}
