// Access changes that wait for the workspace owner (M2-8a, orch-core#1409): the owner's list on the Approvals page. Pure
// apart from the DOM it builds; the signing itself (the browser's channel key) lives in app.js, like every other signature.
// What the owner signs is checked here first: the digest is recomputed from the `ctx` the hub sent and the body in it must
// be the very change that is listed, so the words on screen and the bytes signed cannot differ.
import { h, text, when } from "./ui.js";
import { cj, hex, sha256, utf8 } from "./cj.js";

export const LABEL = "orch/v2/hub-access-change|";
export const KIND = "orch.access_change.v1";
const CTX_KEYS = "base_snapshot_version body change_id contract delta expires_ms kind workspace_id";

const ROLE = { read: "read", edit: "edit", admin: "admin" };
const role = (r) => ROLE[r] ?? text(r);

/** The plain-words sentence for one change and the workspace it is about. */
export function sentence(change, workspaceName) {
  const who = text(change.proposer?.name || "An administrator"), ws = text(workspaceName || "a workspace");
  if (change.kind === "member_add") return who + " wants to add " + text(change.person_name || "someone") + " to " + text(change.group) + ", which can open " + ws;
  return who + " wants to give " + text(change.group) + " " + role(change.role) + " access to " + ws;
}

/** The digest to sign, recomputed from the context the hub sent. Throws Error("event_changed" | "malformed") unless the
 *  context is exactly the one change and workspace the owner was shown, and the hub's digest equals ours. */
export async function checkChange(prepared, change, workspaceId) {
  const ctx = prepared?.ctx;
  if (ctx === null || typeof ctx !== "object" || Array.isArray(ctx) || Object.keys(ctx).sort().join(" ") !== CTX_KEYS) throw new Error("malformed");
  let ours, digest;
  try {
    digest = await sha256(utf8(LABEL + cj(ctx)));
    ours = cj(change.body);
    if (cj(ctx.body) !== ours) throw new Error("event_changed");
    // the people the owner was shown are exactly the ones signed for (what the change does to who can reach the workspace)
    const shown = (change.approvals ?? []).find((a) => a.workspace_id === workspaceId);
    if (!shown || cj(ctx.delta) !== cj(shown.delta)) throw new Error("event_changed");
  } catch (e) {
    throw new Error(e?.message === "event_changed" ? "event_changed" : "malformed");
  }
  if (ctx.contract !== 1 || ctx.kind !== KIND || ctx.change_id !== change.id || ctx.workspace_id !== workspaceId || ctx.expires_ms !== change.expires_ms) throw new Error("event_changed");
  if (typeof prepared.digest !== "string" || hex(digest) !== prepared.digest) throw new Error("event_changed");
  return digest;
}

/** `data` is GET /me/changes ({pending, notices}). `actions`: approve(change, approval), reject(change, approval), and
 *  `canSign` (false: this browser has no key yet). Returns an array of nodes (empty when there is nothing to show). */
export function changesView(data, actions, now = Date.now()) {
  const out = [];
  const rows = [];
  for (const change of data?.pending ?? []) {
    for (const ap of change.approvals ?? []) {
      if (ap.state !== "pending") continue;
      const expired = Number.isFinite(change.expires_ms) && now >= change.expires_ms;
      const live = !ap.stale && !expired;
      rows.push(h("div", { class: "list-row row change" }, h("div", { class: "grow" },
        h("strong", { text: sentence(change, ap.workspace_name) }),
        ap.people?.length ? h("p", { class: "small", text: "This gives access to: " + ap.people.map((x) => text(x.name || "A person") + " (" + role(x.role) + ")").join(", ") + "." }) : null,
        h("p", { class: "small muted", text: "Asked " + when(change.created_ms, now) + ". If nobody answers it lapses on " + when(change.expires_ms, now) + ", and nothing changes." }),
        ap.stale ? h("p", { class: "small", text: "Access to this workspace changed after this was asked, so it can no longer be approved. " + text(change.proposer?.name || "The administrator") + " can ask again." }) : null,
        expired ? h("p", { class: "small", text: "This request has lapsed." }) : null,
        live && actions.canSign === false ? h("p", { class: "small", text: "Set up this browser to approve (Home > Where you can sign)." }) : null),
      live ? h("div", { class: "row" },
        h("button", { class: "pri", type: "button", text: "Approve", disabled: actions.canSign === false, onclick: () => actions.approve(change, ap) }),
        h("button", { type: "button", text: "Reject", onclick: () => actions.reject(change, ap) })) : null));
    }
  }
  if (rows.length) out.push(h("section", { class: "card", id: "access-changes" }, h("h2", { text: "Access changes waiting for you (" + rows.length + ")" }),
    h("p", { class: "small muted", text: "Nothing changes in your workspace until you approve. You confirm with the key held by this browser." }), rows));
  for (const change of data?.notices ?? []) {
    for (const ap of change.approvals ?? []) {
      out.push(h("div", { class: "banner warn", role: "status" }, h("b", { text: "Applied without your approval" }),
        h("div", { class: "small", text: text(change.decided_by || "An administrator") + " applied this on " + when(change.decided_ms, now) + ": " + sentence(change, ap.workspace_name) + "." }),
        h("div", { class: "small", text: "Reason given: " + text(change.reason || "none") })));
    }
  }
  return out;
}
