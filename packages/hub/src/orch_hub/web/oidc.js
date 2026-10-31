// Company login (HUB-PE-04): the sign-in button, the callback, the admin settings card and the account card. Plain words,
// createElement and textContent only. Company login opens a SESSION; it never makes a signing key (that still needs the hub
// password and a 6-digit code), and the page says so where it matters.
import { busyButton } from "./feel.js";
import { chip, h, text } from "./ui.js";

const PROVIDER = { entra: "Microsoft", google: "Google" };
export const providerName = (p) => PROVIDER[p] ?? "your company";

export const INVITE_WORDS = {
  bad_invite: "This link does not work. It may have been used already or it has expired. Ask your admin for a new one.",
  oidc_already_linked: "This company account is already used on this hub. Ask your hub admin.",
};
export const WELCOME = "Welcome. You are signed in with your company account. To approve things from this browser, ask your hub admin for a set-up link: it lets you choose a hub password and a 6-digit code once.";
export const KEY_WORDS = "You signed in with your company login. To approve things from this browser, ask your hub admin for a set-up link: it lets you choose a hub password and a 6-digit code once.";
export const WHY = {
  oidc_failed: "Company login did not work. Try again, or sign in with your password.",
  oidc_not_allowed: "This company account is not set up for this hub. Ask your hub admin.",
  oidc_not_ready: "Run the test login first, and tick that your company login asks for a second step.",
  oidc_rate_limited: "Too many tries. Wait a minute, then try again.",
  unavailable: "Company login is not available on this hub.",
  malformed: "Check the values. The Microsoft tenant is a long id (not common), the Google domain looks like acme.example.",
};

/** Only an address the hub itself built for the IdP: https, or http on this computer. */
export function safeLoginUrl(u) {
  try {
    const x = new URL(String(u));
    return x.protocol === "https:" || (x.protocol === "http:" && (x.hostname === "localhost" || x.hostname === "127.0.0.1")) ? x.href : null;
  } catch { return null; }
}

/** `?code=..&state=..` from the IdP, or null. */
export function callbackParams(search) {
  const p = new URLSearchParams(String(search ?? ""));
  const code = p.get("code"), state = p.get("state");
  const ok = (v) => typeof v === "string" && v.length > 0 && v.length <= 2048;
  return ok(code) && ok(state) ? { code, state } : null;
}

const FINISH = { signin: "/oidc/finish", link: "/oidc/link/finish", test: "/admin/oidc/test/finish", invite: "/oidc/invite/finish" };
const WHERE = { signin: "", link: "#/account", test: "#/hub-settings", invite: "" };

/** The page the IdP sent the browser back to. The hub says what the sealed login cookie is for (no browser storage), then
 *  the right finish route is called. Returns `{purpose, ok, body, where}`; the caller shows it and removes the address. */
export async function finishCallback(api, search) {
  const q = callbackParams(search);
  if (!q) await api("POST", "/oidc/cancel", {}); // the provider returned an error, or the person cancelled: drop the login cookie
  const pending = q ? await api("GET", "/oidc/pending") : null;
  const purpose = pending?.status === 200 && FINISH[pending.body?.purpose] ? pending.body.purpose : null;
  if (!q || !purpose) return { purpose: purpose ?? "signin", ok: false, body: { error: "oidc_failed" }, where: WHERE[purpose ?? "signin"] };
  const r = await api("POST", FINISH[purpose], q);
  return { purpose, ok: r.status === 200, body: r.body, where: WHERE[purpose] };
}

/** Go to the company's sign-in page. `path` is the start route of the purpose; `body` its input. */
export async function begin(api, purpose, body = {}) {
  const path = { signin: "/oidc/start", link: "/oidc/link/start", test: "/admin/oidc/test/start" }[purpose];
  const r = await api("POST", path, body);
  const url = r.status === 200 ? safeLoginUrl(r.body.url) : null;
  if (!url) return { ok: false, error: r.body?.error ?? "oidc_failed" };
  globalThis.location.assign(url);
  return { ok: true };
}

/** The button on the sign-in page; null when company login is off. */
export function signInButton(options, go) {
  if (!options?.enabled) return null;
  const b = h("button", { type: "button", class: "pri", id: "company-login", text: "Sign in with " + providerName(options.provider) });
  b.addEventListener("click", () => busyButton(b, go));
  return h("div", { class: "row top" }, b, h("span", { class: "small muted", text: "or use your name, password and code below" }));
}

const field = (label, input) => h("div", {}, h("label", { for: input.id, text: label }), input);
const opt = (v, label, sel) => h("option", { value: v, text: label, selected: sel === v });

/** Admin: Hub settings > Company login. `actions.save(body)`, `actions.test()`, `actions.copy(text)`. */
export function companyCard(data, actions, opts = {}) {
  const cur = data.configured ? data : { provider: "entra", client_id: "", tenant: "", domain: "", mode: "invited", idp_mfa: false, enabled: false, tested: false };
  const provider = h("select", { id: "oidc-provider" }, opt("entra", "Microsoft work account", cur.provider), opt("google", "Google Workspace", cur.provider));
  const clientId = h("input", { id: "oidc-client", value: cur.client_id ?? "", autocomplete: "off", maxlength: 200 });
  const tenant = h("input", { id: "oidc-tenant", value: cur.tenant ?? "", autocomplete: "off", placeholder: "00000000-0000-0000-0000-000000000000" });
  const domain = h("input", { id: "oidc-domain", value: cur.domain ?? "", autocomplete: "off", placeholder: "acme.example" });
  const secret = h("input", { id: "oidc-secret", type: "password", autocomplete: "new-password", placeholder: data.has_secret ? "Saved. Type a new one only to replace it." : "" });
  const mode = h("select", { id: "oidc-mode" }, opt("invited", "Only people I already added", cur.mode), opt("company", (cur.domain ? "Anyone at " + cur.domain : "Anyone at my company") + " (they join with no access)", cur.mode));
  const mfa = h("input", { id: "oidc-mfa", type: "checkbox", checked: cur.idp_mfa === true });
  const on = h("input", { id: "oidc-on", type: "checkbox", checked: cur.enabled === true, disabled: !cur.tested });
  const out = h("div", { class: "small", role: "status", "aria-live": "polite" });
  let failOpen = () => {};
  const show = (ok, words) => { out.textContent = words; out.className = "small " + (ok ? "ok" : "bad"); if (!ok) failOpen(); };
  const body = () => ({ provider: provider.value, client_id: clientId.value.trim(), tenant: tenant.value.trim(), domain: domain.value.trim(),
    mode: mode.value, idp_mfa: mfa.checked, enabled: on.checked, ...(secret.value ? { client_secret: secret.value } : {}) });
  const tenantRow = field("Directory ID (Microsoft only)", tenant), domainRow = field("Company domain (Google)", domain);
  const sync = () => { const e = provider.value === "entra"; tenantRow.hidden = !e; domainRow.hidden = e; };
  provider.addEventListener("change", sync);
  const save = h("button", { type: "button", class: "pri", id: "oidc-save", text: "Save" });
  save.addEventListener("click", () => busyButton(save, async () => { const r = await actions.save(body()); secret.value = ""; show(r.status === 200, r.status === 200 ? "Saved." : WHY[r.body?.error] ?? "That did not work."); }));
  const test = h("button", { type: "button", id: "oidc-test", text: "Test the connection", disabled: !data.configured || !data.has_secret });
  test.addEventListener("click", () => busyButton(test, async () => { const r = await actions.test(); if (!r.ok) show(false, WHY[r.error] ?? "The test could not start. Save first."); }));
  const secure = data.secure_address !== false && !!data.redirect_uri;
  const state = cur.enabled ? chip("On", "ok") : cur.tested ? chip("Tested") : chip("Off");
  const intro = "People sign in with their company account instead of a hub password. It opens a session only: approving still needs a key from the hub password and code.";
  const fields = [
    secure ? null : h("p", { class: "small bad", role: "alert", text: "Company login needs a secure address (https), or this server's own localhost. A plain office IP address is not accepted by Microsoft or Google." }),
    data.redirect_uri ? h("div", { class: "small" }, "Paste this return address into Microsoft or Google: ", h("code", { text: data.redirect_uri }),
      " ", h("button", { type: "button", class: "small", text: "Copy", onclick: () => actions.copy?.(data.redirect_uri) })) : null,
    field("Work account type", provider), field("Application ID", clientId), tenantRow, domainRow,
    field("Secret", secret), field("Who may use it", mode),
    h("label", { class: "small" }, mfa, " My company login already asks for a second step (like a code or an app)"),
    h("label", { class: "small" }, on, " Turn company login on", cur.tested ? null : h("span", { class: "muted", text: " (test the connection first)" })),
    h("details", { class: "info" }, h("summary", { text: "Good to know" }),
      h("p", { class: "small muted", text: "Microsoft may leave out the sign-in method (amr) unless optional claims are set up; then the second-step tick is your statement, not something the hub can check." }),
      h("p", { class: "small muted", text: "For Microsoft, add the optional claim auth_time to the app registration (Token configuration, ID token). Without it the test login and linking fail, on purpose." }),
      h("p", { class: "small muted", text: "With \"Anyone at\" your company, guests of your Microsoft tenant also join as members. They can see nothing until you give them a group." })),
    h("div", { class: "row top" }, save, test), out];
  if (!opts.inside) {
    const card = h("div", { class: "card", "data-card": "company-login" }, h("h2", { text: "Company login" }),
      h("div", { class: "row" }, h("div", { class: "grow" }, h("b", { text: "Microsoft or Google work account" }), h("div", { class: "small muted", text: intro })), state), ...fields);
    sync();
    return card;
  }
  // inside Hub settings: one closed line (title, one plain line, On/Off, Set up) that opens into the fields; open by default only when a test failed
  const card = h("details", { class: "company-login", "data-card": "company-login", open: opts.open === true || data.test_failed === true },
    h("summary", { class: "company-summary-row" }, h("b", { text: "Company login" }), h("span", { class: "small muted", text: " · sign in with a Microsoft or Google work account" }), " ", state,
      h("span", { class: "link small", text: cur.enabled ? " Change" : " Set up" })),
    h("p", { class: "small muted", text: "Copy these from your Microsoft or Google admin page. " + intro }), ...fields);
  failOpen = () => { card.open = true; };
  sync();
  return card;
}

/** Account: link or remove my company login. `a.link({password,totp})`, `a.unlink()`. */
export function accountCard(me, a) {
  if (!me?.enabled && !me?.linked) return null;
  const out = h("div", { class: "small", role: "status", "aria-live": "polite" });
  const show = (ok, words) => { out.textContent = words; out.className = "small " + (ok ? "ok" : "bad"); };
  if (me.linked) {
    const rm = h("button", { type: "button", text: "Remove company login" });
    rm.addEventListener("click", () => busyButton(rm, async () => { const r = await a.unlink(); show(r.status === 200, r.status === 200 ? "Removed. Company sessions ended." : "That did not work."); }));
    return h("section", { class: "card", id: "company-login-card" }, h("h2", { text: "Company login" }),
      h("p", { text: "Linked to your " + providerName(me.linked.provider) + " account." }), h("div", { class: "row top" }, rm), out);
  }
  const pw = h("input", { id: "oidc-link-pw", type: "password", autocomplete: "current-password" });
  const code = h("input", { id: "oidc-link-code", inputmode: "numeric", autocomplete: "one-time-code", maxlength: 6 });
  const go = h("button", { type: "button", class: "pri", text: "Link my " + providerName(me.provider) + " account" });
  go.addEventListener("click", () => busyButton(go, async () => {
    const r = await a.link({ password: pw.value, totp: code.value.trim() });
    pw.value = code.value = "";
    if (!r.ok) show(false, r.error === "bad_credentials" ? "The password or the code was not right. A code works once." : WHY[r.error] ?? "That did not work.");
  }));
  return h("section", { class: "card", id: "company-login-card" }, h("h2", { text: "Company login" }),
    h("p", { class: "small muted", text: "Link your company account to sign in with it. Linking asks for your hub password and a fresh code." }),
    field("Password", pw), field("6-digit code", code), h("div", { class: "row top" }, go), out);
}

export { text };
