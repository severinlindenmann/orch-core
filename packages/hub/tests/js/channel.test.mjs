import test from "node:test";
import assert from "node:assert/strict";
import { ChannelError, LABEL_PROOF, LABEL_REG, checkChallenge, generateKey, signProof, signRegistration } from "../../src/orch_hub/web/channel.js";
import { b64u, cj, unb64u, utf8 } from "../../src/orch_hub/web/cj.js";

const NOW = 1_790_000_000_000;
async function verify(keyPubB64, sigB64, msg) {
  const pub = await crypto.subtle.importKey("raw", unb64u(keyPubB64, 65), { name: "ECDSA", namedCurve: "P-256" }, false, ["verify"]);
  return crypto.subtle.verify({ name: "ECDSA", hash: "SHA-256" }, pub, unb64u(sigB64, 64), msg);
}
const challenge = (g, over = {}) => ({
  v: 2, suite: 2, kind: "hub.channel_register", hv: 0, hub_id: "ab".repeat(16), user_id: "cd".repeat(16), channel_kind: "browser",
  key_pub: g.keyPub, attestation: "session", nonce: b64u(new Uint8Array(32).fill(7)), expires_ms: NOW + 120_000, dev: true, ...over,
});

test("the key is non-extractable, 65-byte public, 64-byte raw r||s signatures", async () => {
  const g = await generateKey();
  await assert.rejects(crypto.subtle.exportKey("raw", g.pair.privateKey));
  await assert.rejects(crypto.subtle.exportKey("pkcs8", g.pair.privateKey));
  assert.equal(unb64u(g.keyPub).length, 65);
  assert.match(g.channelId, /^[0-9a-f]{32}$/);
  const digest = new Uint8Array(32).fill(9);
  const proof = await signProof(g.pair.privateKey, digest);
  assert.equal(unb64u(proof).length, 64);
  assert.ok(await verify(g.keyPub, proof, new Uint8Array([...utf8(LABEL_PROOF), ...digest])));
  assert.equal(await verify(g.keyPub, proof, new Uint8Array([...utf8(LABEL_REG), ...digest])), false); // one label, one meaning
});

test("signProof only takes a 32-byte digest", async () => {
  const g = await generateKey();
  for (const bad of [new Uint8Array(31), new Uint8Array(33), "00".repeat(32), [1, 2], undefined]) await assert.rejects(signProof(g.pair.privateKey, bad), ChannelError);
});

test("the registration challenge is checked field by field before signing", async () => {
  const g = await generateKey();
  const me = { keyPub: g.keyPub, userId: "cd".repeat(16), hubId: null, dev: true };
  const signed = await signRegistration(g.pair.privateKey, challenge(g), me, NOW);
  assert.ok(await verify(g.keyPub, signed.sig, utf8(LABEL_REG + cj(signed.o))));
  const other = await generateKey();
  for (const over of [
    { key_pub: other.keyPub }, { user_id: "ee".repeat(16) }, { channel_kind: "phone" }, { attestation: "pairing-code" }, { dev: false },
    { kind: "hub.channel_cert" }, { expires_ms: NOW + 10 * 60_000 }, { nonce: "AA" }, { hub_id: "zz" }, { extra: 1 }, { hv: 1 },
  ]) await assert.rejects(signRegistration(g.pair.privateKey, challenge(g, over), me, NOW), ChannelError, JSON.stringify(over));
  // once the hub is pinned, a challenge from another hub is refused
  assert.throws(() => checkChallenge(challenge(g), { ...me, hubId: "11".repeat(16) }, NOW), ChannelError);
});

test("the challenge's dev flag must match the serving hub's own mode (a real hub is not a dev hub)", async () => {
  const g = await generateKey();
  const base = { keyPub: g.keyPub, userId: "cd".repeat(16), hubId: null };
  // real hub (dev:false) accepts its own dev:false challenge, and refuses a dev:true one
  const real = { ...base, dev: false };
  const signed = await signRegistration(g.pair.privateKey, challenge(g, { dev: false }), real, NOW);
  assert.ok(await verify(g.keyPub, signed.sig, utf8(LABEL_REG + cj(signed.o))));
  await assert.rejects(signRegistration(g.pair.privateKey, challenge(g, { dev: true }), real, NOW), ChannelError);
  // dev hub: the reverse
  await assert.rejects(signRegistration(g.pair.privateKey, challenge(g, { dev: false }), { ...base, dev: true }, NOW), ChannelError);
  // an unknown hub mode never signs
  assert.throws(() => checkChallenge(challenge(g), base, NOW), ChannelError);
});
