import { h, text, when } from "./ui.js";

export function actorText(actor) {
  if (actor?.kind === "agent") return "agent:" + text(actor.session);
  if (actor?.kind === "person") return text(actor.name ?? actor.id);
  return actor?.kind === "host" ? "host" : "orch hub";
}

export function attribution(row) {
  const detail = row.detail ?? row;
  if (detail.requested_by_agent_session && detail.approved_by) {
    return "requested by agent:" + text(detail.requested_by_agent_session) + ", approved by " + text(detail.approved_by);
  }
  return actorText(row.actor);
}

const info = (label, body) => h("details", { class: "info" }, h("summary", { "aria-label": label, text: "i" }), h("p", { class: "small muted", text: body }));

/** What the page shows: a person's name, "An agent" (the session behind the "i"), never an agent under a person's name. */
export function attributionView(row) {
  const detail = row.detail ?? row;
  if (detail.requested_by_agent_session && detail.approved_by) {
    return h("p", { class: "muted" }, "An agent asked, " + text(detail.approved_by) + " approved ",
      info("Which agent", "agent:" + text(detail.requested_by_agent_session)));
  }
  if (row.actor?.kind === "agent") return h("p", { class: "muted" }, "An agent ", info("Which agent", actorText(row.actor)));
  return h("p", { class: "muted", text: actorText(row.actor) });
}

export function csvCell(value) {
  let s = String(value ?? "");
  if (/^[\t\r]/.test(s) || /^[=+@-]/.test(s.trimStart())) s = "'" + s;
  return '"' + s.replaceAll('"', '""') + '"';
}

export function toCsv(rows) {
  return [["Time", "Event", "Actor", "Activity", "Workspace"], ...rows.map((r) => [
    new Date(r.ts_ms).toISOString(), r.type, attribution(r), r.text, r.workspace_name ?? r.workspace_id,
  ])].map((cells) => cells.map(csvCell).join(",")).join("\r\n") + "\r\n";
}

export function filterRows(rows, filters = {}) {
  return rows.filter((r) => (!filters.workspace_id || r.workspace_id === filters.workspace_id)
    && (!filters.person || (r.actor?.kind === "person" && r.actor.id === filters.person) || r.subject_user === filters.person || r.approved_by_id === filters.person)
    && (!filters.type || r.type === filters.type)
    && (!filters.since_ms || r.ts_ms >= Number(filters.since_ms))
    && (!filters.until_ms || r.ts_ms <= Number(filters.until_ms)));
}

function download(rows) {
  const url = URL.createObjectURL(new Blob([toCsv(rows)], { type: "text/csv;charset=utf-8" }));
  const link = h("a", { href: url, download: "activity.csv" });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

export function activityView(data, actions = {}, now = Date.now()) {
  const filters = { ...data.filters };
  const rows = data.rows ?? [];
  const list = h("div", { "aria-live": "polite" });
  const filtered = () => filterRows(rows, filters);
  const paint = () => {
    const visible = filtered();
    list.replaceChildren(...(visible.length ? visible.map((r) => h("article", { class: "card" },
      h("p", { text: text(r.text) }), attributionView(r),
      h("p", { class: "small", text: [when(r.ts_ms, now), text(r.workspace_name)].filter(Boolean).join(" · ") }),
    )) : [h("p", { text: "No activity matches these filters." })]));
  };
  const select = (label, key, entries) => {
    const input = h("select", { "aria-label": label, onchange: () => { filters[key] = input.value; paint(); } },
      h("option", { value: "", text: "All" }), entries.map(([value, name]) => h("option", { value, text: text(name) })));
    input.value = filters[key] ?? "";
    return h("label", {}, label, input);
  };
  const date = (label, key, end) => {
    const input = h("input", { type: "date", "aria-label": label, onchange: () => {
      filters[key] = input.value ? String(new Date(input.value + (end ? "T23:59:59.999" : "T00:00:00")).getTime()) : "";
      paint();
    } });
    if (filters[key]) {
      const d = new Date(Number(filters[key]));
      input.value = [d.getFullYear(), String(d.getMonth() + 1).padStart(2, "0"), String(d.getDate()).padStart(2, "0")].join("-");
    }
    return h("label", {}, label, input);
  };
  const unique = (pairs) => [...new Map(pairs).entries()];
  const form = h("form", { onsubmit: (ev) => { ev.preventDefault(); actions.filter?.({ ...filters }); } },
    select("Workspace", "workspace_id", unique((data.workspaces ?? rows.filter((r) => r.workspace_id)).map((r) => [r.workspace_id, r.name ?? r.workspace_name ?? r.workspace_id]))),
    select("Person", "person", unique(data.users ? data.users.map((u) => [u.user_id, u.name]) : rows.filter((r) => r.actor?.kind === "person").map((r) => [r.actor.id, r.actor.name]))),
    select("Kind of event", "type", (data.types ?? unique(rows.map((r) => [r.type, r.type]))).map((entry) => Array.isArray(entry) ? [entry[0], entry[1].replaceAll(/[._]/g, " ")] : [entry, (data.type_words ?? {})[entry] ?? entry.replaceAll(/[._]/g, " ")])),
    date("From", "since_ms", false), date("Until", "until_ms", true),
    h("button", { type: "submit", text: "Apply filters" }));
  paint();
  return h("section", {}, h("h1", { text: "Activity" }), form,
    h("div", { class: "row" },
      h("button", { type: "button", text: "Export CSV", onclick: async () => (actions.save ?? download)(actions.exportAll ? await actions.exportAll({ ...filters }) : filtered()) }),
      info("About the export", "The file holds every row that matches the filters you applied, not only the ones on this page.")), list,
    data.before ? h("button", { type: "button", text: "Load older", onclick: () => actions.older?.(data.before) }) : null);
}
