// The authenticator step shared by the first-admin page and an invited person's page: a QR of the otpauth:// URI,
// and the same secret as text behind "Can't scan?" (nothing is removed from what the person can see).
import { h } from "./ui.js";
import { qrSvg } from "./qr.js";

export function totpPanel(uri, secret, { id = "new-totp", userId = null } = {}) {
  let qr = null;
  try { qr = String(uri ?? "").startsWith("otpauth://") ? qrSvg(uri, { label: "Sign-in code for your authenticator app" }) : null; } catch { qr = null; }
  const key = h("details", { class: "totp-key", open: qr ? null : true },
    h("summary", { text: qr ? "Can't scan? Show the key" : "Authenticator key" }),
    h("p", { class: "small muted", text: "Type this key into your authenticator app. It is shown only once." }),
    h("div", { class: "fp", id, text: String(secret ?? "") }),
    userId ? h("p", { class: "small muted" }, "Your user id (if your name does not work to sign in): ", h("span", { class: "fp", id: "new-user-id", text: String(userId) })) : null);
  return h("div", { class: "totp" }, qr ? h("div", { class: "qr" }, qr) : null, key);
}
