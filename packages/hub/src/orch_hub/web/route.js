// Fragment routes contain data only; malformed links fall back to Home.
export function parseRoute(hash) {
  const value = String(hash ?? "");
  let m = /^#setup=([\w-]+)$/.exec(value);
  if (m) return { kind: "setup", token: m[1] };
  m = /^#invite=([A-Za-z0-9_-]{32,128})$/.exec(value);
  if (m) return { kind: "invite", token: m[1] };
  m = /^#t=([A-Za-z0-9._-]{8,128})$/.exec(value);
  if (m) return { kind: "ticket", token: m[1] };
  m = /^#\/(home|approvals|workspaces|services|admin|activity|account|handoffs|connections)$/.exec(value);
  if (m) return { kind: m[1] };
  m = /^#\/w\/([0-9a-f]{32})(?:\/(item|addon|settings|access)\/([^/]+))?$/.exec(value);
  if (!m) return { kind: "home" };
  try {
    const key = m[3] === undefined ? null : decodeURIComponent(m[3]);
    if (key !== null && (!key || /[\x00-\x1f\x7f]/.test(key))) return { kind: "home" };
    return key === null ? { kind: "workspace", workspace: m[1] } : { kind: m[2], workspace: m[1], key };
  } catch { return { kind: "home" }; }
}
