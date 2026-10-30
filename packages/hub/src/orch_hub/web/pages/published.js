// The Published page (M4 #1357, S1 #1452): the company's shared links and apps, read only. The rows come from orch-publish
// through the hub (GET /admin/published); this page never shows a link address, a token or a password. Every piece of text a
// workspace wrote goes in with textContent. The hub can only NARROW (S2, #1453): "Revoke link" and "Stop app", one click, no
// dialog. Anything that widens (extend, add a password, start, change who can open it) is shown as words naming the
// workspace ("Approve in Product"), never as a hub button.
import { emptyState, loading } from "../feel.js";
import { h, text } from "../ui.js";

const DAY = 86400000;
/** "in 7 days", "in 3 hours", "in a few minutes"; "ended" once the time has passed. */
export function until(ms, now = Date.now()) {
  if (!Number.isFinite(ms)) return "no end date";
  const d = ms - now;
  if (d <= 0) return "ended";
  if (d < 3600000) return "in a few minutes";
  if (d < DAY) { const n = Math.round(d / 3600000); return "in " + n + (n === 1 ? " hour" : " hours"); }
  const n = Math.round(d / DAY);
  return "in " + n + (n === 1 ? " day" : " days");
}
const tag = (label, kind = "") => h("span", { class: "chip " + kind, text: label });
const who = (r) => r.by_kind === "agent" ? (r.by_name ? text(r.by_name) + "'s agent" : "an agent") : r.by_name ? text(r.by_name) : "a person";
const ACCESS = { public: "Public", password: "Password", company: "Company only", group: "A group" };
const KIND = { page: "Page", app: "Web app", form: "Form" };
const live = (r) => r.state === "live" || r.state === "running";

const approveIn = (r) => h("div", { class: "small muted", "data-approve": r.id, text: "Extend or add a password: approve in " + text(r.workspace) });

function linksTable(links, now, act) {
  const rows = [...links].sort((a, b) => Number(live(b)) - Number(live(a)));
  return h("div", { class: "card" }, h("table", { class: "grid" },
    h("thead", {}, h("tr", {}, ["What", "From", "Until", "Opened", ""].map((c) => h("th", { text: c })))),
    h("tbody", {}, rows.map((r) => h("tr", { "data-link": r.id, class: live(r) ? "" : "muted" },
      h("td", {}, h("b", { text: text(r.label) }), " ", r.has_password ? tag("password") : null,
        h("div", { class: "small muted", text: "by " + who(r) })),
      h("td", {}, text(r.workspace)),
      h("td", { class: "small" }, r.state === "revoked" ? tag("revoked") : r.state === "expired" ? tag("expired") : until(r.expires_ms, now)),
      h("td", { class: "small", text: r.views + (r.views === 1 ? " time" : " times") }),
      h("td", {}, r.state === "live" && act ? [
        h("button", { type: "button", class: "danger", "data-revoke": r.id, text: "Revoke link", onclick: () => act("links/" + r.id + "/revoke", "Link revoked. People who already downloaded the files keep their copy.") }),
        approveIn(r)] : null))))));
}

function appsTable(apps, act) {
  const rows = [...apps].sort((a, b) => Number(live(b)) - Number(live(a)));
  return h("div", { class: "card" }, h("table", { class: "grid" },
    h("thead", {}, h("tr", {}, ["App", "From", "Who can open it", "Version", "Visits", ""].map((c) => h("th", { text: c })))),
    h("tbody", {}, rows.map((r) => h("tr", { "data-app": r.id, class: live(r) ? "" : "muted" },
      h("td", {}, h("b", { text: text(r.app_name) }), " ", tag(KIND[r.kind] ?? "App"),
        h("div", { class: "small " + (r.state === "running" ? "" : "muted"), text: r.state === "running" ? "running" : r.state === "stopped" ? "stopped" : "removed" })),
      h("td", {}, text(r.workspace), h("div", { class: "small muted", text: "by " + who(r) })),
      h("td", {}, tag(ACCESS[r.access] ?? "Public", r.access === "public" ? "warn" : "")),
      h("td", { class: "small", text: "v" + r.version }),
      h("td", { class: "small", text: r.visits_30d + " (30 days)" }),
      h("td", {}, r.state === "running" && act ? [
        h("button", { type: "button", class: "danger", "data-stop": r.id, text: "Stop app", onclick: () => act("apps/" + r.id + "/stop", "App stopped. People who already opened it keep what they saved.") }),
        h("div", { class: "small muted", "data-approve": r.id, text: "Start again or change who can open it: approve in " + text(r.workspace) })] : null))))));
}

/** P08: one line per workspace that keeps its links to itself, saying what to do. `rows` are the rail rows (role, openable);
 * `openSettings(id)` opens that workspace's hub settings (offered only for the person's own, openable workspace). The words
 * a workspace wrote go in through `text` and textContent only. */
function hiddenRow(w, rows, openSettings) {
  const row = rows.find((r) => r.id === w.workspace_id);
  const own = row?.role === "owner";
  const who = typeof w.owner_name === "string" && w.owner_name ? text(w.owner_name) : "its owner";
  const todo = own
    ? (row.online && row.canOpen && openSettings ? null : "Open it on its own computer, then choose Share everything in Settings > Hub.")
    : "Ask " + who + " to share them.";
  return h("div", { class: "row published-hidden", "data-hidden": w.workspace_id },
    h("div", { class: "grow small" }, h("b", { text: text(w.name) }), h("span", { text: " keeps its links and apps to itself, so they are not listed here." }),
      todo ? h("div", { class: "muted", text: todo }) : null),
    own && !todo ? h("button", { type: "button", class: "pri", "data-share-hub": w.workspace_id, text: "Let the hub see them", onclick: () => openSettings(w.workspace_id) }) : null);
}

/** `act(path, doneWords)`: POST /admin/published/<path>; absent in a read-only view (the buttons are then not drawn). */
export function publishedView(data, { now = Date.now(), tab, act, rows = [], openSettings } = {}) {
  const links = data.links ?? [], apps = data.apps ?? [], hidden = data.hidden ?? [];
  let current = tab ?? (apps.length ? "apps" : "links");
  const body = h("div", { class: "published-body" });
  const tabs = h("div", { class: "tabs", role: "tablist" });
  const draw = () => {
    tabs.replaceChildren(
      ...[["apps", "Apps & pages (" + apps.length + ")"], ["links", "Shared files (" + links.length + ")"]].map(([id, label]) =>
        h("button", { type: "button", role: "tab", "data-pubtab": id, "aria-selected": String(current === id), class: current === id ? "active" : "", text: label, onclick: () => { current = id; draw(); } })));
    const list = current === "apps" ? apps : links;
    body.replaceChildren(list.length
      ? (current === "apps" ? appsTable(apps, act) : linksTable(links, now, act))
      : emptyState(current === "apps" ? "published-apps" : "published-links"));
  };
  draw();
  return h("div", { class: "published" },
    h("h1", { text: "Published" }),
    h("p", { class: "lead", text: "Everything workspaces put out through Publish: apps and pages people can use, and files shared by link." }),
    data.stale_ms != null ? h("p", { class: "banner warn", role: "status", text: "Publish does not answer right now. This is what it said " + Math.max(1, Math.round(data.stale_ms / 60000)) + " min ago." }) : null,
    tabs, body,
    hidden.map((w) => hiddenRow(w, rows, openSettings)));
}

export async function publishedPage(d) {
  const finish = loading();
  d.pageLoading("published");
  try {
    if (!(await d.refresh())) return d.loginView();
    if (!d.state.user.is_admin) return d.home();
    if (d.state.user.needs_sign_in) return d.reauthView();
    await d.loadRail();
    const r = await d.api("GET", "/admin/published");
    if (r.status === 403 && r.body.error === "reauth_required") return d.reauthView();
    if (r.status !== 200) return d.shell("published", h("h1", { text: "Published" }), h("p", { role: "alert", text: "Cannot read what is published. " + d.say(r.body.error) }), h("button", { type: "button", text: "Try again", onclick: () => publishedPage(d) }));
    // Revoke / Stop: one click, then the list is read again and a notice says what happened
    const act = async (path, done) => {
      const x = await d.api("POST", "/admin/published/" + path);
      d.state.notice = x.status === 200 ? d.banner("ok", x.body.result === "unchanged" ? "Already ended" : done, null) : d.banner("bad", "Not changed", d.say(x.body.error ?? "unavailable"));
      await publishedPage(d);
    };
    d.shell("published", d.state.notice, publishedView(r.body, { act, rows: d.state.rail.rows, openSettings: d.openSettings }));
    d.state.notice = null;
  } finally { finish(); }
}

export const page = { path: "published", nav: { label: "Published", admin: true, needsWorkspace: true, after: "services" }, render: publishedPage };
