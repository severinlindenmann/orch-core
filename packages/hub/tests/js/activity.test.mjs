import test from "node:test";
import assert from "node:assert/strict";
import { activityView, actorText, attribution, csvCell, filterRows, toCsv } from "../../src/orch_hub/web/activity.js";
import { parseRoute } from "../../src/orch_hub/web/route.js";

class Node {
  constructor(tag) { this.tag = tag; this.nodeType = 1; this.children = []; this.attrs = {}; this.listeners = {}; this._text = ""; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(k, v) { this.listeners[k] = v; }
  set textContent(v) { this._text = v; }
  get textContent() { return this._text + this.children.map((n) => n.textContent).join(""); }
  all() { return [this, ...this.children.flatMap((n) => n.all())]; }
}
globalThis.document = { createElement: (tag) => new Node(tag), createTextNode: (text) => { const n = new Node("text"); n.textContent = text; return n; } };
const rows = [
  { id: "e:2", ts_ms: 2000, type: "user.disabled", actor: { kind: "person", id: "p", name: "Pat" }, text: "disabled a person", workspace_id: "w", workspace_name: "Work" },
  { id: "e:1", ts_ms: 1000, type: "change.proposed", actor: { kind: "agent", session: "s", name: "Never show this" }, text: "proposed a change", workspace_id: "x" },
];

test("CSV quotes every cell and guards spreadsheet formulas", () => {
  for (const s of ["=cmd", "+1", "-1", "@x", " =x", "\tx", "\rx", " \t=x", "\n@x"]) assert.equal(csvCell(s), '"\'' + s + '"');
  assert.equal(csvCell('a"b'), '"a""b"');
  assert.equal(csvCell("plain"), '"plain"');
  assert.equal(csvCell(null), '""');
});

test("typed actors and joint attribution never name an agent as a person", () => {
  assert.equal(actorText(rows[1].actor), "agent:s");
  assert.equal(actorText(rows[0].actor), "Pat");
  assert.equal(actorText({ kind: "hub" }), "orch hub");
  assert.equal(actorText({ kind: "host" }), "host");
  assert.equal(attribution({ requested_by_agent_session: "s", approved_by: "Pat" }), "requested by agent:s, approved by Pat");
  assert.doesNotMatch(toCsv(rows), /Never show this/);
  assert.deepEqual(parseRoute("#/activity"), { kind: "activity" });
});

test("workspace, person, type and date filters combine", () => {
  assert.deepEqual(filterRows(rows, { workspace_id: "w", person: "p", type: "user.disabled", since_ms: 2000, until_ms: 2000 }), [rows[0]]);
  assert.deepEqual(filterRows(rows, { person: "s" }), []);
  assert.deepEqual(filterRows(rows, { until_ms: 1500 }), [rows[1]]);
  assert.deepEqual(filterRows(rows, { since_ms: 3000 }), []);
});

test("export receives exactly the rows currently shown; older retains the filters", () => {
  let exported, older, applied;
  const view = activityView({ rows, before: "1000:e:1" }, {
    export: (r) => { exported = r; }, older: (...args) => { older = args; }, filter: (f) => { applied = f; },
  }, 4000);
  const workspace = view.all().find((n) => n.attrs["aria-label"] === "Workspace");
  workspace.value = "w";
  workspace.listeners.change();
  const button = (name) => view.all().find((n) => n.tag === "button" && n.textContent === name);
  button("Export CSV").listeners.click();
  assert.deepEqual(exported, [rows[0]]);
  assert.equal(toCsv(exported).split("\r\n").length, 3);
  assert.doesNotMatch(toCsv(exported), /proposed a change/);
  assert.doesNotMatch(view.textContent, /proposed a change/);
  button("Load older").listeners.click();
  assert.deepEqual(older, ["1000:e:1", { workspace_id: "w" }]);
  view.all().find((n) => n.tag === "form").listeners.submit({ preventDefault() {} });
  assert.deepEqual(applied, { workspace_id: "w" });
  workspace.value = "absent";
  workspace.listeners.change();
  assert.match(view.textContent, /No activity matches/);
  button("Export CSV").listeners.click();
  assert.deepEqual(exported, []);
});
