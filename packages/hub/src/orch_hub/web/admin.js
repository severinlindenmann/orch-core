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
  const names = ["People & groups", "Access", "Workspaces", "Marketplaces", "Audit"];
  const buttons = names.map((name, i) => h("button", {
    id: "admin-tab-" + i, role: "tab", text: name, "aria-controls": "admin-panel-" + i,
    "aria-selected": String(i === selected), tabindex: i === selected ? "0" : "-1",
    onclick: () => choose(i), onkeydown: (ev) => {
      const next = ev.key === "ArrowRight" ? (i + 1) % names.length : ev.key === "ArrowLeft" ? (i + names.length - 1) % names.length : ev.key === "Home" ? 0 : ev.key === "End" ? names.length - 1 : null;
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
      const keys = ["users", "groups", "grants", "workspaces", "channels", "market-policy", "audit"];
      const responses = await Promise.all(keys.map((key) => d.api("GET", "/admin/" + key)));
      if (current !== generation) return;
      const failed = responses.find((r) => r.status !== 200);
      if (failed) { error(failed.body?.error ?? "unavailable"); panels.replaceChildren(h("button", { text: "Try again", onclick: load })); return; }
      data = Object.fromEntries(keys.map((key, i) => [key, key === "audit" || key === "market-policy" ? responses[i].body : responses[i].body[key]]));
      data.policy = data["market-policy"];
      syncMp();
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
      if (!armed) { armed = true; button.textContent = "Really? " + label; return; }
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
  const workspaceName = (id) => txt(data.workspaces.find((w) => w.workspace_id === id)?.name ?? "Workspace " + String(id).slice(0, 8));
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
          p(data.channels.filter((c) => c.user_id === u.user_id).map((c) => txt(c.label || c.kind) + " · " + txt(c.status)).join(", ") || "No approval device set up")),
        remove("Remove person", "DELETE", "/admin/users/" + enc(u.user_id)))) : p("No people yet.")),
      h("section", { class: "card" }, h("h3", { text: "Groups" }),
        form("Create group", [field("Group name", groupName)], "POST", "/admin/groups", () => ({ name: groupName.value.trim() })),
        data.groups.map((g) => h("article", { class: "list-row" }, h("div", { class: "row" }, h("b", { class: "grow", text: txt(g.name) }),
          g.name !== "hub-admin" ? remove("Remove group", "DELETE", "/admin/groups/" + enc(g.name)) : chip("Hub administrators")),
          g.members.length ? g.members.map((id) => h("div", { class: "row top" }, h("span", { class: "grow", text: personName(id) }),
            remove("Remove from group", "DELETE", "/admin/groups/" + enc(g.name) + "/members/" + enc(id)))) : p("No members"))))];
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
  // "Connect a workspace": one click mints a single-use invite and shows ONE link (docs/connect-link.md).
  const connectBox = h("div", { "aria-live": "polite" });
  async function mintLink(button) {
    button.disabled = true;
    try {
      const r = await d.api("POST", "/admin/connect", {});
      if (r.status !== 200) { connectBox.replaceChildren(h("div", { class: "banner bad", role: "alert", text: d.say(r.body?.error ?? "unavailable") })); return; }
      const { link, code, endpoint, hub_id: hubId, expires_ms: expires } = r.body;
      const input = h("input", { readonly: true, value: link, id: "connect-link", "aria-label": "Connect link", onfocus: (ev) => ev.target.select() });
      const copied = h("span", { class: "small muted", "aria-live": "polite" });
      const copy = h("button", { type: "button", class: "pri", text: "Copy link", onclick: async () => {
        try { await navigator.clipboard.writeText(link); copied.textContent = "Copied"; } catch { input.focus(); input.select(); copied.textContent = "Press Ctrl+C / Cmd+C to copy"; }
      } });
      const fp = hubId.match(/.{4}/g).join(" ");
      connectBox.replaceChildren(h("section", { class: "card", id: "connect-panel" },
        h("h3", { text: "Connect a workspace" }),
        p("Send this link to the person who runs the workspace. It works once."),
        field("Link", input),
        h("div", { class: "row" }, copy, copied),
        p("Valid until " + new Date(expires).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) + " (30 minutes), one use."),
        h("div", { class: "row" },
          h("button", { type: "button", text: "Make a new one", onclick: (ev) => void mintLink(ev.target) }),
          h("button", { type: "button", text: "Done", onclick: () => connectBox.replaceChildren() })),
        h("details", {}, h("summary", { text: "Technical details" }),
          p("Hub key fingerprint " + fp + " is part of the link, so nobody has to read it out."),
          p("Prefer two fields? Hub address " + (endpoint ?? "(this hub)") + " and code " + code + " work too."))));
      input.focus(); input.select();
    } catch { connectBox.replaceChildren(h("div", { class: "banner bad", role: "alert", text: d.say("unavailable") })); }
    finally { button.disabled = false; }
  }
  function workspaces() {
    return [h("div", { class: "row" }, h("h2", { class: "grow", text: "Workspaces" }),
      h("button", { type: "button", class: "pri", id: "connect-workspace", text: "Connect a workspace", onclick: (ev) => void mintLink(ev.target) })),
      connectBox, ...data.workspaces.map((w) => h("article", { class: "card" },
      h("div", { class: "row" }, h("h3", { class: "grow", text: workspaceName(w.workspace_id) }), chip(w.mode === "full" ? "Full" : "Selective", w.mode === "full" ? "full" : "sel")),
      p(w.mode === "full" ? "Hub can read everything" : "Hub can read shared items and summaries"),
      chip((w.online ?? (w.status === "bound" || w.status === "online")) ? "Connection online" : "Connection offline", (w.online ?? (w.status === "bound" || w.status === "online")) ? "ok" : "warn"),
      p("Binding: " + txt(w.status ?? "unknown")), w.mode === "selective" ? p("Personal · owner only · " + personName(w.owner_alias)) : null)),
      !data.workspaces.length ? p("No workspaces yet.") : null];
  }
  const CAPS = [["choose_model", "Choose the AI model"], ["credential", "Use stored credentials"], ["git_push", "Push code to git"], ["network", "Use the network"], ["pty", "Run terminal sessions"], ["serve_http", "Serve web pages"], ["spawn_agent", "Start other agents"]];
  const MAX_COMPANY = 16;
  const official = () => data.policy.official_url;
  const ids = (text) => [...new Set(text.split(/[\s,]+/).filter(Boolean))].sort();
  const byUrl = (a, b) => (a.url < b.url ? -1 : 1);
  const canon = (v) => JSON.stringify(v, (k, x) => (x && typeof x === "object" && !Array.isArray(x) ? Object.fromEntries(Object.entries(x).sort(([a], [b]) => (a < b ? -1 : 1))) : x));
  // One draft of the whole policy for the Marketplaces tab: it survives redraws until it is saved or discarded, and
  // one Save sends it as the whole object with the version it was based on (a stale version is a conflict).
  let ui = null, mpNote = null, mpFocus = null, mpBusy = false;
  const mpRoot = h("div", {});
  // ``base`` is what the draft started from ({version, policy}); Save sends base.version, and nothing moves it under a
  // draft with unsaved edits (a refresh from another panel only shows "changed elsewhere").
  const fromPolicy = (cur) => ({
    base: JSON.parse(JSON.stringify({ version: cur.version, policy: cur.policy })), overwrites: [], confirmOver: false,
    ...draftOf(cur.policy),
  });
  const draftOf = (pol) => ({
    markets: JSON.parse(JSON.stringify(pol.markets)),
    onlyAddons: pol.addons.allow !== null, allow: (pol.addons.allow ?? []).join(" "), block: pol.addons.block.join(" "),
    limit: pol.ceiling !== null, caps: new Set(pol.ceiling ?? []),
    confirm: null, editKeys: null, err: null, form: { addr: "", k1: "", k2: "" },
  });
  // The admin's own changes (draft against its base) laid onto the latest policy. Where someone else changed the same
  // thing differently the admin's value wins but is listed in ``overwrites`` and needs an explicit confirm.
  function rebase(base, mine, latest) {
    const shortKey = (k) => "key " + k.slice(0, 8) + "…" + k.slice(-4);
    const over = [], same = (a, b) => canon(a) === canon(b);
    const listMerge = (b, m, l) => { const B = new Set(b), M = new Set(m), out = new Set(l); for (const x of M) if (!B.has(x)) out.add(x); for (const x of B) if (!M.has(x)) out.delete(x); return [...out].sort(); };
    const field = (label, b, m, l) => {
      if (same(m, b)) return l;
      if (Array.isArray(b) && Array.isArray(m) && Array.isArray(l)) return listMerge(b, m, l);
      if (same(l, b) || same(l, m)) return m;
      over.push(label); return m;
    };
    const map = (p) => new Map(p.markets.map((x) => [x.url, x])), B = map(base), M = map(mine), res = map(latest);
    for (const [url, m] of M) {
      const b = B.get(url), l = res.get(url);
      if (b === undefined) { if (l !== undefined && !same(l, m)) over.push("Marketplace " + url + " was added elsewhere with other keys"); res.set(url, m); }
      else if (!same(m, b)) { if (l === undefined) over.push("Marketplace " + url + " was removed elsewhere; your key change adds it back"); else if (!same(l, b) && !same(l, m)) { const back = m.keys.filter((k) => !l.keys.includes(k)), gone = l.keys.filter((k) => !m.keys.includes(k)); over.push("Keys of " + url + " were changed elsewhere" + (back.length ? "; your version brings back " + back.map(shortKey).join(", ") : "") + (gone.length ? "; it drops " + gone.map(shortKey).join(", ") : "")); } res.set(url, m); }
    }
    for (const [url, b] of B) if (!M.has(url)) { const l = res.get(url); if (l !== undefined && !same(l, b)) over.push("Keys of " + url + " were changed elsewhere before you removed it"); res.delete(url); }
    const policy = {
      markets: [...res.values()].sort(byUrl),
      addons: { allow: field("Which add-ons may be installed", base.addons.allow, mine.addons.allow, latest.addons.allow), block: field("Blocked add-ons", base.addons.block, mine.addons.block, latest.addons.block) },
      ceiling: field("Permission limit", base.ceiling, mine.ceiling, latest.ceiling),
    };
    return { policy, over };
  }
  const toPolicy = () => ({
    markets: [...ui.markets].sort(byUrl),
    addons: { allow: ui.onlyAddons ? ids(ui.allow) : null, block: ids(ui.block) },
    ceiling: ui.limit ? [...ui.caps].sort() : null,
  });
  const mpDirty = () => ui !== null && canon(toPolicy()) !== canon(ui.base.policy);
  const syncMp = () => { if (ui === null || !mpDirty()) ui = fromPolicy(data.policy); };
  const mpPaint = () => {
    mpRoot.replaceChildren(...buildMp().filter(Boolean));
    if (mpFocus) { document.getElementById(mpFocus)?.focus(); mpFocus = null; }
  };
  const mpFail = (field, msg) => { ui.err = { field, msg }; mpFocus = "mp-" + field; mpPaint(); };
  const bad = (field) => (ui.err?.field === field ? { "aria-invalid": "true", "aria-describedby": "mp-err" } : {});
  async function saveMp() {
    mpBusy = true; mpPaint();
    try {
      const r = await d.api("PUT", "/admin/market-policy", { base_version: ui.base.version, policy: toPolicy() });
      if (r.status === 200) {
        data.policy = { ...data.policy, ...r.body }; ui = fromPolicy(data.policy);
        mpNote = { kind: "ok", text: "Saved. Every connected workspace gets the new rules (version " + r.body.version + ")." };
        await load();
      } else if (r.status === 409) {
        const g = await d.api("GET", "/admin/market-policy");
        if (g.status === 200) {
          data.policy = g.body;
          const { policy, over } = rebase(ui.base.policy, toPolicy(), g.body.policy);
          const keep = { form: ui.form };
          ui = { ...fromPolicy(g.body), ...draftOf(policy), ...keep, overwrites: over, confirmOver: false };
          mpNote = { kind: "warn", conflict: true, text: "Someone else changed these rules while you were editing (they are now version " + g.body.version + "). Nothing was saved. Your own changes are laid on top of their version below: check them, then save." + (over.length ? " Some of your changes replace theirs; confirm that below before saving." : "") };
        } else mpNote = { kind: "bad", text: d.say(g.body?.error ?? "unavailable") };
      } else if (r.status === 400) {
        mpNote = { kind: "bad", text: "The hub did not accept these rules. Check the marketplace addresses and public keys (a key is 87 characters). Nothing was changed." };
      } else mpNote = { kind: "bad", text: d.say(r.body?.error ?? "unavailable") };
    } catch { mpNote = { kind: "bad", text: d.say("unavailable") }; }
    mpBusy = false; mpFocus = "mp-note"; mpPaint();
  }
  function mpRow(m, i) {
    const isOff = m.class === "official", name = isOff ? "official marketplace" : txt(m.url);
    const editing = ui.editKeys?.url === m.url, confirming = ui.confirm === m.url;
    return h("article", { class: "list-row" },
      h("div", { class: "row" },
        h("div", { class: "grow" }, h("b", { text: isOff ? "Official marketplace" : txt(m.url) }), isOff ? h("p", { class: "small muted", text: txt(m.url) }) : null,
          h("div", { class: "row" }, chip(isOff ? "Official" : "Company", isOff ? "ok" : ""),
            isOff ? null : chip(m.keys.length + (m.keys.length === 1 ? " accepted public key" : " accepted public keys")))),
        confirming ? null : h("div", { class: "row" },
          isOff ? null : h("button", { type: "button", text: "Edit keys", "aria-label": "Edit keys of " + name, onclick: () => { ui.editKeys = { url: m.url, k1: m.keys[0] ?? "", k2: m.keys[1] ?? "" }; mpFocus = "mp-ek1"; mpPaint(); } }),
          h("button", { type: "button", class: "danger", text: "Remove", "aria-label": "Remove " + name, id: "mp-rm-" + i, onclick: () => { ui.confirm = m.url; mpFocus = "mp-rm-yes"; mpPaint(); } }))),
      confirming ? h("div", { class: "banner warn", role: "alert" },
        p("Remove the " + name + " for every workspace on this hub? Nobody can install from it once you save."),
        h("div", { class: "row" },
          h("button", { type: "button", class: "danger", id: "mp-rm-yes", text: "Remove from all workspaces", onclick: () => { ui.markets = ui.markets.filter((x) => x.url !== m.url); ui.confirm = null; mpFocus = "mp-add-official"; mpPaint(); } }),
          h("button", { type: "button", text: "Cancel", onclick: () => { ui.confirm = null; mpFocus = "mp-rm-" + i; mpPaint(); } }))) : null,
      editing ? h("form", { onsubmit: (ev) => {
        ev.preventDefault();
        const keys = [...new Set([ui.editKeys.k1.trim(), ui.editKeys.k2.trim()].filter(Boolean))].sort();
        if (!keys.length) { ui.err = { field: "ek1", msg: "A company marketplace needs at least one public key." }; mpFocus = "mp-ek1"; mpPaint(); return; }
        ui.markets = ui.markets.map((x) => (x.url === m.url ? { ...x, keys } : x)); ui.editKeys = null; ui.err = null; mpFocus = "mp-rm-" + i; mpPaint();
      } },
        h("div", { class: "form-grid mp-keys" },
          field("Current public key", h("input", { id: "mp-ek1", maxlength: 100, autocomplete: "off", value: ui.editKeys.k1, ...bad("ek1"), oninput: (ev) => { ui.editKeys.k1 = ev.target.value; } })),
          field("Next public key (optional; both are accepted while you change keys)", h("input", { id: "mp-ek2", maxlength: 100, autocomplete: "off", value: ui.editKeys.k2, oninput: (ev) => { ui.editKeys.k2 = ev.target.value; } })),
          h("button", { class: "pri", type: "submit", text: "Use these keys" }),
          h("button", { type: "button", text: "Cancel", onclick: () => { ui.editKeys = null; ui.err = null; mpFocus = "mp-rm-" + i; mpPaint(); } })),
        ui.err?.field === "ek1" ? h("p", { id: "mp-err", class: "small", role: "alert", text: ui.err.msg }) : null) : null);
  }
  function buildMp() {
    const company = ui.markets.filter((m) => m.class === "company");
    const hasOfficial = ui.markets.some((m) => m.url === official());
    const atLimit = company.length >= MAX_COMPANY;
    const dirty = mpDirty();
    const radio = (name, checked, label, onchange, id) => h("label", { class: "check" }, h("input", { type: "radio", name, checked, id, onchange }), label);
    const area = (id, label, key, help, disabled) => [h("label", { for: id, text: label }),
      h("textarea", { id, rows: 2, maxlength: 4000, disabled, oninput: (ev) => { ui[key] = ev.target.value; if (key === "block") warn(); touch(); } }), p(help)];
    const warnEl = h("div", {});
    const saveBtn = h("button", { class: "pri", type: "button", id: "mp-save", text: "Save rules", disabled: !dirty || mpBusy || (ui.overwrites.length > 0 && !ui.confirmOver), onclick: () => void saveMp() });
    const dirtyBox = h("span", {});
    const touch = () => { const now = mpDirty(); saveBtn.disabled = !now || mpBusy || (ui.overwrites.length > 0 && !ui.confirmOver); dirtyBox.replaceChildren(...(now ? [chip("Unsaved changes", "warn")] : [])); };
    const warn = () => {
      const blocksDashboard = ids(ui.block).some((x) => x === "dashboard" || x.startsWith("dashboard@"));
      warnEl.replaceChildren(blocksDashboard ? h("div", { class: "banner warn", role: "alert", text: "This blocks the dashboard. It stops working in every workspace on this hub until you remove the rule." })
        : p("Blocking a built-in add-on such as the dashboard pauses it in every workspace until the rule is removed."));
    };
    const allowArea = area("mp-allow", "Allowed add-on names", "allow", "Names separated by spaces or commas, for example: wiki notes. An empty list permits no add-on. A blocked add-on stays blocked even if it is listed here.", !ui.onlyAddons);
    const blockArea = area("mp-block", "Blocked add-ons", "block", "Add-on names, or name@1.2.3 for one version, separated by spaces or commas. Example: wiki wiki@1.2.3", false);
    allowArea[1].value = ui.allow; blockArea[1].value = ui.block; warn(); touch();
    const note = mpNote ? h("div", { class: "banner " + mpNote.kind, id: "mp-note", tabindex: "-1", role: mpNote.kind === "ok" ? "status" : "alert" },
      h("div", { text: mpNote.text }),
      mpNote.conflict ? h("button", { type: "button", text: "Load latest rules and drop my edits", onclick: () => { ui = fromPolicy(data.policy); mpNote = null; mpFocus = "mp-add-addr"; mpPaint(); } }) : null) : null;
    const moved = dirty && data.policy.version !== ui.base.version
      ? h("div", { class: "banner warn", role: "status", text: "These rules were changed elsewhere (now version " + data.policy.version + "). Your draft started from version " + ui.base.version + ". Saving will be refused and your changes will be laid on top of the latest rules for you to check." }) : null;
    const over = ui.overwrites.length ? h("div", { class: "banner warn", role: "alert" },
      p("Saving would replace changes someone else made:"), h("ul", {}, ui.overwrites.map((t) => h("li", { text: txt(t) }))),
      h("label", { class: "check" }, h("input", { type: "checkbox", id: "mp-confirm-over", checked: ui.confirmOver, onchange: (ev) => { ui.confirmOver = ev.target.checked; touch(); } }), "I have read this: use my version of these")) : null;
    return [note, moved, over,
      p("These rules apply to every workspace connected to this hub. Installing an add-on still needs a person's review and signature. A signature on a marketplace shows who published it, not that the code is safe."),
      h("section", { class: "card" }, h("h3", { text: "Allowed marketplaces" }),
        ui.markets.length ? ui.markets.map(mpRow) : p("No marketplaces are allowed. Nothing can be installed from a marketplace until you allow one."),
        !hasOfficial ? h("button", { type: "button", id: "mp-add-official", text: "Allow the official marketplace", onclick: () => { ui.markets.push({ url: official(), class: "official", keys: [] }); mpFocus = "mp-add-official"; mpPaint(); } }) : null),
      h("section", { class: "card" }, h("h3", { text: "Add your company marketplace" }),
        p("Ask whoever runs your marketplace for the address of its repository and the public key it signs with. Only add-ons signed with an accepted key are installable. Allowing a company marketplace here does not make it installable in workspaces where outside marketplaces are not available."),
        p(company.length + " of " + MAX_COMPANY + " company marketplaces." + (atLimit ? " The limit is reached; remove one to add another." : "")),
        h("form", { onsubmit: (ev) => {
          ev.preventDefault();
          const addr = ui.form.addr.trim(), keys = [...new Set([ui.form.k1.trim(), ui.form.k2.trim()].filter(Boolean))].sort();
          if (!addr) return mpFail("add-addr", "Enter the address of the marketplace repository.");
          if (addr === official()) return mpFail("add-addr", "That is the official marketplace, which needs no key. Use \"Allow the official marketplace\" above.");
          if (ui.markets.some((m) => m.url === addr)) return mpFail("add-addr", "This address is already in the list. Use Edit keys on its row to change its keys.");
          if (!keys.length) return mpFail("add-k1", "Paste the public key this marketplace signs with.");
          ui.markets.push({ url: addr, class: "company", keys }); ui.form = { addr: "", k1: "", k2: "" }; ui.err = null; mpFocus = "mp-add-addr"; mpPaint();
        } },
          h("div", { class: "form-grid mp-form" },
            field("Marketplace repository address", h("input", { id: "mp-add-addr", maxlength: 300, autocomplete: "off", value: ui.form.addr, ...bad("add-addr"), oninput: (ev) => { ui.form.addr = ev.target.value; } })),
            field("Current public key", h("input", { id: "mp-add-k1", maxlength: 100, autocomplete: "off", value: ui.form.k1, ...bad("add-k1"), oninput: (ev) => { ui.form.k1 = ev.target.value; } })),
            field("Next public key (optional)", h("input", { id: "mp-add-k2", maxlength: 100, autocomplete: "off", value: ui.form.k2, oninput: (ev) => { ui.form.k2 = ev.target.value; } })),
            h("button", { class: "pri", type: "submit", text: "Add marketplace", disabled: atLimit })),
          p("A public key is 87 characters. The optional second key is for a key change: both are accepted."),
          ui.err && ui.err.field.startsWith("add-") ? h("p", { id: "mp-err", class: "small bad-text", role: "alert", text: ui.err.msg }) : null)),
      h("section", { class: "card" }, h("h3", { text: "Add-ons and permissions" }),
        h("fieldset", {}, h("legend", { text: "Which add-ons may be installed" }),
          radio("allow", !ui.onlyAddons, "Any add-on from an allowed marketplace", () => { ui.onlyAddons = false; mpFocus = "mp-any"; mpPaint(); }, "mp-any"),
          radio("allow", ui.onlyAddons, "Only these add-ons", () => { ui.onlyAddons = true; mpFocus = "mp-allow"; mpPaint(); }, "mp-only"), ...allowArea),
        h("fieldset", {}, h("legend", { text: "Add-ons that are never allowed" }), ...blockArea, warnEl),
        h("fieldset", {}, h("legend", { text: "Permissions an add-on may ask for" }),
          radio("limit", !ui.limit, "No permission limit", () => { ui.limit = false; mpFocus = "mp-nolimit"; mpPaint(); }, "mp-nolimit"),
          radio("limit", ui.limit, "Allow only the selected permissions", () => { ui.limit = true; mpFocus = "mp-limit"; mpPaint(); }, "mp-limit"),
          CAPS.map(([id, name]) => h("label", { class: "check" }, h("input", { type: "checkbox", checked: ui.caps.has(id), disabled: !ui.limit, onchange: (ev) => { if (ev.target.checked) ui.caps.add(id); else ui.caps.delete(id); mpPaint(); } }), name)),
          p("With a limit and nothing ticked, no add-on may use any of these permissions. Ticking a permission only makes it possible: a person still has to grant it."))),
      h("div", { class: "row" },
        saveBtn,
        dirty ? h("button", { type: "button", text: "Discard changes", onclick: () => { ui = fromPolicy(data.policy); mpNote = null; mpPaint(); } }) : null,
        dirtyBox),
      p("Rules version " + data.policy.version + ".")];
  }
  function marketplaces() {
    mpPaint();
    return [h("h2", { text: "Marketplaces" }), mpRoot];
  }
  function audit() {
    const records = [...(data.audit.records ?? [])].sort((a, b) => b.seq - a.seq);
    return [h("h2", { text: "Audit" }), p("The API returns up to 100 records per page. Outcomes are shown only when recorded."),
      table(["Time", "Actor", "What", "Outcome"], records.map(({ record, event: e }) => {
        const o = record?.o ?? {};
        const time = new Date(o.ts_ms);
        let what = o.type === "grant" ? txt(e.group) + " on " + workspaceName(e.workspace_id) + ": " + (e.role ? roleName(e.role) : "Revoked")
          : o.type === "membership" ? personName(e.user_id) + (e.added ? " added to " : " removed from ") + txt(e.group)
          : o.type === "market_policy" ? "Marketplace rules changed (version " + txt(e.version) + ")" : txt(e.verb ?? e.change ?? o.type);
        if ("before" in e || "after" in e) what += ": " + txt(e.before) + " → " + txt(e.after);
        const actor = o.type === "custodial" ? "Hub on behalf of " + personName(e.user_id) : personName(e.actor_user_id ?? "Hub operator");
        return [Number.isNaN(time.getTime()) ? "Not recorded" : time.toLocaleString(), actor, what, txt(e.outcome ?? "Not recorded")];
      }), "Recent access changes"), !records.length ? p("No access changes recorded.") : null];
  }
  function draw() {
    panels.replaceChildren(...[people, access, workspaces, marketplaces, audit].map((view, i) => h("section", {
      id: "admin-panel-" + i, role: "tabpanel", "aria-labelledby": "admin-tab-" + i, hidden: i !== selected,
    }, view())));
  }
  void load();
  return [h("h1", { text: "Admin" }), p("Groups managed here; external sync not configured"), message, tabs, panels];
}
