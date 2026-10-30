import { emptyState, toast } from "./feel.js";
// Connected workspaces and their availability.
import { restoredNotice } from "./pages/hub-settings.js";
import { serverGuideView } from "./serverguide.js";
import { ago, chip, h, text } from "./ui.js";
import { OPEN_ON_ITS_COMPUTER } from "./switch.js";

const nameOf = (end) => (end && end.name ? text(end.name) : end && end.owner_name ? text(end.owner_name + "'s workspace") : "Another workspace");

const modeName = (w) => w.mode === "full" ? "Shares everything" : "Shares only what you choose";
const personName = (p) => text(typeof p === "string" ? p : p?.name ?? p?.alias ?? p?.user_id ?? "unknown");
const button = (label, onclick, props = {}) => h("button", { type: "button", text: label, onclick, ...props });
const small = (value) => h("div", { class: "small muted", text: value });
const statusView = (w, now) => h("div", {}, chip(w.online ? "Online" : "Offline", w.online ? "ok" : ""),
  !w.online && Number.isFinite(w.last_seen_ms) ? small("since " + ago(w.last_seen_ms, now)) : null);
const hostView = (w) => h("div", {}, text(w.host ?? "unknown"), small(text(w.address ?? "unknown")));
const versionView = (w, hub) => h("div", {},
  h("div", { text: text(w.version ?? "unknown") + (w.commit ? " · " + text(w.commit.slice(0, 7)) : "") }),
  w.version_state === "unsupported" ? chip("not supported any more", "warn") :
    w.version_state === "older" && hub?.version ? chip("update to " + text(hub.version), "warn") :
      w.version_state === "newest" ? chip("newest", "ok") : null);

/** P05: a hub admin also sees the workspaces that are connected but that selective mode keeps out of /me/workspaces. Those
 * rows come from the admin-only list, are marked private, and carry only name, owner, mode, status and version: no host,
 * address, grants or roles, and no Open. `admin` is null for everyone else (and when the admin read failed): no change. */
export function privateRows(mine, admin) {
  if (!Array.isArray(admin)) return [];
  const have = new Set((mine ?? []).map((w) => w.workspace_id));
  return admin.filter((w) => w && w.status === "bound" && !have.has(w.workspace_id)).map((w) => ({
    workspace_id: w.workspace_id, name: w.name, owner_name: w.owner_name, mode: w.mode, online: w.online ?? w.status === "bound", last_seen_ms: w.last_seen_ms,
    version: w.version, commit: w.commit, version_state: w.version_state, private: true }));
}

/** Pure details view; roles is null when the caller cannot read access. */
export function workspaceDetailsView(w, { roles = null, owner = null, hub, actions = {} } = {}, now = Date.now()) {
  return [h("h2", { text: nameOf(w) }),
    h("dl", { class: "facts" },
      h("dt", { text: "Status" }), h("dd", {}, statusView(w, now)),
      h("dt", { text: "Owner" }), h("dd", { text: personName(roles?.owner ?? owner ?? w.owner_name) }),
      w.private ? null : h("dt", { text: "Runs on" }), w.private ? null : h("dd", {}, hostView(w)),
      h("dt", { text: "orch version" }), h("dd", {}, versionView(w, hub)),
      h("dt", { text: "Mode" }), h("dd", {}, chip(modeName(w)), small(w.private ? "Private to " + personName(w.owner_name) + ": the hub sees only what they choose to share." : w.mode === "full"
        ? "The hub can read everything and help across workspaces."
        : "The hub sees only what this workspace chooses to share, plus its name."))),
    roles ? h("section", {}, h("h3", { text: "Who has access" }),
      h("p", { text: "Owner: " + personName(roles.owner) }),
      roles.groups?.length ? h("ul", {}, roles.groups.map((g) => h("li", {},
        text(g.group) + " · " + text(g.role), small((g.members ?? []).map(personName).join(", "))))) : small("Only the owner.")) : null,
    roles?.profile ? h("section", {}, h("h3", { text: "Profile (untrusted display data)" }),
      h("dl", {}, Object.entries(roles.profile).flatMap(([key, value]) => [
        h("dt", { text: key }), h("dd", { text: JSON.stringify(value) })]))) : null,
    w.private ? null : h("h3", { text: "Change mode" }),
    w.private ? null : h("p", { text: "To change what this workspace shares, open it and use Settings > Hub. The workspace owner confirms with one click on their dashboard." }),
    w.private ? small("Only " + personName(w.owner_name) + " can open it.") : w.mode === "selective" ? small(OPEN_ON_ITS_COMPUTER) : button("Open " + nameOf(w), () => actions.open?.(w), { class: "pri" })].filter(Boolean);
}

/** The link is copied unchanged; after Copy the page says what to do next.
 * `link` is null until a person is chosen (several people: nothing is preselected). "Someone new..." invites a person
 * (the existing admin invite) and never mints a workspace link. */
export function connectWorkspaceView(link, actions = {}, { people = [], selected = null, self = null } = {}) {
  const NEW = "__new__";
  const next = h("p", { class: "small muted", "aria-live": "polite" });
  const ROUTE = "open their workspace, go to Settings › Hub and press Connect a hub.";
  // P12: after Copy, say where the link goes next, in the words of the screens the person will see.
  const nextWords = (who) => who === "you" ? "Open your workspace, go to Settings › Hub and press Connect a hub." : "Send it to " + who + ". They " + ROUTE;
  const ownerName = () => (self && select.value === self) ? "you" : text(known.find((p) => p.user_id === select.value)?.name ?? "the workspace owner");
  const clock = (ms) => { const d = new Date(ms); return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0"); };
  const input = h("input", { class: "grow", readonly: true, value: link?.link ?? "", "aria-label": "Connect link" });
  let current = link?.link ?? "";
  const copy = button("Copy", async () => {
    let ok = false;
    try { ok = !!(await actions.copy?.(current)); } catch { ok = false; }
    copy.textContent = ok ? "Copied ✓" : "Could not copy. Select the link to copy it.";
    next.textContent = ok ? "Copied ✓ " + nextWords(ownerName()) : "";
    next.className = ok ? "banner ok" : "small muted";
  });
  const openLink = button("Open link", () => actions.openLink?.(current));
  // r11 P03: "Open link" opens the link in THIS browser, so only for the signed-in person's own workspace; anyone else's
  // link is copied and opened on their computer (never offered to a dashboard on this one).
  const showOpen = (owner) => { openLink.hidden = !(self && owner === self); };
  showOpen(link ? selected : null);
  const until = h("p", { text: link ? "Works once, for 30 minutes (until " + clock(link.expires_ms) + ")" : "" });
  const problem = h("p", { role: "alert" });
  const linkRow = h("div", { class: "ws-link" }, h("div", { class: "row" }, input, copy, openLink), next, until);
  linkRow.hidden = !link;
  // Only people the hub knows are offered, and the hub rejects anyone else (the workspace host never picks its owner).
  let known = people.filter((p) => p && typeof p.user_id === "string");
  const select = h("select", { id: "ws-owner" });
  const fill = (chosen) => {
    select.textContent = "";
    if (!chosen) select.append(h("option", { value: "", text: "Choose…" }));
    for (const p of known) select.append(h("option", { value: p.user_id, selected: p.user_id === chosen ? true : null, text: text(p.name ?? "Unnamed") }));
    select.append(h("option", { value: NEW, text: "Someone new (add them now)" }));
    select.value = chosen ?? "";
  };
  // "Someone new...": name + Create sign-in link (POST /admin/invites). The workspace link waits until they have signed in.
  const nameInput = h("input", { id: "ws-new-name", maxlength: 80, autocomplete: "off", "aria-label": "Their name" });
  const inviteInput = h("input", { class: "grow", readonly: true, "aria-label": "Sign-in link" });
  let inviteLink = "";
  const inviteCopy = button("Copy", async () => {
    let ok = false;
    try { ok = !!(await actions.copy?.(inviteLink)); } catch { ok = false; }
    inviteCopy.textContent = ok ? "Copied ✓" : "Could not copy. Select the link to copy it.";
  });
  const waiting = h("p", { class: "small muted", "aria-live": "polite", text: "" });
  const inviteResult = h("div", {}, h("div", { class: "row" }, inviteInput, inviteCopy),
    small("Send them the link. They choose a password, then come back here to connect their workspace."), waiting);
  inviteResult.hidden = true;
  const signedIn = h("div", { class: "banner ok", role: "status" });
  signedIn.hidden = true;
  // r11 P12/P12b: after a sign-in link, ask the hub every 3 s about THAT link (GET /admin/invites/<id>, only the admin
  // who made it may read it). "joined" names exactly the person who used it: select them and make their workspace link.
  // Anything else that ends the wait (ended, expiry, no valid user id) asks the admin to pick: the panel never guesses.
  const pick = h("div", { class: "ws-pick" }, h("h3", { text: "Pick the person" }), small("Choose who signed in with your link."));
  pick.hidden = true;
  let poll = null, lastPoll = null, picked = 0; // picked counts hand picks; lastPoll is the newest watch started
  const stopWaiting = () => { if (poll) clearInterval(poll); poll = null; };
  const askToPick = async () => {
    stopWaiting();
    waiting.textContent = "";
    try { const fresh = await actions.people?.(); if (Array.isArray(fresh)) known = fresh.filter((p) => p && typeof p.user_id === "string"); } catch { /* keep the old list */ }
    fill(null);
    newBox.hidden = true;
    linkRow.hidden = true;
    pick.hidden = false;
  };
  const startWaiting = (name, inviteId, expiresMs) => {
    stopWaiting();
    if (typeof inviteId !== "string" || !inviteId) return; // no id to ask about: the admin picks by hand later
    waiting.textContent = "Waiting for " + text(name) + " to sign in…";
    let busy = false;
    poll = lastPoll = setInterval(async () => {
      if (section.isConnected === false || actions.alive?.() === false) return stopWaiting(); // panel or page left
      if (globalThis.document?.hidden || busy) return;
      if (Number.isFinite(expiresMs) && Date.now() >= expiresMs) return void (await askToPick());
      busy = true;
      try {
        const st = await actions.inviteStatus?.(inviteId);
        if (!poll || !st || st.state === "waiting") return;
        if (st.state !== "joined" || typeof st.user_id !== "string" || !st.user_id) return void (await askToPick());
        const mine = poll;
        stopWaiting();
        const pickedBefore = picked;
        const handPicked = () => picked !== pickedBefore || mine !== lastPoll;
        const fresh = await actions.people?.();
        if (handPicked()) return; // the admin picked someone by hand (or a new link started) while the list loaded
        if (Array.isArray(fresh)) known = fresh.filter((p) => p && typeof p.user_id === "string");
        if (!known.some((p) => p.user_id === st.user_id)) return void (await askToPick());
        fill(st.user_id);
        newBox.hidden = true;
        waiting.textContent = "";
        const who = text(known.find((p) => p.user_id === st.user_id)?.name ?? st.name ?? name);
        signedIn.textContent = "✓ " + who + " has signed in";
        signedIn.hidden = false;
        await choose(st.user_id);
        if (!linkRow.hidden) next.textContent = nextWords(who);
      } catch { /* try again on the next tick */ }
      finally { busy = false; }
    }, 3000);
  };
  const create = button("Create sign-in link", async () => {
    problem.textContent = "";
    const name = (nameInput.value ?? "").trim();
    if (!name) { problem.textContent = "Enter their name first."; return; }
    create.disabled = true;
    try {
      const made = await actions.invite?.(name);
      if (!made || typeof made.link !== "string") throw new Error("invite failed");
      inviteLink = made.link; inviteInput.value = made.link; inviteInput.setAttribute("value", made.link);
      inviteResult.hidden = false;
      nameInput.value = "";
      try { const fresh = await actions.people?.(); if (Array.isArray(fresh)) { known = fresh.filter((p) => p && typeof p.user_id === "string"); fill(NEW); } } catch { /* keep the old list */ }
      startWaiting(name, made.invite_id, made.expires_ms);
    } catch { problem.textContent = "Could not create a sign-in link. Please try again."; }
    finally { create.disabled = false; }
  }, { class: "pri" });
  const newBox = h("div", { class: "ws-new" }, h("label", { for: "ws-new-name", text: "Their name" }),
    h("div", { class: "row" }, nameInput, create), inviteResult);
  newBox.hidden = true;
  fill(selected);
  const choose = async (value) => {
    select.disabled = true;
    try {
      const made = await actions.connect?.(value);
      if (made) { showOpen(value); current = made.link; input.value = made.link; input.setAttribute("value", made.link); until.textContent = "Works once, for 30 minutes (until " + clock(made.expires_ms) + ")"; next.textContent = ""; copy.textContent = "Copy"; linkRow.hidden = false; }
    } catch { problem.textContent = "Could not create a link for this person. Please try again."; }
    finally { select.disabled = false; }
  };
  select.addEventListener("change", async (e) => {
    stopWaiting(); // the admin chose by hand: stop watching for the invited person
    picked++;
    problem.textContent = "";
    signedIn.hidden = true;
    pick.hidden = true;
    const value = e.target.value;
    const isNew = value === NEW;
    newBox.hidden = !isNew;
    linkRow.hidden = true;
    if (isNew || !value) return;
    await choose(value);
  });
  const who = h("div", {}, h("label", { for: "ws-owner", text: "Whose workspace is this?" }), select,
    small("You open the hub from it as this person."));
  const section = h("section", { class: "card ws-connect", "aria-label": "Connect a workspace" },
    h("h2", { text: "Connect a workspace" }),
    pick, who, signedIn, linkRow, newBox, problem);
  section.stopWaiting = stopWaiting;
  return section;
}

/** Workspaces in `rows` whose id is not in `known` (a Set of ids). */
export const newWorkspaces = (known, rows) => (rows ?? []).filter((w) => w.workspace_id && !known.has(w.workspace_id));

/** The green message that replaces the used link box once a workspace has joined. */
export function joinedBannerView(row, actions = {}) {
  // A private (selective-mode) workspace: only its owner can open it, so no Open button and nothing else about it.
  const note = row.private ? "It is online now. Only " + (row.owner_name || "its owner") + " can open it."
    : row.mode === "selective" ? "It is online now. It shares only chosen items with the hub, so open it on its own computer."
    : "It is online now. You can open it from here.";
  return h("div", { class: "banner ok ws-joined", role: "status", "data-joined": row.workspace_id },
    h("div", { class: "row" },
      h("div", { class: "grow" }, h("b", { text: "✓ " + nameOf(row) + " joined" }), small(note)),
      row.private || row.mode === "selective" ? null : button("Open " + nameOf(row), () => actions.open?.(row), { class: "pri" })));
}

/** data: {workspaces, handoffs, admin: rows|null, hub, actions, joined?: rows that joined while the page was open}. */
export function workspacesView(data, now = Date.now()) {
  const actions = data.actions ?? {};
  const connectBox = h("div");
  const joined = data.joined ?? [];
  for (const row of joined) connectBox.append(joinedBannerView(row, actions));
  // The node with the connect link (or the person picker); the link is made here, only when a button asks for it.
  const connectNode = async () => {
    const people = (await actions.people?.()) ?? [];
    // With several people nothing is preselected and no link exists until one is chosen; alone, the admin is the person.
    const mine = people.length > 1 ? null : people.find((x) => x.user_id === data.self)?.user_id ?? people[0]?.user_id ?? null;
    const first = people.length > 1 ? null : await actions.connect(mine);
    return connectWorkspaceView(first, actions, { people, selected: mine, self: data.self });
  };
  const connect = button("Connect a workspace", async () => {
    if (data.needsSignIn) return actions.signInToConnect?.(); // a session that came from a workspace: sign in once first
    connect.disabled = true;
    connectBox.textContent = "";
    try { connectBox.append(await connectNode()); }
    catch { connectBox.append(h("p", { role: "alert", text: "Could not create a link. Please try again." })); }
    finally { connect.disabled = false; }
  }, { class: "pri" });
  // HUB-WS-07: a guide only (no request when it opens); the installer line stays hidden until the installer is published.
  const guide = button("New workspace on a server", () => {
    connectBox.textContent = "";
    connectBox.append(serverGuideView(actions, { installerUrl: data.installerUrl, installerVerified: data.installerVerified,
      connectView: connectNode, needsSignIn: data.needsSignIn, signInToConnect: actions.signInToConnect }));
  });
  const waitingFor = new Map();
  for (const r of data.handoffs) if (r.status === "waiting" && r.to.workspace_id) waitingFor.set(r.to.workspace_id, (waitingFor.get(r.to.workspace_id) ?? 0) + 1);
  const shown = [...data.workspaces, ...privateRows(data.workspaces, data.admin)];
  const list = shown.length ? h("div", { class: "scroll" }, h("table", { class: "tbl ws-table" },
    h("thead", {}, h("tr", {}, ["Workspace", "Status", "orch version", "Runs on", "Actions"].map((label) => h("th", { scope: "col", text: label })))),
    h("tbody", {}, shown.map((w) => {
      const n = waitingFor.get(w.workspace_id) ?? 0;
      return h("tr", { "data-workspace": w.workspace_id, class: joined.some((j) => j.workspace_id === w.workspace_id) ? "row-new" : "" },
        h("td", {}, h("b", { text: nameOf(w) }), h("div", {}, chip(modeName(w), w.mode === "full" ? "full" : "sel")),
          w.owner_name ? small("Owner: " + text(w.owner_name)) : null,
          w.private ? small("Private to " + text(w.owner_name ?? "its owner")) : w.mode === "selective" ? small("Personal: only its owner") : null,
          n ? small(n + (n === 1 ? " hand-off is" : " hand-offs are") + " waiting for this workspace") : null),
        h("td", {}, statusView(w, now)), h("td", {}, versionView(w, data.hub)), h("td", {}, w.private ? small("Not shown") : hostView(w)),
        h("td", {}, h("div", { class: "ws-actions" }, button("Details", () => actions.details?.(w)),
          w.private ? small("Only " + text(w.owner_name ?? "its owner") + " can open it")
            : w.mode === "selective" ? small("Open it on its own computer") : button("Open", () => actions.open?.(w)))));
    })))) : emptyState(data.admin != null || data.canConnect ? "workspaces" : "workspaces-member");
  const older = data.workspaces.filter((w) => ["older", "unsupported"].includes(w.version_state));
  const left = (data.admin ?? []).filter((w) => w.status === "unbound");
  const leftView = data.admin != null ? h("details", {}, h("summary", { text: "Show left workspaces (" + left.length + ")" }),
    ...left.map((w) => {
      const error = h("p", { role: "alert" });
      const remove = button("Remove", async () => {
        remove.disabled = true;
        error.textContent = "";
        try { await actions.remove(w); }
        catch { error.textContent = "Could not remove this workspace. Please try again."; }
        finally { remove.disabled = false; }
      }, { class: "danger" });
      return h("article", { class: "list-row" }, h("div", { class: "row" }, h("b", { class: "grow", text: nameOf(w) }), chip("Left"), remove),
        small(Number.isFinite(w.left_ms) ? "Left " + new Date(w.left_ms).toLocaleDateString() : "Left on an unknown date"), error);
    })) : null;
  // HUB-WS-04: a hub admin asks the owners to update; the request carries only the version, the owner clicks in Mission Control
  const askedAt = Math.max(0, ...older.filter((w) => w.update_asked_target === data.hub?.version).map((w) => w.update_asked_ms).filter(Number.isFinite));
  const askBox = h("div", { class: "row" });
  const showAsked = (at) => { askBox.textContent = ""; askBox.append(small("Asked " + ago(at, Math.max(now, at)))); };
  if (data.admin != null && actions.askUpdate && older.length) {
    if (askedAt > 0) showAsked(askedAt);
    else {
      const error = h("p", { role: "alert" });
      const ask = button("Ask owners to update", async () => {
        ask.disabled = true;
        error.textContent = "";
        try {
          const r = await actions.askUpdate();
          showAsked(Number.isFinite(r?.at_ms) ? r.at_ms : now);
          try { toast("Each owner now sees an update request in Mission Control."); } catch { /* no page to show it on */ }
        } catch { error.textContent = "Could not send the request. Please try again."; ask.disabled = false; }
      });
      askBox.append(ask, error);
    }
  }
  return [
    data.restored ? restoredNotice(data.restored, actions) : null,
    h("div", { class: "row" }, h("h1", { class: "grow", text: "Workspaces" }), data.admin != null || data.canConnect ? [guide, connect] : null),
    connectBox,
    older.length ? h("div", { class: "banner warn" }, h("b", { text: older.length === 1 ? "1 workspace runs an older orch" : older.length + " workspaces run an older orch" }),
      small(older.map(nameOf).join(", ")), data.hub?.min_supported ? small("The hub still works with " + text(data.hub.min_supported) + " and newer.") : null,
      data.admin != null && actions.askUpdate ? askBox : null) : null,
    h("section", { class: "card", "aria-labelledby": "ws-h" }, h("h2", { id: "ws-h", text: "Connected workspaces" }), list, leftView),
  ].filter(Boolean);
}
