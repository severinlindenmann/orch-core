// The page behind an invitation link (#invite=<token>): a new person joins, or a person resets their password and code.
// The token lives in the link's fragment; it is sent only in the body of the two POSTs below, never in a path or query.
// createElement and textContent only (see tests/js/lint.test.mjs).
import { sha256, unb64u, utf8 } from "./cj.js";
import { loadChannel, signProof } from "./channel.js";
import { h, text } from "./ui.js";
import { SIGN_IN_NOTE, totpPanel } from "./totp.js";

export const PROOF_LABEL = "orch/v2/invite-proof|";
export const MIN_PASSWORD = 10;
export const NEED_PROOF = "Open this link in a browser where you already approve, or ask your admin for a recovery link.";
export const DEAD_LINK = "This link does not work. It may have been used already or it has expired. Ask your admin for a new one.";
const WORDS = {
  bad_invite: DEAD_LINK,
  proof_required: NEED_PROOF,
  weak_password: "Choose a password of at least " + MIN_PASSWORD + " characters.",
  rate_limited: "Too many tries. Wait a moment, then try again.",
  unavailable: "The hub cannot be reached. Try again in a moment.",
};

/** What this browser's approving device signs to show it is the person's: SHA-256(label || challenge || SHA-256(token)),
 * signed with the ordinary channel proof (`signProof`, the same primitive as an approval). The hub checks the same bytes. */
export async function proofDigest(challenge, token) {
  const tokenHash = await sha256(utf8(token));
  const raw = unb64u(challenge, 32);
  const label = utf8(PROOF_LABEL);
  const msg = new Uint8Array(label.length + raw.length + tokenHash.length);
  msg.set(label);
  msg.set(raw, label.length);
  msg.set(tokenHash, label.length + raw.length);
  return sha256(msg);
}

/** The proof object for POST /invite/redeem, or null when this browser holds no key of this person. */
export async function makeProof(info, token, me, sign = signProof) {
  if (!info.needs_proof || !info.challenge || !me?.pair?.privateKey || me.userId !== info.user_id) return null;
  const digest = await proofDigest(info.challenge, token);
  return { channel_id: me.channelId, challenge: info.challenge, sig: await sign(me.pair.privateKey, digest) };
}

/** The page. `d`: `api(method, path, body)`, `say(code)` (optional), `signIn()` (go to the sign-in page), `finished()`
 * (the link was used: drop it from the address bar), and for tests `loadChannel` / `signProof`. */
export function inviteView(token, d) {
  const box = h("div", { class: "invite", "aria-live": "polite" });
  const say = (code) => WORDS[code] ?? d.say?.(code) ?? "Something went wrong. Try again.";
  const banner = (kind, msg) => h("div", { class: "banner " + kind, role: kind === "bad" ? "alert" : "status", text: msg });
  const paint = (...nodes) => box.replaceChildren(...nodes.flat().filter(Boolean));
  const field = (label, input) => h("div", {}, h("label", { text: label }), input);

  async function start(note) {
    paint(h("p", { class: "muted", text: "Checking your link…" }));
    let r;
    try { r = await d.api("POST", "/invite/peek", { token }); } catch { r = { status: 0, body: { error: "unavailable" } }; }
    if (r.status !== 200) return paint(h("h1", { text: "This link does not work" }), banner("bad", say(r.body?.error ?? "unavailable")));
    const info = r.body;
    const me = info.needs_proof ? await (d.loadChannel ?? loadChannel)() : null;
    const canProve = !info.needs_proof || (me?.userId === info.user_id && !!me?.pair);
    form(info, me, canProve, note);
  }

  function form(info, me, canProve, note) {
    const name = text(info.name ?? "");
    const join = info.kind === "join";
    const pw = h("input", { id: "invite-pw", type: "password", autocomplete: "new-password", required: true, minlength: MIN_PASSWORD });
    const again = h("input", { id: "invite-pw2", type: "password", autocomplete: "new-password", required: true, minlength: MIN_PASSWORD });
    const status = h("div", { "aria-live": "polite" });
    const go = h("button", { class: "pri", type: "submit", text: join ? "Create my account" : "Save new password", disabled: !canProve });
    const onsubmit = async (ev) => {
      ev.preventDefault();
      if (pw.value !== again.value) return status.replaceChildren(banner("bad", "The two passwords are not the same."));
      if (pw.value.length < MIN_PASSWORD) return status.replaceChildren(banner("bad", say("weak_password")));
      go.disabled = true;
      const body = { token, password: pw.value };
      try {
        if (info.needs_proof) body.proof = await makeProof(info, token, me, d.signProof ?? signProof);
        if (info.needs_proof && !body.proof) { go.disabled = false; return status.replaceChildren(banner("bad", NEED_PROOF)); }
      } catch { go.disabled = false; return status.replaceChildren(banner("bad", NEED_PROOF)); }
      let r;
      try { r = await d.api("POST", "/invite/redeem", body); } catch { r = { status: 0, body: { error: "unavailable" } }; }
      pw.value = again.value = "";
      body.password = "";
      if (r.status === 200) return secret(r.body);
      const code = r.body?.error ?? "unavailable";
      if (code === "proof_required") return start(NEED_PROOF); // the challenge is used up: ask for a fresh one
      if (code === "bad_invite") return paint(h("h1", { text: "This link does not work" }), banner("bad", DEAD_LINK));
      go.disabled = false;
      status.replaceChildren(banner("bad", say(code)));
    };
    paint(
      h("h1", { text: join ? "Welcome, " + name : "Reset your password and code" }),
      join ? h("p", { class: "muted", text: "Your admin invited you. Choose your own password. Nobody else will see it." })
        : h("p", { class: "muted", text: "This is for " + name + ". Choose a new password. You will also get a new code for your authenticator app." }),
      info.recovery && !join ? banner("warn", "Every device you approved on will be removed. You can set them up again after you sign in.") : null,
      note ? banner("bad", note) : null,
      !canProve ? banner("warn", NEED_PROOF) : null,
      h("form", { onsubmit }, field("New password (" + MIN_PASSWORD + " or more characters)", pw), field("Type it again", again),
        status, h("div", { class: "row top" }, go)),
    );
  }

  function secret(done) {
    d.finished?.();
    const code = String(done.totp_secret ?? "");
    paint(
      h("h1", { text: done.kind === "join" ? "Your account is ready" : "Your password and code are changed" }),
      banner("ok", "Add your sign-in code to your phone now. " + SIGN_IN_NOTE),
      h("dl", { class: "facts" },
        h("dt", { text: "Your name for signing in" }), h("dd", { id: "invite-name", text: text(done.name ?? "") }),
        ),
      totpPanel(done.totp_uri, code, { id: "invite-secret" }),
      done.kind === "join" ? h("p", { class: "muted", text: "You do not have access to any workspace yet. Your admin adds you to groups afterwards." }) : null,
      done.recovery ? h("p", { class: "muted", text: "Your devices were removed. Set them up again once you are signed in." }) : null,
      // A new person is already signed in by the redeem; a reset or recovery signs in with the new password and code.
      done.kind === "join" && !done.recovery && d.openHub
        ? h("div", { class: "row top" }, h("button", { class: "pri", type: "button", id: "open-hub", text: "Open the hub", onclick: () => d.openHub() }), h("span", { class: "small muted", text: "You are already signed in." }))
        : h("div", { class: "row top" }, h("button", { class: "pri", type: "button", text: "Continue to sign in", onclick: () => d.signIn?.() })),
    );
  }

  const view = [box];
  view.ready = start();
  return view;
}
