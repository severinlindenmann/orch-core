// The Published page's demo data: the mockup's links (docs/mockups/hub/index.html, D.links) in the shape of GET /admin/published.
// Used by published.test.mjs and by the screenshot harness. Times are relative to `now`.
const DAY = 86400000, H = 3600000;
const ws = { pr: ["1".repeat(32), "Product"], mk: ["2".repeat(32), "Marketing"], lg: ["3".repeat(32), "Legal"] };
const row = (id, key, label, by, made, exp, views, state, pw = false) => ({
  id: id.repeat(32), workspace_id: ws[key][0], workspace: ws[key][1], kind: "secret", state, created_ms: made, expires_ms: exp, bytes: 2048,
  label, ticket: null, by_kind: "person", by_name: by, has_password: pw, views,
});
export const demo = (now = Date.now()) => ({
  links: [
    row("a", "pr", "hello.txt", "Severin", now - 6 * H, now + 7 * DAY, 2, "live"),
    row("b", "mk", "Release notes 2.4 (page)", "Anna Keller", now - 6 * H, now + 30 * DAY, 11, "live"),
    row("c", "lg", "Contract draft v3.pdf", "Lea Brunner", now - 4 * DAY, now + 2 * DAY, 1, "live", true),
    row("d", "mk", "Logo pack.zip", "Anna Keller", now - 17 * DAY, now - 3 * DAY, 24, "expired"),
  ],
  apps: [],
  hidden: [],
  stale_ms: null,
});
