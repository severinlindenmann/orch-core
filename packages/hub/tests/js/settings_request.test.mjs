// Protocol 8.8 settings.changed from the hub page: signable only for 1-3 of the three timings, in range, on the workspace
// log, against the action this page sent; words come from this page.
import test from "node:test";
import assert from "node:assert/strict";
import { describe, checkBindings, settingsTermsOk } from "../../src/orch_hub/web/render.js";

const D = new Uint8Array(32);
const ev = (set) => ({ auth: "hub", type: "settings.changed", set });
const d = (e, log = "workspace", sent = { workspaceId: "w", action: { type: "settings.changed", log: "workspace", set: e.set } }) => describe({ log, event: e }, D, sent);

test("a settings change is signable and says it in plain words", () => {
  const r = d(ev({ grant_hours: 24, claim_ttl_min: 90 }));
  assert.equal(r.signable, true);
  assert.match(r.sentence, /Longest agent access 1 day/);
  assert.match(JSON.stringify(r.facts), /Ticket reserved for.*90 minutes/);
  assert.match(d(ev({ grant_hours: 72 })).sentence, /3 days/);
  assert.match(d(ev({ grant_hours: 5 })).sentence, /5 hours/);
});

test("not signable: a ticket log, an off-channel key, out of range, empty, extra field", () => {
  assert.equal(d(ev({ grant_hours: 24 }), "01J9ZP0000000000000000TKT1").signable, false);
  assert.equal(d(ev({ sign_methods: ["click"] })).signable, false);
  assert.equal(d(ev({ grant_hours: 500 })).signable, false);
  assert.equal(d(ev({ claim_ttl_min: 14 })).signable, false);
  assert.equal(d(ev({})).signable, false);
  assert.equal(d({ ...ev({ grant_hours: 24 }), repos: [] }).signable, false);
  assert.equal(settingsTermsOk({ grant_hours: 1.5 }), false);
  assert.equal(settingsTermsOk({ lease_ttl_min: 5 }), true);
});

test("bindings: the signed set must equal the set this page sent", () => {
  const me = { channelId: "c".repeat(32), keyPub: "k", userId: "u".repeat(32), hubId: "h".repeat(32) };
  const full = (e) => ({ workspace_id: "w", log: "workspace", event: { ...e, channel: me.channelId, channel_factor: "password+totp", hub_id: me.hubId, actor: { id: "u_" + me.userId, via: me.channelId }, channel_cert: { o: { channel_id: me.channelId, key_pub: "k", user_id: me.userId, factor: "password+totp" } } } });
  const sent = { workspaceId: "w", action: { type: "settings.changed", log: "workspace", set: { grant_hours: 24 } } };
  checkBindings(full(ev({ grant_hours: 24 })), me, sent);
  assert.throws(() => checkBindings(full(ev({ grant_hours: 168 })), me, sent), { code: "event_changed" });
  assert.throws(() => checkBindings(full(ev({ grant_hours: 24, lease_ttl_min: 5 })), me, sent), { code: "event_changed" });
});

test("a session that came by the rail switch is asked to sign in, in plain words, before it signs", async () => {
  const src = await (await import("node:fs/promises")).readFile(new URL("../../src/orch_hub/web/app.js", import.meta.url), "utf8");
  assert.match(src, /r\.body\.error === "reauth_required"\) return signInToSign\(/);
  assert.match(src, /"Sign in to " \+ verb/);
  assert.match(src, /confirm it is you with your password and a 6-digit code/);
});

test("the settings page refuses an expired link and does not claim the workspace wrote it", async () => {
  const src = await (await import("node:fs/promises")).readFile(new URL("../../src/orch_hub/web/app.js", import.meta.url), "utf8");
  assert.match(src, /exp \* 1000 > Date\.now\(\)/);
  assert.match(src, /This link has expired/);
  assert.match(src, /This link asks to change how long things are held/);
  assert.doesNotMatch(src, /"The workspace " \+ renderText\(mine\.name \?\? wsId\.slice\(0, 8\)\) \+ " asks to change/);
});
