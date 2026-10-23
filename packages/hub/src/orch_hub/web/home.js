import { renderText } from "./render.js";

function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v != null && v !== false) el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const child of kids.flat()) if (child != null && child !== false)
    el.append(child.nodeType ? child : document.createTextNode(String(child)));
  return el;
}
const text = (v) => renderText(String(v ?? ""));
const chip = (label, kind = "") => h("span", { class: "chip " + kind, text: label });
const note = (label) => h("p", { class: "small muted", text: label });
const readLine = (w) => w.mode === "full" ? "Hub can read everything" : "Hub can read shared items and summaries";

/** Person home only; the driver owns navigation and the manual decision form. */
export function homeView(d) {
  const workspaces = d.workspaces ?? [];
  const byId = new Map(workspaces.map((w) => [w.workspace_id, w]));
  const offline = new Set(d.offline ?? []);
  for (const w of workspaces) if (w.online === false) offline.add(w.workspace_id);
  const mode = (it) => it.mode ?? byId.get(it.workspace_id)?.mode;
  const summary = (it) => it.summary_only || (!it.signable && mode(it) === "selective");
  const items = (d.items ?? []).filter((it) => !offline.has(it.workspace_id) && it.online !== false && (it.signable || summary(it)));
  const shortName = (w, id) => text(w?.name ?? "Workspace " + String(id).slice(0, 8));
  const workspaceName = (id) => shortName(byId.get(id), id);
  const needs = h("section", { class: "card needs-you", "aria-labelledby": "needs-heading" },
    h("div", { class: "section-head row" }, h("h2", { id: "needs-heading", class: "grow", text: "Needs you" }), chip(String(items.length))),
    items.length ? items.map((it) => h("article", { class: "list-row row" },
      h("div", { class: "grow" }, h("h3", { text: text(it.title ?? it.kind) }),
        note([text(it.key), workspaceName(it.workspace_id)].filter(Boolean).join(" · ")),
        note(summary(it) ? "Summary only, answer on your Mac" : "Shared with the hub, you can sign here")),
      !summary(it) ? h("button", { text: "Review", onclick: () => d.actions.review(it) }) : null))
      : h("p", { class: "empty", text: "Nothing is waiting for you." }),
    [...offline].map((id) => h("div", { class: "list-row row" },
      h("div", { class: "grow" }, h("b", { text: workspaceName(id) }), note("Workspace is offline")), chip("Waiting", "warn"))));
  const spaces = h("section", { class: "card", "aria-labelledby": "workspaces-heading" },
    h("h2", { id: "workspaces-heading", text: "My workspaces" }),
    workspaces.length ? workspaces.map((w) => h("article", { class: "list-row" },
      h("div", { class: "row" }, h("h3", { class: "grow", text: shortName(w, w.workspace_id) }),
        chip(w.mode === "full" ? "Full" : "Selective", w.mode === "full" ? "full" : "sel")),
      note(readLine(w)), chip(offline.has(w.workspace_id) ? "Offline" : "Online", offline.has(w.workspace_id) ? "warn" : "ok")))
      : note("No workspaces yet."));
  const browser = d.thisBrowser;
  const active = browser?.state === "active";
  const local = h("div", { class: "browser-card" },
    h("b", { text: active ? "This browser holds a signing key for you" : "This browser cannot sign yet" }),
    active ? note("Name: " + text(browser.label || "this browser")) : note("Browser not registered"),
    !active ? note(browser?.state === "dead" ? "The key is missing here or was removed at the hub. Register again to sign here."
      : "Registering gives this browser a signing key for you. Signing needs the key; being signed in only lets you look.") : null,
    browser?.state === "dead" ? h("button", { text: "Forget the key in this browser", onclick: () => d.actions.forgetLocal() }) : null,
    !active ? h("button", { class: "pri", text: "Register this browser", onclick: () => d.actions.register() }) : null);
  const signing = h("section", { class: "card", "aria-labelledby": "signing-heading" },
    h("h2", { id: "signing-heading", text: "Where you can sign" }), local,
    (d.channels ?? []).map((c) => h("div", { class: "list-row row" },
      h("div", { class: "grow" }, h("b", { text: text(c.label || c.kind) }), note(text(c.kind) + " · " + text(c.status))),
      c.status === "active" ? h("button", { text: "Remove", onclick: () => d.actions.removeChannel(c.channel_id) }) : null)),
    note("You confirm with a key held by this browser or your iPhone. The hub signs this approval for you."));
  const notice = d.notice?.nodeType ? d.notice : d.notice ? h("div", { class: "banner", role: "status", text: text(d.notice) }) : null;
  return [h("h1", { text: "Hello, " + text(d.user?.name ?? "you") }), notice,
    h("div", { class: "home-grid" }, needs, h("div", { class: "home-side" }, spaces, signing))].filter(Boolean);
}
