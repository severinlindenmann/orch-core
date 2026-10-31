import { emptyState, itemKey } from "./feel.js";
import { renderText } from "./render.js";
import { newChannelsNotice } from "./phones.js";
import { privateRows } from "./workspaces.js";

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

const sharedId = (it) => String(it.workspace_id) + ":" + String(it.key);
/** P04: tickets that were in the last successful /me/shared read and are gone from this one are "ended". `prev` is null
 * when there is no earlier read (nothing to compare). A shared-again ticket drops out of the ended list. Only call this
 * after a successful read, so an unreachable hub never produces a false "stopped sharing". */
export function sharedEnded(prev, next, ended = []) {
  const now = new Set(next.map(sharedId));
  const have = new Set(ended.map((e) => e.id));
  const gone = Array.isArray(prev) ? prev.filter((it) => it.shared_by && !now.has(sharedId(it)) && !have.has(sharedId(it))) : [];
  return [...ended, ...gone.map((it) => ({ id: sharedId(it), by: it.shared_by, title: it.title ?? it.key ?? "a ticket" }))].filter((e) => !now.has(e.id));
}

/** Health and totals use only this person's workspace list. */
export function homeSummary(workspaces, items = [], offline = []) {
  const down = new Set(offline);
  const rows = workspaces.map((w) => ({ ...w, online: w.online !== false && !down.has(w.workspace_id) }));
  const byId = new Map(rows.map((w) => [w.workspace_id, w]));
  // Offline waiting items are notices, not actionable Needs-you rows or totals.
  const mine = items.filter((it) => {
    const w = byId.get(it.workspace_id);
    return w?.online && it.online !== false && (it.signable || it.summary_only || (it.mode ?? w.mode) === "selective");
  });
  return { rows, items: mine, online: rows.filter((w) => w.online).length, offline: rows.filter((w) => !w.online).length, waiting: mine.length };
}

export function workspaceStatus(w) {
  if (w.online) return "online";
  const seen = w.last_seen;
  if (seen == null) return "offline since an unknown time";
  const date = new Date(typeof seen === "number" ? seen * 1000 : NaN);
  return "offline since " + (Number.isNaN(date.getTime()) ? "an unknown time" : date.toLocaleString());
}

/** The first-run home is replaced by the full home once a workspace or an item shows up. */
export const firstRunOutgrown = (rows, items) => (rows ?? []).length > 0 || (items ?? []).length > 0;

/** A brand-new hub shows one thing: connect a workspace. No signing or developer widgets yet. */
function firstRunHome(d) {
  const notice = d.notice?.nodeType ? d.notice : d.notice ? h("div", { class: "banner", role: "status", text: text(d.notice) }) : null;
  const added = newChannelsNotice(d.newChannels, { remove: (c) => d.actions?.removeChannel(c.channel_id), dismiss: () => d.actions?.dismissNew?.() });
  return [h("h1", { text: "Hello, " + text(d.user?.name ?? "you") }), notice, ...added,
    h("p", { class: "company-summary muted", text: "Nothing connected yet." }),
    h("section", { class: "card first-run first-run-root", "aria-labelledby": "first-run-heading" },
      h("h2", { id: "first-run-heading", text: "Connect your first workspace" }),
      d.user?.is_admin && d.actions?.connect
        ? [h("p", { text: "We make a link for your workspace. You use it once." }), h("button", { class: "pri", text: "Connect a workspace", onclick: () => d.actions.connect() })]
        : h("p", { text: "Ask your hub admin to connect a workspace for you." }),
      note("More areas (approvals, add-ons, activity…) appear here once a workspace is connected."))].filter(Boolean);
}

/** Person home only; the driver owns navigation and the manual decision form. */
export function homeView(d) {
  if (!(d.workspaces ?? []).length && !(d.channels ?? []).length && !(d.items ?? []).length) return firstRunHome(d);
  const health = homeSummary(d.workspaces ?? [], d.items ?? [], d.offline ?? []);
  // P05: a hub admin's count also includes connected workspaces that are private to someone else (the card below stays own only)
  const hidden = privateRows(d.workspaces ?? [], d.adminWorkspaces);
  const counted = { ...health, online: health.online + hidden.filter((w) => w.online).length, offline: health.offline + hidden.filter((w) => !w.online).length };
  const workspaces = health.rows;
  const byId = new Map(workspaces.map((w) => [w.workspace_id, w]));
  const offline = new Set(d.offline ?? []);
  for (const w of workspaces) if (w.online === false) offline.add(w.workspace_id);
  const mode = (it) => it.mode ?? byId.get(it.workspace_id)?.mode;
  const summary = (it) => it.summary_only || (!it.signable && mode(it) === "selective");
  const items = health.items;
  const shortName = (w, id) => text(w?.name || (w?.owner_name ? w.owner_name + "'s workspace" : "A workspace"));
  const workspaceName = (id) => shortName(byId.get(id), id);
  const needs = h("section", { class: "card needs-you", "aria-labelledby": "needs-heading" },
    h("div", { class: "section-head row" }, h("h2", { id: "needs-heading", class: "grow", text: "Needs you" }), chip(String(items.length))),
    workspaces.some((w) => w.mode === "selective") ? h("p", { class: "small muted only-shared" }, "Only items you share appear here ",
      h("details", { class: "info" }, h("summary", { "aria-label": "More about what appears here", text: "i" }),
        h("p", { class: "small muted", text: "A workspace that shares only what you choose sends the hub just what you share. Other questions are still waiting in that workspace, so its own count can be higher." }))) : null,
    items.length ? workspaces.filter((w) => items.some((it) => it.workspace_id === w.workspace_id)).map((w) => h("section", { class: "workspace-needs", "data-live-key": w.workspace_id },
      h("h3", { text: workspaceName(w.workspace_id) }),
      items.filter((it) => it.workspace_id === w.workspace_id).map((it) => h("article", { class: "list-row row", "data-live-key": itemKey(it) },
      h("div", { class: "grow" }, h("h3", { text: text(it.title ?? it.kind) }),
        note([text(it.key), workspaceName(it.workspace_id)].filter(Boolean).join(" · ")),
        note(summary(it) ? "Summary only, answer on your Mac" : "Shared with the hub, you can sign here")),
      !summary(it) ? h("button", { text: "Review", onclick: () => d.actions.review(it) }) : null))))
      : emptyState("home"),
    [...offline].map((id) => h("div", { class: "list-row row" },
      h("div", { class: "grow" }, h("b", { text: workspaceName(id) }), note("Workspace is offline")), chip("Waiting", "warn"))));
  const spaces = h("section", { class: "card health-strip", "aria-labelledby": "workspaces-heading" },
    h("h2", { id: "workspaces-heading", text: "My workspaces" }),
    workspaces.length ? workspaces.map((w) => h("article", { class: "list-row row", "data-live-key": w.workspace_id },
      h("div", { class: "grow" }, h("b", { text: shortName(w, w.workspace_id) }),
        note(workspaceStatus(w) + " · " + (w.online
          ? health.items.filter((it) => it.workspace_id === w.workspace_id).length + " things need you"
          : "Hand-offs and approvals wait until it is back"))),
      d.actions.open ? h("button", { text: "Open", onclick: () => d.actions.open(w.workspace_id) }) : null))
      : emptyState("workspaces"));
  // Tickets shared with this person (GET /me/shared, the same filter as search): read-only, hidden when there are none.
  const sharedItems = (d.shared ?? []).filter((it) => it && typeof it === "object");
  const endedItems = (d.ended ?? []).filter((e) => e && typeof e === "object");
  const sharedCard = sharedItems.length || endedItems.length ? h("section", { class: "card shared-tickets", "aria-labelledby": "shared-heading" },
    h("h2", { id: "shared-heading", text: "Shared tickets (" + sharedItems.length + ")" }),
    endedItems.map((e) => h("div", { class: "list-row row ended", role: "status" },
      h("div", { class: "grow" }, note(text(e.by) + " stopped sharing " + text(e.title))),
      d.actions?.dismissEnded ? h("button", { text: "Dismiss", onclick: () => d.actions.dismissEnded(e.id) }) : null)),
    sharedItems.map((it) => h("article", { class: "list-row row" },
      h("div", { class: "grow" }, h("b", { text: text(it.title ?? it.key ?? "Ticket") }),
        note([workspaceName(it.workspace_id), it.shared_by ? "shared by " + text(it.shared_by) : "shared with you"].join(" · "))),
      d.actions?.openShared && it.workspace_id ? h("button", { text: "Read", onclick: () => d.actions.openShared(it) }) : null))) : null;
  const browser = d.thisBrowser;
  const active = browser?.state === "active";
  const dead = browser?.state === "dead";
  const local = h("div", { class: "browser-card" },
    h("b", { text: active && browser.needsSignIn ? "Ready. Sign in once to approve." : active ? "This browser holds a signing key for you" : dead ? "This browser's key no longer works" : "Approvals need a signing key in this browser" }),
    active ? note("Name: " + text(browser.label || "this browser"))
      : note(dead ? "Set it up again to keep approving here." : "One step: confirm with your password and a code."),
    !active ? h("button", { class: "pri", text: dead ? "Set up signing again" : "Set up signing", onclick: () => d.actions.register() }) : null);
  // P08: a person with nothing to sign (not an admin, nothing shared for approval, no key or phone) is not shown the card;
  // it appears by itself as soon as something needs their approval.
  const canSign = active || dead || (d.channels ?? []).length > 0 || !!d.user?.is_admin || items.some((it) => !summary(it));
  const signing = !canSign ? null : h("section", { class: "card", "aria-labelledby": "signing-heading" },
    h("h2", { id: "signing-heading", text: "Where you can sign" }), local,
    (d.channels ?? []).map((c) => h("div", { class: "list-row row" },
      h("div", { class: "grow" }, h("b", { text: text(c.label || c.kind) }), note(text(c.kind) + " · " + text(c.status))),
      c.status === "active" ? h("button", { text: "Remove", onclick: () => d.actions.removeChannel(c.channel_id) }) : null)),
    note("You confirm with a key held by this browser or your iPhone."));
  const notice = d.notice?.nodeType ? d.notice : d.notice ? h("div", { class: "banner", role: "status", text: text(d.notice) }) : null;
  const added = newChannelsNotice(d.newChannels, { remove: (c) => d.actions?.removeChannel(c.channel_id), dismiss: () => d.actions?.dismissNew?.() });
  return [h("h1", { text: "Hello, " + text(d.user?.name ?? "you") }), notice, ...added,
    h("p", { class: "company-summary", text: counted.online + (counted.online === 1 ? " workspace" : " workspaces") + " online, " + counted.offline + " offline, " + health.waiting + (health.waiting === 1 ? " thing needs you" : " things need you") }),
    h("div", { class: "home-grid" }, needs, h("div", { class: "home-side" }, sharedCard, spaces, signing))].filter(Boolean);
}
