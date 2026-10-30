// The authenticator step shared by the first-admin page and an invited person's page: a QR of the otpauth:// URI,
// and the same secret as text right under it, with Copy (the QR already carries it; a hidden key helped nobody).
// The key is rendered only here, on the one page that gets it from the server: never stored, logged, or put in a URL or title.
import { h } from "./ui.js";
import { qrSvg } from "./qr.js";

/** Copy next to the key, only where the browser allows it (https or localhost); elsewhere the key stays selectable. */
function copyKey(secret) {
  const clip = typeof navigator !== "undefined" ? navigator.clipboard : undefined;
  if (!clip || typeof clip.writeText !== "function") return null;
  const button = h("button", { type: "button", class: "copy-key", text: "Copy", onclick: async () => {
    try { await clip.writeText(secret); button.textContent = "Copied ✓"; } catch { button.textContent = "Could not copy. Select the key."; }
  } });
  return button;
}

/** The one sentence both account pages (first admin, invited person) tell about the app. */
export const SIGN_IN_NOTE = "You will need the app's 6-digit code each time you sign in.";

/** The one "gone after this page" sentence; each account page shows it beside its continue button. */
export const KEY_GONE_NOTE = "You will not see this key again after this page.";

export function totpPanel(uri, secret, { id = "new-totp" } = {}) {
  let qr = null;
  try { qr = String(uri ?? "").startsWith("otpauth://") ? qrSvg(uri, { label: "Sign-in code for your authenticator app" }) : null; } catch { qr = null; }
  const key = h("div", { class: "totp-key" },
    h("p", { class: "small muted", text: qr ? "Can't scan? Type this key into your app instead." : "Authenticator key" }),
    h("div", { class: "row" }, h("div", { class: "fp grow", id, text: String(secret ?? "") }), copyKey(String(secret ?? ""))));
  return h("div", { class: "totp" }, qr ? h("div", { class: "qr" }, qr) : null, key);
}
