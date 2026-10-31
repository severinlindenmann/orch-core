// The company Tool servers page (M3, #1435 #1436): remote MCP servers a hub admin offers to workspaces. Offering is not
// enabling: a workspace's owner sees the exact address and turns a server on at their own host, and agents reach it only
// through the host's own proxy. There is no way to offer a program to run, only an https address.
import { emptyState, loading } from "../feel.js";
import { attentionList, h, sharePicker, text, withNames } from "../ui.js";

const info = (label, body) => h("details", { class: "info" }, h("summary", { "aria-label": label, text: "i" }), h("p", { class: "small muted", text: body }));
const NAME = /^[a-z][a-z0-9-]{1,39}$/;

// Mirrors the hub's own check so a typo is caught before sending; the hub decides in the end.
export function serverProblem(name, url, auth, secret) {
  if (!NAME.test(name)) return "Use a short name of lower-case letters, numbers and dashes.";
  if (name.startsWith("orch")) return "Names starting with orch are reserved.";
  if (!url.startsWith("https:" + "//")) return "Use a secure address (it starts with https).";
  if (/[?#\s]/.test(url)) return "Leave out spaces, ? and # (a key never goes into an address).";
  if (auth === "hub_secret" && !/^[a-z][a-z0-9-]{0,39}$/.test(secret ?? "")) return "Name the company secret to use.";
  return null;
}

const STATUS_WORD = { reachable: "Reachable", needs_sign_in: "Needs sign-in", failing: "Not reachable", not_checked: "Not checked yet" };
export const statusWord = (st) => STATUS_WORD[st?.status] ?? STATUS_WORD.not_checked;

export function mcpView(data, actions = {}) {
  const workspaces = data.workspaces ?? [];
  const form = (s, isNew) => {
    const nameIn = isNew ? h("input", { type: "text", "aria-label": "Name of the new server", placeholder: "for example: crm", maxlength: "40" }) : null;
    const url = h("input", { type: "url", "aria-label": "Address", placeholder: "https:" + "//tools.example.com/mcp"  /* built, so the console has no literal address */, maxlength: "200" });
    const desc = h("input", { type: "text", "aria-label": "What it is for", placeholder: "What it is for (short)", maxlength: "200" });
    const auth = h("select", { "aria-label": "Sign-in" }, h("option", { value: "none", text: "No sign-in" }), h("option", { value: "hub_secret", text: "A company secret" }));
    const secret = h("input", { type: "text", "aria-label": "Company secret name", placeholder: "secret name", maxlength: "40" });
    url.value = s?.url ?? ""; desc.value = s?.description ?? ""; auth.value = s?.auth ?? "none"; secret.value = s?.secret ?? "";
    const note = h("p", { class: "small", role: "alert" });
    const sync = () => { secret.hidden = auth.value !== "hub_secret"; };
    auth.addEventListener("change", sync); sync();
    const save = h("button", { type: "button", class: "primary", text: isNew ? "Add server" : "Save", onclick: () => {
      const name = isNew ? (nameIn.value ?? "") : s.name;
      const sec = auth.value === "hub_secret" ? secret.value ?? "" : null;
      const bad = serverProblem(name, url.value ?? "", auth.value, sec);
      if (bad) { note.textContent = bad; return; }
      actions.save?.(name, { url: url.value, auth: auth.value, secret: sec, description: desc.value ?? "" });
    } });
    return [nameIn, url, desc, auth, secret, note, save];
  };
  const card = (s) => {
    let sure = false;
    const remove = h("button", { type: "button", text: "Remove", onclick: () => {
      if (!sure) { sure = true; remove.textContent = "Sure? Remove it"; return; }
      actions.remove?.(s);
    } });
    const word = statusWord(s.status);
    const check = h("button", { type: "button", class: "check", text: "Check now", onclick: () => { check.disabled = true; actions.check?.(s.name); } });
    return h("article", { class: "card mcp", "data-server": s.name },
      h("div", { class: "row" }, h("h3", { class: "grow", text: s.name }), remove),
      h("div", { class: "row" }, h("span", { class: "status small", "data-status": s.status?.status ?? "not_checked", text: word }), check),
      form(s, false),
      h("p", { class: "small muted", text: "Saving changes the server, so each workspace owner has to accept it again." }),
      sharePicker(s, workspaces, (shared, ids) => actions.offer?.(s.name, shared, ids)));
  };
  return h("div", { class: "mcp-servers" },
    h("div", { class: "row" }, h("h1", { class: "grow", text: "Tool servers" }),
      info("About tool servers", "A tool server gives your agents extra tools over the internet, such as your CRM. Each workspace owner sees the address and turns it on. Agents never see the sign-in secret: the workspace connects for them, and you can switch single tools off there. Tools that change things start off; each workspace owner turns them on.")),
    attentionList(data.attention),
    h("p", { class: "banner warn", role: "note", text: "Nothing is switched on for anyone until their workspace owner accepts it." }),
    (data.servers ?? []).length ? (data.servers ?? []).map(card) : emptyState("mcp"),
    h("section", { class: "card" }, h("h3", { text: "New server" }), form(null, true)));
}

export async function mcpPage(d, retried = false) {
  const finish = loading();
  d.pageLoading("mcp");
  try {
    if (!(await d.refresh())) return d.loginView();
    if (!d.state.user.is_admin) return d.home();
    if (d.state.user.needs_sign_in) return d.reauthView();
    await d.loadRail();
    const r = await d.api("GET", "/admin/mcp-servers");
    if (r.status === 403 && r.body.error === "reauth_required") return d.reauthView();
    if (r.status !== 200) return d.shell("mcp", h("h1", { text: "Tool servers" }), h("p", { role: "alert", text: "Cannot read the tool servers. " + d.say(r.body.error) }));
    const att = await d.api("GET", "/admin/needs-attention");
    const say = (ok, x) => { d.state.notice = x.status === 200 ? d.banner("ok", ok, null) : d.banner("bad", "Not changed", d.say(x.body.error ?? "unavailable")); };
    const save = async (name, body) => { say("Saved", await d.api("PUT", "/admin/mcp-servers/" + name, body)); await mcpPage(d); };
    const remove = async (s) => { say("Removed", await d.api("DELETE", "/admin/mcp-servers/" + s.name)); await mcpPage(d); };
    const check = async (name) => { const x = await d.api("POST", "/admin/mcp-servers/" + name + "/check"); if (x.status !== 200) say("", x); await mcpPage(d); };
    const offer = async (name, shared, workspaces) => { say("Saved", await d.api("PUT", "/admin/mcp-servers/" + name + "/offers", { shared, workspaces })); await mcpPage(d); };
    d.shell("mcp", d.state.notice, mcpView({ ...r.body, attention: att.status === 200 ? att.body.items : [], workspaces: withNames(r.body.workspaces, d.state.rail?.rows ?? []) }, { save, remove, offer, check }));
    d.state.notice = null;
    // The hub checks stale servers in the background when this page opens; ask once more shortly for the answer.
    if (!retried && (r.body.servers ?? []).some((x) => x.status?.status === "not_checked")) setTimeout(() => mcpPage(d, true), 4000);
  } finally { finish(); }
}

// The registry entry: the console's router, tabs and file list are built from this one export (no other file names a page).
export const page = { path: "mcp", nav: { label: "Tools", admin: true, needsWorkspace: true, after: "skills" }, render: mcpPage };
