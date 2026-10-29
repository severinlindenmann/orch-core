// The authenticator step shared by the first-admin page and an invited person's page: a QR of the otpauth:// URI,
// and the same secret as text behind "Can't scan?" (nothing is removed from what the person can see).
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

export function totpPanel(uri, secret, { id = "new-totp" } = {}) {
  let qr = null;
  try { qr = String(uri ?? "").startsWith("otpauth://") ? qrSvg(uri, { label: "Sign-in code for your authenticator app" }) : null; } catch { qr = null; }
  const key = h("details", { class: "totp-key", open: qr ? null : true },
    h("summary", { text: qr ? "Can't scan? Show the key" : "Authenticator key" }),
    h("p", { class: "small muted", text: "Type this key into your authenticator app. It is shown only once." }),
    h("div", { class: "row" }, h("div", { class: "fp grow", id, text: String(secret ?? "") }), copyKey(String(secret ?? ""))));
  return h("div", { class: "totp" }, qr ? h("div", { class: "qr" }, qr) : null, key);
}
