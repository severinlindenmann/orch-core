// Prints a key, a digest and the channel_proof the browser code makes over it, for the Python cross-check
// (tests/test_static_site.py verifies it with the hub's own verifier). Not a test file.
import { generateKey, signProof, signRegistration } from "../../src/orch_hub/web/channel.js";
import { b64u } from "../../src/orch_hub/web/cj.js";

const g = await generateKey();
const digest = crypto.getRandomValues(new Uint8Array(32));
const proof = await signProof(g.pair.privateKey, digest);
const o = {
  v: 2, suite: 2, kind: "hub.channel_register", hv: 0, hub_id: "ab".repeat(16), user_id: "cd".repeat(16), channel_kind: "browser",
  key_pub: g.keyPub, attestation: "session", nonce: b64u(crypto.getRandomValues(new Uint8Array(32))), expires_ms: 1_790_000_120_000, dev: true,
};
const reg = await signRegistration(g.pair.privateKey, o, { keyPub: g.keyPub, userId: o.user_id, hubId: null }, 1_790_000_000_000);
console.log(JSON.stringify({ key_pub: g.keyPub, channel_id: g.channelId, digest: Array.from(digest, (b) => b.toString(16).padStart(2, "0")).join(""), proof, reg }));
