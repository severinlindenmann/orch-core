import { h, text, chip, when } from "./ui.js";
import { busyButton, emptyState } from "./feel.js";

// Plain words for what the hub can refuse (the codes are the hub's, the sentences are ours).
const WORDS = {
  bad_credentials: "The password or the code was not right. A code works once, so wait for the next one.",
  rate_limited: "Too many tries. Wait a few minutes, then try again.",
  weak_password: "The new password needs at least 10 characters.",
  same_password: "The new password must be different from the current one.",
  bad_code: "That code did not match the new authenticator.",
  no_pending: "There is no new authenticator waiting. Start again.",
  pending_expired: "The new authenticator ran out of time (10 minutes). Start again.",
  reauth_required: "Sign in again with your password and code to do this.",
  no_session: "Your sign-in ended. Sign in again.",
};
export const securityWords = (code) => WORDS[code] ?? "That did not work. Please try again.";

const labelled = (label, input) => h("div", {}, h("label", { for: input.id, text: label }), input);
const result = () => h("div", { class: "small", role: "status", "aria-live": "polite" });
function say(box, ok, words) {
  box.textContent = words;
  box.className = "small " + (ok ? "ok" : "bad");
}

function passwordCard(a) {
  const current = h("input", { id: "sec-pw-now", type: "password", autocomplete: "current-password", required: true });
  const code = h("input", { id: "sec-pw-code", inputmode: "numeric", autocomplete: "one-time-code", maxlength: 6, required: true });
  const fresh = h("input", { id: "sec-pw-new", type: "password", autocomplete: "new-password", required: true, minlength: 10 });
  const again = h("input", { id: "sec-pw-again", type: "password", autocomplete: "new-password", required: true, minlength: 10 });
  const out = result();
  const go = h("button", { class: "pri", type: "submit", text: "Change password" });
  const form = h("form", {
    onsubmit: (ev) => {
      ev.preventDefault();
      return busyButton(go, async () => {
        if (fresh.value.length < 10) return say(out, false, WORDS.weak_password);
        if (fresh.value !== again.value) return say(out, false, "The two new passwords are not the same.");
        const r = await a.password({ current_password: current.value, totp: code.value.trim(), new_password: fresh.value });
        if (r.status === 200) {
          current.value = code.value = fresh.value = again.value = "";
          return say(out, true, "Password changed. Other devices were signed out.");
        }
        code.value = "";
        say(out, false, securityWords(r.body?.error));
      });
    },
  }, labelled("Current password", current), labelled("6-digit code from your authenticator app", code),
    labelled("New password (10 or more characters)", fresh), labelled("New password again", again),
    h("div", { class: "row top" }, go), out);
  return h("section", { class: "card", id: "sec-password" }, h("h2", { text: "Change your password" }),
    h("p", { class: "small muted", text: "You need your current password and a current code. Your phones and browsers stay as they are." }), form);
}

function authenticatorCard(a) {
  const pw = h("input", { id: "sec-auth-pw", type: "password", autocomplete: "current-password", required: true });
  const code = h("input", { id: "sec-auth-code", inputmode: "numeric", autocomplete: "one-time-code", maxlength: 6, required: true });
  const out = result();
  const box = h("div", {});
  const start = h("button", { class: "pri", type: "submit", text: "Start a new authenticator" });
  const startForm = h("form", {
    onsubmit: (ev) => {
      ev.preventDefault();
      return busyButton(start, async () => {
        const r = await a.begin({ password: pw.value, totp: code.value.trim() });
        pw.value = code.value = "";
        if (r.status !== 200 || typeof r.body?.secret !== "string") return say(out, false, securityWords(r.body?.error));
        out.textContent = "";
        confirmStep(r.body.secret);
      });
    },
  }, labelled("Current password", pw), labelled("Current 6-digit code", code), h("div", { class: "row top" }, start));

  function confirmStep(secret) {
    const next = h("input", { id: "sec-auth-new", inputmode: "numeric", autocomplete: "off", maxlength: 6, required: true });
    const turnOn = h("button", { class: "pri", type: "submit", text: "Turn on the new authenticator" });
    const copy = h("button", { type: "button", text: "Copy", "aria-label": "Copy the secret", onclick: () => a.copy?.(secret) });
    const confirm = h("form", {
      onsubmit: (ev) => {
        ev.preventDefault();
        return busyButton(turnOn, async () => {
          const r = await a.confirm(next.value.trim());
          if (r.status === 200) {
            box.textContent = "";
            box.append(startForm);
            return say(out, true, "The new authenticator is on. The old one no longer works. Other devices were signed out.");
          }
          next.value = "";
          if (r.body?.error === "pending_expired" || r.body?.error === "no_pending") { box.textContent = ""; box.append(startForm); }
          say(out, false, securityWords(r.body?.error));
        });
      },
    }, h("p", { text: "Add this secret to your authenticator app as a new account. It is shown only now, and it works for 10 minutes. Until you turn it on, your old authenticator keeps working." }),
      h("div", { class: "row" }, h("code", { class: "fp", id: "sec-auth-secret", text: secret }), copy),
      labelled("6-digit code from the new account in your app", next), h("div", { class: "row top" }, turnOn));
    box.textContent = "";
    box.append(confirm);
  }

  box.append(startForm);
  return h("section", { class: "card", id: "sec-authenticator" }, h("h2", { text: "Use a new authenticator app" }),
    h("p", { class: "small muted", text: "Use this if you lost your phone or want to move to another app. You need your password and a current code first." }), box, out);
}

function signOutCard(a) {
  const out = result();
  const go = h("button", { text: "Sign out everywhere", onclick: () => busyButton(go, async () => {
    const r = await a.endAll();
    if (r.status !== 200) say(out, false, securityWords(r.body?.error));
  }) });
  return h("section", { class: "card", id: "sec-signout" }, h("h2", { text: "Sign out everywhere" }),
    h("p", { class: "small muted", text: "This signs you out here and on every other device. Your phones and browsers stay registered. You can sign in again with your password and code." }),
    h("div", { class: "row top" }, go), out);
}

function securityCards(data, a) {
  if (data.needsSignIn) {
    return [h("section", { class: "card", id: "sec-needs-sign-in" }, h("h2", { text: "Password and sign-in" }),
      h("p", { text: "You arrived from a workspace. To change your password or authenticator, sign in again with your password and code." }),
      h("div", { class: "row top" }, h("button", { class: "pri", text: "Sign in again", onclick: () => a.signInAgain() })))];
  }
  return [passwordCard(a), authenticatorCard(a), signOutCard(a)];
}

export function accountView(data, actions) {
  const channels = (data.channels ?? []).filter((c) => c.kind === "phone" || c.kind === "browser");
  const rows = channels.map((c) => {
    const kind = c.kind === "phone" ? "Phone" : "Browser";
    const status = ({ active: "Active", revoked: "Removed", expired: "Expired" })[c.status] ?? "Unavailable";
    const dates = [["Added", c.created_ms], ["Last used", c.last_used_ms]]
      .filter(([, ms]) => Number.isFinite(ms)).map(([label, ms]) => label + " " + when(ms)).join(" · ");
    const remove = c.status === "active" ? h("button", { text: "Remove", "aria-label": "Remove " + text(c.label || kind),
      onclick: () => busyButton(remove, () => actions.remove(c.channel_id)) }) : null;
    return h("div", { class: "list-row row" },
      h("div", { class: "grow" }, h("b", { text: text(c.label || kind) }),
        h("div", { class: "small muted", text: kind + " · " + status }),
        dates ? h("div", { class: "small muted", text: dates }) : null,
        c.status === "active" ? h("div", { class: "small", text: "After removal: It can no longer approve for you." }) : null),
      c.kind === "browser" && c.channel_id === data.thisBrowserId ? chip("This browser") : null, remove);
  });
  return [h("h1", { text: "Account" }),
    h("p", { text: "Signed in as " + text(data.user?.name ?? "") }),
    h("section", { class: "card" }, h("h2", { text: "My phones and browsers" }),
      rows.length ? rows : emptyState("account"),
      rows.length ? h("p", { class: "small muted", text: "To add a phone, open the phone app and choose Pair a phone." }) : null),
    ...(actions.security ? securityCards(data, actions.security) : [])];
}
