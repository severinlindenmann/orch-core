// HUB-WS-07: "New workspace on a server". A guide only: the hub shows steps and copyable commands. The setup runs on the
// server, started by its owner; the hub never connects to the server, never holds a key or the setup link, and makes
// the connect link (the existing single-use invite) only when the person presses the button of step 4.
import { h } from "./ui.js";

// Same closed patterns and same command as orch core (orch/setup/tunnel.py): ASCII, never a leading "-".
const USER = /^[A-Za-z0-9._][A-Za-z0-9._-]{0,63}$/;
const SERVER = /^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$/;
const IPV6 = /^[0-9A-Fa-f:]{2,39}$/;
export const SETUP_PORT = 47821;
export const validUser = (v) => typeof v === "string" && USER.test(v);
export const validServer = (v) => typeof v === "string" && (SERVER.test(v) || IPV6.test(v));
/** The tunnel command, or null when user or server fail their patterns (then nothing is shown or copied). */
export function tunnelCommand(user, server, port = SETUP_PORT) {
  if (!validUser(user) || !validServer(server) || !Number.isInteger(port) || port < 1 || port > 65535) return null;
  return "ssh -o ExitOnForwardFailure=yes -L 127.0.0.1:" + port + ":127.0.0.1:" + port + " " + user + "@" + server;
}
/** The installer command, only for an https URL on the official release origin that the caller says verifies signed releases. */
// TODO(install.sh): pin the official repo path (owner/repo/branch) too, once install.sh ships; today only the host is checked.
export const OFFICIAL_INSTALLER_HOST = "raw.githubusercontent.com";
export function installerCommand(installerUrl, verified) {
  if (verified !== true || typeof installerUrl !== "string" || !/^[A-Za-z0-9:/._-]+$/.test(installerUrl)) return null;
  try { const u = new URL(installerUrl); return u.protocol === "https:" && u.host === OFFICIAL_INSTALLER_HOST && !u.username && !u.password && !u.search && !u.hash ? "curl -LsSf " + installerUrl + " | sh" : null; }
  catch { return null; }
}

const code = (value) => h("code", { class: "mono", text: value });
function copyRow(getCommand, actions) {
  const shown = h("div", { class: "row" });
  const copy = h("button", { type: "button", text: "Copy" });
  const set = () => { const c = getCommand(); shown.textContent = ""; if (c) shown.append(code(c), copy); copy.disabled = !c; return c; };
  copy.addEventListener("click", async () => {
    const c = getCommand(); // checked again at the moment of copying
    if (!c) return void set();
    let ok = false;
    try { ok = !!(await actions.copy?.(c)); } catch { ok = false; }
    copy.textContent = ok ? "Copied" : "Could not copy. Select the command to copy it.";
  });
  return { shown, set };
}

/** opts: {installerUrl, installerVerified, connectView: async () => node, signInToConnect}. actions.copy(text) => bool. */
export function serverGuideView(actions = {}, opts = {}) {
  const step = (n, title, ...body) => h("li", {}, h("b", { text: title }), ...body);
  const installer = installerCommand(opts.installerUrl, opts.installerVerified);
  const s1 = installer ? copyRow(() => installer, actions) : null;
  s1?.set();
  const s2 = copyRow(() => "orch setup --no-browser", actions);
  s2.set();
  const user = h("input", { id: "sg-user", maxlength: 64, autocomplete: "off", spellcheck: "false", "aria-label": "Your user name on the server", placeholder: "user" });
  const server = h("input", { id: "sg-server", maxlength: 253, autocomplete: "off", spellcheck: "false", "aria-label": "The server's name or address", placeholder: "server" });
  const problem = h("p", { class: "small muted", role: "alert" });
  const s3 = copyRow(() => tunnelCommand(user.value, server.value), actions);
  const refresh = () => {
    const u = user.value, s = server.value;
    problem.textContent = (u && !validUser(u)) || (s && !validServer(s)) ? "Use only letters, digits, dots, dashes and underscores (not a dash first)." : "";
    s3.set();
  };
  for (const el of [user, server]) { el.addEventListener("input", refresh); el.addEventListener("change", refresh); }
  s3.set();
  const result = h("div");
  const make = h("button", { type: "button", class: "pri", text: "Make the connect link" });
  make.addEventListener("click", async () => {
    if (opts.signInToConnect && opts.needsSignIn) return opts.signInToConnect();
    make.disabled = true;
    result.textContent = "";
    try { result.append(await opts.connectView()); }
    catch { result.append(h("p", { role: "alert", text: "Could not create a link. Please try again." })); }
    finally { make.disabled = false; }
  });
  return h("section", { class: "card ws-server", "aria-label": "New workspace on a server" },
    h("h2", { text: "New workspace on a server" }),
    h("p", { class: "small muted", text: "You do the setup on the server yourself. This page only shows the steps. The hub never connects to the server." }),
    h("ol", { class: "ws-steps" },
      step(1, "On the server, install orch", installer ? s1.shown : h("p", { class: "small muted", text: "The installer is not published yet. Ask your IT for it, or install orch the way your IT describes." })),
      step(2, "On the server, start the setup", s2.shown, h("p", { class: "small muted", text: "It prints a link that works once. Keep it in that terminal." })),
      step(3, "On your computer, open the secure tunnel", h("div", { class: "ws-who" }, user, h("span", { text: "@" }), server), problem, s3.shown,
        h("p", { class: "small muted", text: "If setup shows a different number, use the line it prints on the server." }),
        h("p", { class: "small muted", text: "Then open the link from step 2 in your browser and finish the setup there. You prove you own the workspace on that page." })),
      step(4, "Connect it to this hub", h("p", { class: "small muted", text: "When the setup is finished, make a link and paste it in the new workspace's Mission Control, under Settings > Hub > Connect." }), make, result)));
}
