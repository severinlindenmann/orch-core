// Pure helpers of the hub <-> workspace switch (hub-one-app, TECH.md "Decisions 27 Oct"): no DOM, no storage, so node can test them.
// The ticket travels in the URL fragment only; the page strips it at once and exchanges it with a same-origin POST.

const TICKET = /^#t=([A-Za-z0-9._-]{8,128})$/;
const RETURN = /^\/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,255}$/;

/** The ticket in a location hash, or null. */
export function ticketFromHash(hash) {
  const m = TICKET.exec(String(hash ?? ""));
  return m ? m[1] : null;
}

/** A relative path the hub console serves, else null (the caller shows Home). Mirrors the server's return_to rule. */
export function safeReturn(path) {
  if (typeof path !== "string" || !RETURN.test(path) || path.startsWith("//") || path.includes("\\") || path.includes("..")) return null;
  return path === "/" || path === "/c" || path.startsWith("/c?") ? path : null;
}

/** Up to two letters for a rail icon. */
export function initials(name, id = "") {
  const words = String(name ?? "").trim().split(/\s+/).filter(Boolean);
  const s = words.length > 1 ? words[0][0] + words[1][0] : (words[0] ?? String(id)).slice(0, 2);
  return s.toUpperCase();
}

/** A stable colour class (c0..c3) from the workspace id. */
export function colour(id) {
  let n = 0;
  for (const ch of String(id)) n = (n * 31 + ch.charCodeAt(0)) >>> 0;
  return "c" + (n % 4);
}

/** Rail rows from the hub's answer (GET /rail, else the plain workspace list): only what is shown, closed shape. */
export function railRows(body) {
  const list = Array.isArray(body?.workspaces) ? body.workspaces : [];
  return list
    .filter((w) => w && typeof w.workspace_id === "string")
    .map((w) => ({
      id: w.workspace_id,
      name: typeof w.name === "string" && w.name ? w.name : w.workspace_id.slice(0, 8),
      role: typeof w.role === "string" ? w.role : null,
      online: w.online === true,
      badge: Number.isInteger(w.badge) && w.badge > 0 ? w.badge : 0,
      canOpen: typeof w.origin === "string" && w.origin !== "",
    }));
}

export const OPEN_FIRST = "Open its dashboard once first";

/** Only an http(s) address may be navigated to after a ticket answer. */
export function safeUrl(url) {
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : null;
  } catch {
    return null;
  }
}

/** What the redeem answer means: {kind:"home"|"path"|"signout"|"expired", path?}. */
export function landing(status, body) {
  if (status !== 200 || !body || typeof body !== "object") return { kind: "expired" };
  if (body.kind === "signout") return { kind: "signout" };
  const p = safeReturn(body.return_to);
  return p && p !== "/" ? { kind: "path", path: p } : { kind: "home" };
}
