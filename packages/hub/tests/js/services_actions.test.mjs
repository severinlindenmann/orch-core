// HUB-SV-02: the console's check of a prepared service action (digest vector made by the Python helper) and the buttons.
import test from "node:test";
import assert from "node:assert/strict";

class Node_ {
  constructor(tag, text) { this.tag = tag; this.nodeType = tag === "#text" ? 3 : 1; this.children = []; this.attrs = {}; this._text = text ?? ""; this.className = ""; this.listeners = {}; }
  append(...kids) { for (const k of kids) this.children.push(k && k.nodeType ? k : new Node_("#text", String(k))); }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener(k, f) { this.listeners[k] = f; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this.tag === "#text" ? this._text : this._text + this.children.map((c) => c.textContent).join(""); }
  all() { return [this, ...this.children.flatMap((c) => c.all())]; }
}
globalThis.document = { createElement: (t) => new Node_(t), createTextNode: (t) => new Node_("#text", t), getElementById: () => null };
const { checkServiceAction } = await import("../../src/orch_hub/web/service_actions.js");
const { servicesView } = await import("../../src/orch_hub/web/services.js");

const V = {"ctx": {"kind": "orch.hub.service.action/v1", "hub_id": "abababababababababababababababab", "host_id": "vps1", "part": "relay", "action": "restart", "target_version": null, "nonce": "cccccccccccccccccccccccccccccccc", "iat": 1800000000123, "channel_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "user_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "text_sha256": "c79797be49f69eff6aca358363a5cb80694cdf34d336ae13af76045c3a98a8c2"}, "digest": "0ddc87b5259c4d77982f73bebc8a4f0391f3b29592bf5bbacb6905d7f79cca22", "text": "Restart Relay on vps1. It is unavailable for a few seconds."};
const want = { part: "relay", action: "restart", userId: "a".repeat(32), channelId: "b".repeat(32) };
const code = async (p, w = want) => { try { await checkServiceAction(p, w); return "ok"; } catch (e) { return e.message; } };

test("the digest the helper made equals the one this page recomputes", async () => {
  assert.equal(await code(V), "ok");
});
test("another part, action, person or key is refused", async () => {
  assert.equal(await code(V, { ...want, part: "publish" }), "event_changed");
  assert.equal(await code(V, { ...want, action: "stop" }), "event_changed");
  assert.equal(await code(V, { ...want, userId: "c".repeat(32) }), "event_changed");
  assert.equal(await code(V, { ...want, channelId: "c".repeat(32) }), "event_changed");
});
test("a text that is not the bound one, a changed field or an extra key is refused", async () => {
  assert.equal(await code({ ...V, text: "Stop Relay on vps1." }), "event_changed");
  assert.equal(await code({ ...V, ctx: { ...V.ctx, iat: V.ctx.iat + 1 } }), "event_changed");
  assert.equal(await code({ ...V, ctx: { ...V.ctx, extra: 1 } }), "malformed");
  assert.equal(await code({ ...V, digest: "00".repeat(32) }), "event_changed");
  assert.equal(await code({ ...V, ctx: { ...V.ctx, target_version: "9.9" } }), "event_changed");
  assert.equal(await code({}), "malformed");
  assert.equal(await code({ ...V, text: 5 }), "malformed");
});

const NOW = Date.now();
const data = (helper) => ({
  hub: { state: "up", version: "0.1.0", started_ms: NOW - 1000 },
  relay: { name: "relay", state: "down", url: "http://r", last_ok_ms: null },
  publish: { name: "publish", state: "up", url: "http://p", checked_ms: NOW },
  workspaces: { count: 0, online: 0 },
  helper,
});
const find = (nodes, f) => nodes.flatMap((n) => n.all()).filter(f);

test("buttons appear only for parts the helper manages (the hub row has no Stop), and call the action", () => {
  const calls = [];
  const view = servicesView(data({ state: "up", host_id: "vps1", parts: [{ name: "relay", label: "Relay" }, { name: "hub", label: "Hub" }], hub_busy: false }), { service: (n, v) => calls.push([n, v]), copy: () => {} }, NOW);
  const btns = find(view, (n) => n.attrs["data-svc"]);
  assert.deepEqual(btns.map((b) => b.attrs["data-svc"]).sort(), ["hub|restart", "relay|restart", "relay|start", "relay|stop"]);
  btns.find((b) => b.attrs["data-svc"] === "relay|start").listeners.click?.();
  assert.equal(find(view, (n) => n.attrs["data-svc"] === "publish|restart").length, 0);
  assert.ok(find(view, (n) => n.attrs.id === "how-publish").length === 1); // publish keeps the copy-the-command help
  assert.ok(find(view, (n) => n.attrs.id === "how-relay").length === 0);
});
test("no helper: no buttons, the command help stays; helper down: a note; busy hub: buttons off", () => {
  const none = servicesView(data({ state: "not_set_up", parts: [] }), { service: () => {}, copy: () => {} }, NOW);
  assert.equal(find(none, (n) => n.attrs["data-svc"]).length, 0);
  assert.equal(find(none, (n) => n.attrs.id === "how-relay").length, 1);
  const down = servicesView(data({ state: "down", parts: [] }), { service: () => {}, copy: () => {} }, NOW);
  assert.match(find(down, (n) => n.attrs.id === "helper-note")[0].textContent, /not running/);
  const busy = servicesView(data({ state: "up", parts: [{ name: "relay" }], hub_busy: true }), { service: () => {}, copy: () => {} }, NOW);
  assert.ok(find(busy, (n) => n.attrs["data-svc"]).every((b) => b.getAttribute("disabled") !== null));
});

test("a Logs button per managed part calls logs(name)", () => {
  const calls = [];
  const view = servicesView(data({ state: "up", parts: [{ name: "relay" }], hub_busy: false }), { service: () => {}, logs: (n) => calls.push(n), copy: () => {} }, NOW);
  const b = find(view, (n) => n.attrs["data-logs"]);
  assert.equal(b.length, 1);
  b[0].listeners.click();
  assert.deepEqual(calls, ["relay"]);
});

const { logsDialog, serviceDialog, stopLabel, summarize, withKey, HUB_STOP_NOTE, LOG_NOTE } = await import("../../src/orch_hub/web/service_actions.js");
const nodes = (list) => list.flatMap((n) => n.all());

test("the Logs dialog shows log text literally, with the note, and copies exactly that text", () => {
  const copied = [];
  const dlg = logsDialog("relay", "09:00 <b>x</b> [redacted]", { copy: (t) => copied.push(t), onClose: () => {} });
  const all = nodes(dlg);
  const pre = all.find((n) => n.attrs.id === "log-text");
  assert.equal(pre.textContent, "09:00 <b>x</b> [redacted]");
  assert.equal(all.filter((n) => n.tag === "b").length, 0); // no element was created from the log
  assert.equal(all.find((n) => n.attrs.id === "log-note").textContent, LOG_NOTE);
  all.find((n) => n.attrs.id === "log-copy").listeners.click();
  assert.equal(copied.length, 1);
  assert.ok(copied[0].endsWith("\n09:00 <b>x</b> [redacted]"));
});

test("Logs are offered only where the page is given a logs action (the admin page)", () => {
  const withLogs = servicesView(data({ state: "up", parts: [{ name: "relay" }] }), { service: () => {}, logs: () => {}, copy: () => {} }, NOW);
  const without = servicesView(data({ state: "up", parts: [{ name: "relay" }] }), { service: () => {}, copy: () => {} }, NOW);
  assert.equal(find(withLogs, (n) => n.attrs["data-logs"]).length, 1);
  assert.equal(find(without, (n) => n.attrs["data-logs"]).length, 0);
});

const fmt = (d) => d.toISOString().slice(11, 16); // tests read UTC, so they do not depend on the machine's zone
const NOW_D = new Date("2026-10-30T13:00:00Z");
const J = (t, who, msg) => "2026-10-30T" + t + ":01+0000 lab-b " + who + ": " + msg;

test("the hub row has no Stop (restart and logs only); relay and publish keep Stop", () => {
  const view = servicesView(data({ state: "up", parts: [{ name: "hub" }, { name: "relay" }, { name: "publish" }], hub_busy: false }), { service: () => {}, logs: () => {}, copy: () => {} }, NOW);
  const ids = find(view, (n) => n.attrs["data-svc"]).map((b) => b.attrs["data-svc"]);
  assert.ok(!ids.includes("hub|stop"));
  assert.ok(ids.includes("hub|restart") && ids.includes("relay|stop") && ids.includes("publish|stop"));
});

test("no key: the action opens the register step and resumes after it; with a key it runs at once", () => {
  const log = [];
  withKey(() => false, (resume) => { log.push("register"); resume(); }, () => log.push("action"));
  assert.deepEqual(log, ["register", "action"]);
  const log2 = [];
  withKey(() => true, () => log2.push("register"), () => log2.push("action"));
  assert.deepEqual(log2, ["action"]);
  const log3 = [];
  withKey(() => false, () => log3.push("register"), () => log3.push("action")); // cancelled: never resumed
  assert.deepEqual(log3, ["register"]);
});

test("stopping the Hub opens one dialog with the note and a single red button; relay and publish show no note", () => {
  const mk = (name, verb) => serviceDialog(name, verb, "The sentence.", { sign: Object.assign(new Node_("button", "Stop the Hub"), { className: "dan" }), onCancel: () => {} });
  const hub = nodes(mk("hub", "stop"));
  assert.equal(hub.find((n) => n.tag === "h1").textContent, "Stop the Hub?");
  assert.equal(hub.find((n) => n.attrs.id === "hub-stop-note").textContent, HUB_STOP_NOTE);
  assert.equal(hub.filter((n) => n.tag === "button").length, 2); // Cancel and the one sign button
  assert.equal(hub.filter((n) => n.attrs.id === "hub-stop-confirm").length, 0); // no second confirmation
  for (const [n, v] of [["relay", "stop"], ["publish", "stop"], ["hub", "restart"]]) assert.equal(nodes(mk(n, v)).filter((x) => x.attrs.id === "hub-stop-note").length, 0, n + v);
  assert.equal(stopLabel("relay", "stop"), "Stop Relay");
  assert.equal(stopLabel("hub", "restart"), "Restart Hub");
  assert.equal(stopLabel("hub", "stop"), "Stop the Hub");
});

test("summarize: systemd lines become plain events, benign lines fold, other lines stay", () => {
  const raw = [
    J("12:04", "systemd[1]", "Starting orch publish..."),
    J("12:04", "systemd[1]", "Started orch publish."),
    J("12:30", "systemd[1]", "Stopping orch publish..."),
    J("12:30", "systemd[1]", "orch-x-publish.service: Deactivated successfully."),
    J("12:30", "systemd[1]", "Stopped orch publish."),
    J("12:40", "systemd[1]", "orch-x-publish.service: Failed with result 'exit-code'."),
    J("12:41", "orch-x-publish[321]", "ERROR: disk full"),
    "-- Boot abc --",
  ].join("\n");
  assert.deepEqual(summarize(raw, { fmt, now: NOW_D }).map((e) => (e.time ? e.time + " " : "") + e.text), ["12:04 Started", "12:30 Stopped", "12:40 Stopped unexpectedly", "12:41 ERROR: disk full", "-- Boot abc --"]);
  assert.deepEqual(summarize("", { fmt, now: NOW_D }), []);
  assert.deepEqual(summarize(null), []);
  assert.equal(summarize(J("12:04", "systemd[1]", "Failed to start orch publish."), { fmt, now: NOW_D })[0].text, "Could not start");
  const old = summarize("2026-10-29T09:00:00+0000 h systemd[1]: Started x.", { fmt, now: NOW_D })[0];
  assert.match(old.time, /^09:00, /); // another day carries its date
});

test("the Logs dialog: list by default, raw lines in a closed fold, recent-activity heading, empty text", () => {
  const raw = J("12:04", "systemd[1]", "Started orch publish.") + "\n" + J("12:05", "x[1]", "<b>hi</b>");
  const copied = [];
  const all = nodes(logsDialog("publish", raw, { copy: (t) => copied.push(t), onClose: () => {}, fmt, now: NOW_D }));
  assert.equal(all.find((n) => n.tag === "h1").textContent, "Publish: recent activity");
  assert.equal(all.find((n) => n.attrs.id === "log-events").children.length, 2);
  assert.equal(all.filter((n) => n.tag === "b").length, 0);
  const fold = all.find((n) => n.attrs.id === "log-raw");
  assert.equal(fold.tag, "details");
  assert.equal(fold.attrs.open, undefined); // closed
  assert.equal(all.find((n) => n.attrs.id === "log-text").textContent, raw);
  assert.match(all.find((n) => n.attrs.id === "log-span").textContent, /^Last 24 hours\.$/); // an unrecognised line: no "nothing went wrong"
  all.find((n) => n.attrs.id === "log-copy").listeners.click();
  assert.ok(copied[0].endsWith("\n" + raw) && copied[0].includes("last 24 hours"));
  const ok = nodes(logsDialog("relay", J("12:04", "systemd[1]", "Started orch relay."), { copy: () => {}, onClose: () => {}, fmt, now: NOW_D }));
  assert.match(ok.find((n) => n.attrs.id === "log-span").textContent, /Nothing went wrong/);
  const many = Array.from({ length: 300 }, () => J("12:04", "systemd[1]", "Started orch relay.")).join("\n");
  const cut = nodes(logsDialog("relay", many, { copy: () => {}, onClose: () => {}, fmt, now: NOW_D }));
  assert.equal(cut.find((n) => n.attrs.id === "log-span").textContent, "Last 300 lines."); // cut by the helper: no reassurance
  const empty = nodes(logsDialog("relay", "", { copy: () => {}, onClose: () => {}, fmt, now: NOW_D }));
  assert.equal(empty.find((n) => n.attrs.id === "log-empty").textContent, "Nothing happened in the last 24 hours.");
  assert.equal(empty.filter((n) => n.attrs.id === "log-text").length, 0);
});

test("the helper-down note names the real user unit when it is known", () => {
  const withUnit = servicesView(data({ state: "down", parts: [], helper_unit: "orch-1e0740-hub-helper.service" }), { service: () => {}, copy: () => {} }, NOW);
  assert.match(find(withUnit, (n) => n.attrs.id === "helper-note")[0].textContent, /systemctl --user start orch-1e0740-hub-helper\.service/);
  const notSet = servicesView(data({ state: "not_set_up", parts: [], helper_unit: "orch-1e0740-hub-helper.service" }), { service: () => {}, copy: () => {} }, NOW);
  assert.equal(find(notSet, (n) => n.attrs.id === "helper-note").length, 1);
  const noUnit = servicesView(data({ state: "down", parts: [] }), { service: () => {}, copy: () => {} }, NOW);
  assert.match(find(noUnit, (n) => n.attrs.id === "helper-note")[0].textContent, /orch-hub helper/);
});
