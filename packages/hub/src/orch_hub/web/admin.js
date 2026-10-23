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
const txt = (v) => renderText(typeof v === "object" && v !== null ? JSON.stringify(v) : String(v ?? ""));
const p = (text) => h("p", { class: "small muted", text });
const chip = (text, kind = "") => h("span", { class: "chip " + kind, text });
const field = (name, input) => h("label", {}, name, input);
const roleName = (role) => ({ read: "Read", edit: "Edit", admin: "Admin" }[role] ?? txt(role));
const select = (rows) => h("select", { required: true }, rows.map(([value, name]) => h("option", { value, text: txt(name) })));
function table(headers, rows, label) {
  return h("div", { class: "scroll", tabindex: "0", role: "region", "aria-label": label },
    h("table", { class: "grid" }, h("caption", { text: label }),
      h("thead", {}, h("tr", {}, headers.map((x) => h("th", { scope: "col", text: x })))),
      h("tbody", {}, rows.map((row) => h("tr", {}, row.map((x, i) => h(i ? "td" : "th", i ? {} : { scope: "row" }, x)))))));
}

/** Returns immediately; named API envelopes populate the panels asynchronously. */
export function adminView(d) {
  const message = h("div", { "aria-live": "polite" });
  if (d.notice) message.append(d.notice.nodeType ? d.notice : p(txt(d.notice)));
  const panels = h("div", {}, p("Loading admin data…"));
  const tabs = h("div", { class: "section-tabs", role: "tablist", "aria-label": "Admin sections" });
  let data, selected = 0, generation = 0;
  const names = ["People & groups", "Access", "Workspaces", "Audit"];
  const buttons = names.map((name, i) => h("button", {
    id: "admin-tab-" + i, role: "tab", text: name, "aria-controls": "admin-panel-" + i,
    "aria-selected": String(i === selected), tabindex: i === selected ? "0" : "-1",
    onclick: () => choose(i), onkeydown: (ev) => {
      const next = ev.key === "ArrowRight" ? (i + 1) % 4 : ev.key === "ArrowLeft" ? (i + 3) % 4 : ev.key === "Home" ? 0 : ev.key === "End" ? 3 : null;
      if (next !== null) { ev.preventDefault(); choose(next); buttons[next].focus(); }
    },
  }));
  tabs.append(...buttons);
  function choose(i) {
    selected = i;
    buttons.forEach((b, n) => { b.setAttribute("aria-selected", String(n === i)); b.tabIndex = n === i ? 0 : -1; });
    [...panels.children].forEach((panel, n) => { if (panel.getAttribute("role") === "tabpanel") panel.hidden = n !== i; });
  }
  function error(code) {
    message.replaceChildren(h("div", { class: "banner bad", role: "alert", text: d.say(code) }));
  }
  async function load() {
    const current = ++generation;
    try {
      const keys = ["users", "groups", "grants", "workspaces", "channels", "audit"];
      const responses = await Promise.all(keys.map((key) => d.api("GET", "/admin/" + key)));
      if (current !== generation) return;
      const failed = responses.find((r) => r.status !== 200);
      if (failed) { error(failed.body?.error ?? "unavailable"); panels.replaceChildren(h("button", { text: "Try again", onclick: load })); return; }
      data = Object.fromEntries(keys.map((key, i) => [key, key === "audit" ? responses[i].body : responses[i].body[key]]));
      if (data.audit.head?.seq > Math.max(0, ...(data.audit.records ?? []).map((r) => r.seq))) {
        const recent = await d.api("GET", "/admin/audit?since=" + Math.max(0, data.audit.head.seq - 100));
        if (current !== generation) return;
        if (recent.status !== 200) { error(recent.body?.error ?? "unavailable"); return; }
        data.audit = recent.body;
      }
      draw();
    } catch { if (current === generation) { error("unavailable"); panels.replaceChildren(h("button", { text: "Try again", onclick: load })); } }
  }
  async function change(button, method, path, body) {
    button.disabled = true;
    try {
      const r = await d.api(method, path, body);
      if (r.status < 200 || r.status >= 300) { error(r.body?.error ?? "unavailable"); return; }
      message.replaceChildren(h("div", { class: "banner ok", role: "status", text: "Saved" }));
      await load();
      await d.refresh();
    } catch { error("unavailable"); }
    finally { button.disabled = false; }
  }
  function remove(label, method, path, body) {
    let armed = false;
    const button = h("button", { type: "button", class: "danger", text: label, onclick: () => {
      if (!armed) { armed = true; button.textContent = label === "Revoke" ? "Really revoke?" : "Really remove?"; return; }
      armed = false;
      void change(button, method, path, body);
    }, onblur: () => { armed = false; button.textContent = label; } });
    return button;
  }
  function form(label, fields, method, path, body) {
    const button = h("button", { class: "pri", type: "submit", text: label });
    return h("form", { onsubmit: (ev) => { ev.preventDefault(); if (!button.disabled) void change(button, method, typeof path === "function" ? path() : path, body()); } },
      h("div", { class: "form-grid" }, fields, button));
  }
  const enc = encodeURIComponent;
  const workspaceName = (id) => txt(data.workspaces.find((w) => w.workspace_id === id)?.name ?? id);
  const personName = (id) => txt(data.users.find((u) => u.user_id === id)?.name ?? id);
  const groupMembers = (group) => (data.groups.find((g) => g.name === group)?.members ?? []).map(personName).join(", ") || "No members";
  function people() {
    const name = h("input", { required: true, maxlength: 120, autocomplete: "off" });
    const groupName = h("input", { required: true, maxlength: 120 });
    const person = select(data.users.map((u) => [u.user_id, u.name]));
    const group = select(data.groups.map((g) => [g.name, g.name]));
    const preview = h("p", { class: "access-preview", "aria-live": "polite" });
    const updatePreview = () => {
      const g = data.groups.find((x) => x.name === group.value);
      if (!g || !person.value) { preview.textContent = "Create a person and a group to preview access."; return; }
      const grants = g.name === "hub-admin" ? data.workspaces.filter((w) => w.mode === "full").map((w) => ({ workspace_id: w.workspace_id, role: "admin" }))
        : data.grants.filter((x) => x.group === g.name && data.workspaces.some((w) => w.workspace_id === x.workspace_id && w.mode === "full"));
      const effects = grants.map((x) => roleName(x.role) + " on " + workspaceName(x.workspace_id));
      if (g.name === "hub-admin") effects.push("hub administration");
      preview.textContent = "Adding " + personName(person.value) + " to " + txt(g.name) + " gives " + (effects.join("; ") || "no workspace access yet") + ".";
    };
    person.addEventListener("change", updatePreview); group.addEventListener("change", updatePreview); updatePreview();
    return [h("h2", { text: "People & groups" }),
      h("div", { class: "admin-grid" },
        h("section", { class: "card" }, h("h3", { text: "Add person" }), form("Add person", [field("Name", name)], "POST", "/admin/users", () => ({ name: name.value.trim() }))),
        h("section", { class: "card" }, h("h3", { text: "Add to group" }),
          form("Add to group", [field("Person", person), field("Group", group)], "PUT", () => "/admin/groups/" + enc(group.value) + "/members/" + enc(person.value), () => undefined), preview)),
      h("section", { class: "card" }, h("h3", { text: "People" }), data.users.length ? data.users.map((u) => h("article", { class: "list-row row" },
        h("div", { class: "grow" }, h("b", { text: txt(u.name) }), p(txt(u.user_id)),
          h("div", { class: "row" }, data.groups.filter((g) => g.members.includes(u.user_id)).map((g) => chip(txt(g.name)))),
          p(data.channels.filter((c) => c.user_id === u.user_id).map((c) => txt(c.label || c.kind) + " · " + txt(c.status)).join(", ") || "No signing channels")),
        remove("Remove", "DELETE", "/admin/users/" + enc(u.user_id)))) : p("No people yet.")),
      h("section", { class: "card" }, h("h3", { text: "Groups" }),
        form("Create group", [field("Group name", groupName)], "POST", "/admin/groups", () => ({ name: groupName.value.trim() })),
        data.groups.map((g) => h("article", { class: "list-row" }, h("div", { class: "row" }, h("b", { class: "grow", text: txt(g.name) }),
          g.name !== "hub-admin" ? remove("Remove", "DELETE", "/admin/groups/" + enc(g.name)) : chip("Hub administrators")),
          g.members.length ? g.members.map((id) => h("div", { class: "row top" }, h("span", { class: "grow", text: personName(id) }),
            remove("Remove", "DELETE", "/admin/groups/" + enc(g.name) + "/members/" + enc(id)))) : p("No members"))))];
  }
  function access() {
    const workspace = select(data.workspaces.filter((w) => w.mode === "full").map((w) => [w.workspace_id, w.name ?? w.workspace_id]));
    const group = select(data.groups.filter((g) => g.name !== "hub-admin").map((g) => [g.name, g.name]));
    const role = select(["read", "edit", "admin"].map((r) => [r, roleName(r)]));
    return [h("h2", { text: "Access" }), p("Groups grant access to full workspaces. Selective workspaces accept only their owner. Hub administrators manage every full workspace."),
      table(["Group", ...data.workspaces.map((w) => workspaceName(w.workspace_id)), "Members"], data.groups.map((g) => [txt(g.name),
        ...data.workspaces.map((w) => w.mode === "selective" ? "Personal · owner only" : g.name === "hub-admin" ? "Admin" : roleName(data.grants.find((x) => x.workspace_id === w.workspace_id && x.group === g.name)?.role ?? "No access")),
        String(g.members.length)]), "Grants overview"),
      h("section", { class: "card" }, h("h3", { text: "Grant access" }),
        form("Grant access", [field("Workspace", workspace), field("Group", group), field("Permission", role)], "PUT", "/admin/grants",
          () => ({ workspace_id: workspace.value, group: group.value, role: role.value }))),
      ...data.workspaces.map((w) => h("section", { class: "workspace-detail" }, h("h3", { text: workspaceName(w.workspace_id) }),
        w.mode === "selective" ? p("Personal · owner only") : table(["Group", "Permission", "Members", "Action"],
          [...data.groups.filter((g) => g.name === "hub-admin").map((g) => [txt(g.name), "Admin", groupMembers(g.name), "Built in"]),
            ...data.grants.filter((g) => g.workspace_id === w.workspace_id).map((g) => [txt(g.group), roleName(g.role), groupMembers(g.group),
              remove("Revoke", "POST", "/admin/grants/revoke", { workspace_id: w.workspace_id, group: g.group })])], "Workspace permissions")))];
  }
  function workspaces() {
    return [h("h2", { text: "Workspaces" }), ...data.workspaces.map((w) => h("article", { class: "card" },
      h("div", { class: "row" }, h("h3", { class: "grow", text: workspaceName(w.workspace_id) }), chip(w.mode === "full" ? "Full" : "Selective", w.mode === "full" ? "full" : "sel")),
      p(w.mode === "full" ? "Hub can read everything" : "Hub can read shared items and summaries"),
      chip((w.online ?? (w.status === "bound" || w.status === "online")) ? "Connection online" : "Connection offline", (w.online ?? (w.status === "bound" || w.status === "online")) ? "ok" : "warn"),
      p("Binding: " + txt(w.status ?? "unknown")), w.mode === "selective" ? p("Personal · owner only · " + personName(w.owner_alias)) : null)),
      !data.workspaces.length ? p("No workspaces yet.") : null];
  }
  function audit() {
    const records = [...(data.audit.records ?? [])].sort((a, b) => b.seq - a.seq);
    return [h("h2", { text: "Audit" }), p("The API returns up to 100 records per page. Outcomes are shown only when recorded."),
      table(["Time", "Actor", "What", "Outcome"], records.map(({ record, event: e }) => {
        const o = record?.o ?? {};
        const time = new Date(o.ts_ms);
        let what = o.type === "grant" ? txt(e.group) + " on " + workspaceName(e.workspace_id) + ": " + (e.role ? roleName(e.role) : "Revoked")
          : o.type === "membership" ? personName(e.user_id) + (e.added ? " added to " : " removed from ") + txt(e.group) : txt(e.verb ?? e.change ?? o.type);
        if ("before" in e || "after" in e) what += ": " + txt(e.before) + " → " + txt(e.after);
        const actor = o.type === "custodial" ? "Hub on behalf of " + personName(e.user_id) : personName(e.actor_user_id ?? "Hub operator");
        return [Number.isNaN(time.getTime()) ? "Not recorded" : time.toLocaleString(), actor, what, txt(e.outcome ?? "Not recorded")];
      }), "Recent access changes"), !records.length ? p("No access changes recorded.") : null];
  }
  function draw() {
    panels.replaceChildren(...[people, access, workspaces, audit].map((view, i) => h("section", {
      id: "admin-panel-" + i, role: "tabpanel", "aria-labelledby": "admin-tab-" + i, hidden: i !== selected,
    }, view())));
  }
  void load();
  return [h("h1", { text: "Admin" }), p("Groups managed here; external sync not configured"), message, tabs, panels];
}
