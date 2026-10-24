# Add-on platform: catalog, contract, updates, UI, dashboard versions

Status: design draft, 22 Oct 2026. Companion to [install-update.md](install-update.md), which covers the core and
dashboard releases, the signing keys, and `orch update`. Owner requirement (22 Oct): an update to an add-on, a new
add-on feature, or an entirely **new** add-on must ship **without** a new release of orch core or the dashboard. Core
and add-on updates can be combined in one `orch update`, but they don't have to be.

Labels: **[now]** is built in this effort. **[later]** needs tier B (worker UID, D-P2-20) or a separate security review
first.

## 1. Today

- An add-on is a package directory `<workspace>/addons/<name>/` with `orch-addon.json` (schema `orch.addon/2`, strict:
  `additionalProperties: false`). It is read once with strict rules: no links, ASCII names, caps, and no `.pyc`/`.so`
  (`orch/addons/package.py`). The package digest covers the whole listing.
- `orch setup` copies the **builtin manifests** shipped inside the core wheel (`orch/addons/manifests/*.json`, which a
  test pins to `addons/<name>/orch-addon.json`) into the workspace.
- `addon.granted` (signed by the owner, per workspace) covers `name`, `version`, `package_sha256`, `capabilities` and
  the declarations. The registry (`orch/addons/registry.py`) reports `changed` when the package on disk differs from
  the grant (digest, version or name). Only `active` add-ons run. So **a changed package already stops running until
  someone grants it again**.
- Capabilities form a closed list: `serve_http`, `spawn_agent`, `pty`, `network`, `git_push`, and since addon-api 1.1 `choose_model`, `credential` (`manifest.py`).
- The 11 first-party add-ons are **manifest-only** (D-P2-36). Their behaviour is core code (ops) plus dashboard code:
  the UI in `addons/dashboard/src/…` and server modules in `orch_dashboard/` (`wiki/`, `explorer/`, `terminals.py`,
  `worktrees.py`, `usage.py`, …). D-P2-20: add-on code runs only under the worker UID, which tier A does not have.
- One precedent for safe third-party UI: Widgets (`orch_dashboard/widget_frame.py`). Its templates are pinned by
  digest and run in an opaque-origin, network-less sandboxed iframe that gets its data through `postMessage`.

**Consequence.** To ship add-on changes without a core release, the add-on's behaviour must live in its package. In
tier A that can only be **data**: manifest, declarative UI, digest-pinned frame bundles. **Code** in an add-on
(server or runner side) needs tier B.

## 2. The add-on API contract `orch.addon-api/1` [now]

Contents today (implicit): the manifest schema `orch.addon/2` (`entry`, `fields` with `set_by` roles, `sections`,
`artifact_kinds`, `capabilities`, `needs`, `cli`, `skills`, `agents_md`); the capability list; the events
`addon.granted|disabled|purged`; the runner environment (`ORCH_ADDON`, `ORCH_ADDON_CAPABILITIES`, nothing else); the
`addon.*` ops (`grant`, `preview`, `list`, `disable`, `purge`).

Missing for a stable contract. All of these are written down in `docs/architecture/addon-api.md` (new) and pinned by
contract tests:
1. **Versioning in the manifest:** `api: ">=1.0,<2"` (the range the add-on needs) and `min_core: "2.0.0.dev60"`. This
   needs a schema `orch.addon/3`. Core keeps reading `/2` as `api: ">=1.0,<2"`. An older core rejects `/3` (strict
   schema): that is the wanted fail-closed behaviour.
2. **Core declares** `ADDON_API = "1.N"`. Within major 1, changes only add things (new optional manifest keys, new
   capabilities, new events, new UI components). Removing or narrowing anything means major 2, and core then reads
   majors 1 and 2 for at least 6 months.
3. **A host-op list per capability** (which ops a `network` or `serve_http` add-on may call) and the event kinds an
   add-on may read. Today this is implicit in dispatch. It becomes a table plus a test that dispatch enforces it.
4. **A declarative UI schema** `ui` in the manifest (§5).
5. **A changelog convention:** plain text, at most 4 KB, in the catalog (§3), not in the package.

## 3. The signed add-on catalog [now]

A separate envelope `addons.json`, the same envelope format as install-update §4.2 with payload format
`orch.addon-catalog/1`:

```json
{"format":"orch.addon-catalog/1","seq":311,"issued_at":"…","expires_at":"…",
 "addons":{"wiki":{"title":"Wiki","versions":[
   {"version":"0.2.0","api":">=1.1,<2","min_core":"2.0.0.dev60","capabilities":[],
    "package_sha256":"sha256:…","archive":{"url":"…/addon-wiki-v0.2.0/wiki-0.2.0.tar","sha256":"…","size":5120},
    "changelog":"Page links in tickets.","revoked":false}]}}}
```

- **Releases:** each add-on is released on its own. A tag `addon-<name>-v<version>` holds an immutable release with a
  deterministic tar of the package directory. A workflow `addon-release.yml` (the owner pushes it) runs on a push to
  main/develop that changes `addons/<name>/**` or on `workflow_dispatch`. It builds the tar, checks it with
  `read_package` and the schema, and re-signs the catalog in the fixed release `channel-<ch>` as asset `addons.json`.
  Add-ons stay in orch-core's `addons/` for now. A separate repo is possible later without a format change.
- **Key: same root, a separate catalog key** (cert `purpose: "addon-catalog"`, its own environment
  `addon-release`). Core's verifier requires `purpose: "core"` for core releases and `"addon-catalog"` for the catalog.
  A leaked catalog key therefore cannot ship core code, and its worst case is further bounded: a catalog package never
  runs without a human-signed grant that shows its capabilities. One root means one trust anchor and one rotation and
  revocation path. A fully separate root would double the owner's ceremonies for no gain.
- **Bundled defaults:** each core release embeds the catalog snapshot it was built with
  (`orch/addons/catalog-bundled.json` plus the package tars), so a fresh install sets up offline. The bundled entries
  are ordinary catalog entries, so a later update compares versions as usual.
- **Package intake:** download to `$XDG_CACHE_HOME/orch/addons/<name>/<version>.tar` → check the sha256 → extract
  into a fresh private staging directory. Refuse links, absolute paths, `..`, and anything the package rules refuse →
  `read_package` → check that its digest equals `package_sha256` → check the manifest name and version against the
  catalog entry. Every later step uses the bytes read once.

## 4. Updates, compatibility, re-approval [now]

Commands (all human-only, see §7):

```
orch update                       core + dashboard (if newer), then every granted add-on (if newer and compatible)
orch update core                  core + dashboard only
orch update addon <name> [--workspace W]
orch addon install <name> [--workspace W]       a new add-on from the catalog, then its grant review
orch update [core|addon <name>] --check         read-only, also for agents
```

**Compatibility.** An add-on is *compatible* when core's `ADDON_API` is in its `api` range and core ≥ its `min_core`.
The registry gets a new state `incompatible` (not `active`, so it does not run, and the UI and CLI say why).
`orch update core` first runs a **preflight**, per workspace. It lists (a) the add-ons that would become incompatible
and (b) the add-ons whose update needs a new grant. If (a) is not empty and the catalog has a compatible version, the
plan offers the combined update. If no compatible version exists, it asks: "Wiki 0.1.0 will pause until a compatible
version exists. Continue? [y/N]". A core update never disables a granted add-on silently.

**Re-approval: the old granted version keeps running until the new grant is signed.** The new package is **staged**
at `<state>/addons-staged/<workspace-id>/<name>/<version>/` (host-UID private, outside the workspace). The workspace
copy and the grant stay as they are, and the add-on stays `active` on the old version.

The reasons: the grant covers only the old digest, so nothing is widened. Pausing would turn every update into an
outage and push people to approve quickly without reading. The old version keeps exactly the capabilities that were
granted.

Two exceptions **pause** the add-on (`incompatible` or `revoked` state, with a "needs approval" note): the old version
is incompatible with the running core, or the catalog marks the old version `revoked: true` (a security fix).

**Grant from staged.** `addon.grant` gets an optional `staged: <version>`. Inside the store lock, the host re-reads the
staged package and checks that its digest equals the digest in the signed grant. It appends `addon.granted`, then
swaps the directory by rename (old → `.<name>.old`, staged → `<name>`, remove `.old`). Between the event and the
rename, the registry sees `changed` and fails closed. On a crash, recovery at start finishes or rolls back the rename
by comparing digests with the last grant.

**The review** extends `install.review_lines` / `_changes`, in the CLI and in Settings → Add-ons alike:
`Wiki 0.1.0 → 0.2.0`; the capability diff (`+ network` shown as **new**, `− pty` as removed); the diffs of fields,
sections, needs and cli; the package digest. The changelog is labelled *"from the add-on catalog, not part of what you
sign"*: it is one-line-safe, size-capped, and treated as data. WYSIWYS: the signed body is exactly
`grant_event(manifest, package)` of the staged bytes. A grant can never be widened without that signature, because the
runtime check is digest equals grant.

**Several workspaces:** one grant per workspace, each its own review. Only the roles that may grant today can grant.
Other members see `update available, waiting for the owner`.

## 5. Add-on UI without a dashboard release

Options: (a) the add-on's own prebuilt bundle in a sandboxed iframe; (b) declarative JSON that the dashboard renders;
(c) a hybrid of the two.

**Recommendation: (c), with (b) first [now] and the frame [later].**

- **Declarative [now]:** manifest `ui` (API 1.1) describes settings forms (bound to the add-on's `fields` and its
  settings needs), status cards, lists and tables bound to **named read queries** the host serves (`addon.view` with
  the add-on's declared reads), and actions. Actions are **only** references to existing host ops that the
  capability table (§2.3) allows. The dashboard renders them with its own components. No add-on code runs in the page
  origin, so an add-on cannot reach the session token, the click signer or handoff. New UI components are added through
  dashboard releases. Add-ons pick them up through the `api` range.
- **Frames [later, Opus review first]:** for rich pages, the add-on package carries a prebuilt bundle (HTML, JS, CSS;
  inside the package digest, so the grant covers it). It is served at `/addon-frame/<name>/<package-sha256>/…` with the
  widget-frame rules: CSP `sandbox allow-scripts` (never `allow-same-origin`), `default-src 'none'`,
  `connect-src 'none'`, `frame-ancestors 'self'`, exact digest URLs, no redirects. It talks only through a
  `postMessage` bridge with a fixed message schema: reads scoped to the add-on, and `request_action(op, args)`. The
  parent sends that request to the host, which **prepares** the request. The person then sees the host's normal review
  and signs it in the parent page (page = server = signer parity unchanged). A frame never gets a token, never calls
  sign or handoff, and cannot cause a write without that review.
- **Why not frames only:** every add-on would ship JavaScript, a bigger attack surface, and simple settings need none.
  **Why not declarative only:** explorer- or wiki-style pages are not expressible in it.

**Server-side add-on code** is never loaded into the dashboard server process. That process holds the person's
session and the click signer, so add-on code there would be Tier S code in the signer's process. Such code moves to
the add-on's **runner-side** entry (`entry.cmd`, started by `orch/addons/runner.py` under the worker UID with only its
granted capabilities). It talks to the host through the capability-scoped ops, and the dashboard reaches it only
through the host. That needs tier B **[later]**. Until then, add-ons that need server code stay **core-bundled**
(§6).

## 6. Migration

| Add-on | Plan |
|---|---|
| dashboard, terminals (`pty`), start-agent (`spawn_agent`), worktrees (git) | **Stay core-bundled.** They carry Tier S capabilities and live in the host, runner and dashboard server. They are versioned with the train. |
| estimate, model-routing, usage | **Wave 1 [now]:** move their UI to declarative v1 (task A7 confirms that each fits), then release them through the catalog. |
| wiki, explorer | **Wave 2 [later]:** need frames for their pages; the server modules (`orch_dashboard/wiki`, `explorer/`) need runner-side code. |
| github, publish (`network`) | **Wave 3 [later]:** runner-side network code under tier B; PAT custody stays with core. |

Until an add-on moves, its catalog entry points at the version bundled with core, so updates show up the same way.

## 7. Guard

`orch update` (all forms except `--check`), `orch addon install`, and staging all refuse `human_only` under
`ORCH_GRANT`/`ORCH_SESSION` (the shared helper from install-update U9). `addon.grant` already needs the owner's
signature, and an agent never reaches a human key (invariant 5). The host op `addon.stage` is refused for agent
callers by the host as well. The plugin guard denies these commands in Bash.

## 8. Dashboard: Versions and updates

- **Settings → Versions** (extends the About/versions block from T10). It shows orch core, dashboard and every add-on
  per workspace, each with installed, granted and latest version; an **Update available** badge; a **Needs approval**
  badge; and the changelog (data, capped).
- **Data:** a host op `versions.get` (read-only). The **host** fetches `channel.json` and `addons.json` only when
  asked: `orch update --check`, or **Check now** in Settings → Versions (host op `versions.check`, at most once a
  minute; it writes only the cache). The **background check is off by default** (owner decision 3). The signed
  setting `updates.auto_check` turns on a check every 6 hours. It goes through the usual `settings.set` review and is
  human-only like every `settings.set`. The host honours it from a workspace whose owner is this machine's person;
  setting it off there stops the check. The fetched data is the same either way. It verifies them before caching in `<state>/host/update-check.json` and
  serves only what it verified. The page never fetches GitHub: its CSP stays `connect-src 'self'`. A failed check
  shows "Last checked … (could not reach the release server)".
- **Core/dashboard update: a copy-the-command card, never a signed request from the browser.** Replacing the host,
  signer and dashboard binaries and restarting the runner is Tier S. A dashboard takeover (the D-P2-31 threat) must not
  be able to start a code replacement or stop agents. The terminal is the human-presence channel for this, the same as
  `orch runner enable`. The card shows `orch update` with a copy button (the command sits behind "Technical details"
  for non-technical users, with a plain line: "Open Terminal and paste this").
- **Add-on update: allowed in the dashboard,** because it is the existing signed grant. "Update" calls `addon.stage`
  (download and verify, no code runs), then opens the existing grant review in Settings → Add-ons with the diff from
  §4. It is signed by click-to-sign or passphrase as today. Installing a new add-on works the same way.

## 9. Threat model (adds to install-update §9)

| Threat | What stops it |
|---|---|
| Catalog key leaked | Cannot sign core (`purpose`). Its packages run only after a human-signed grant that shows capabilities. Revocation via root. |
| Update widens capabilities | The runtime check is digest equals grant. A widened package stays staged until it is signed. The review highlights `+capability`. |
| Malicious changelog text | Data only: capped, one-line rules, labelled "not part of what you sign". |
| Name squatting with a new add-on | Names come only from the signed catalog. An install shows title, name and capabilities. |
| Frame escape [later] | Opaque origin, no network, a digest-pinned bundle, and a bridge that can only *request* host-prepared, human-reviewed actions. |
| Core update breaks add-ons silently | Preflight, the `incompatible` state, and an explicit question. |

## 10. Tests

Units: the catalog vectors (same suite as release vectors, plus `purpose` confusion), the api-range and `min_core`
compare, the registry state `incompatible`, safe tar extraction (links, `..`, absolute paths, size bombs),
grant-from-staged under the lock (a digest swap between staging and grant → refused), crash recovery of the rename,
review diff lines, agent refusals, and `versions.get` with a fake fetcher (an unverified cache is never served), no network call while `updates.auto_check` is off and nobody asked, the `versions.check` rate limit. Dashboard:
vitest for the Versions view and the copy card, a Playwright check of the add-on update review on an isolated HOME. E2E:
`e2e/run.sh addon-update` (a test-channel catalog: stage wiki 0.2.0 → review → sign → active on the new digest; the
old version keeps running before the signature).

## 11. Tasks (about 30 min each; T0 in `plugins/orch-core` unless noted)

| # | When | Task | T0 |
|---|---|---|---|
| A1 | now | `addon-api.md`, schema `orch.addon/3` (`api`, `min_core`), `ADDON_API`, registry state `incompatible` | `uv run pytest tests/addons/test_manifest.py tests/addons/test_registry.py -x -q` |
| A2 | now | Catalog envelope (`purpose` check) on top of U2; `addon-release.yml` written (the owner pushes it) | `uv run pytest tests/update/test_catalog.py -x -q` |
| A3 | now | Package intake: download, safe extraction, digest check, staging dir | `uv run pytest tests/addons/test_intake.py -x -q` |
| A4 | now, Tier S | `addon.grant` from staged: lock, swap, recovery, `addon.stage` op, catalog codes and goldens | `uv run pytest tests/addons/test_grant_staged.py -x -q` |
| A5 | now | Review diff (capabilities ±, changelog label) in CLI and Settings → Add-ons | `uv run pytest tests/addons/test_review_lines.py -x -q` |
| A6 | now | `orch update addon` / `orch addon install`, core preflight | `uv run pytest tests/update/test_addon_update.py -x -q` |
| A7 | now | Host fetch and cache on request (`versions.check`, rate-limited), `versions.get`, the signed `updates.auto_check` setting (off by default) and the 6-hour timer | `uv run pytest tests/host/test_versions_op.py -x -q` |
| A8 | now | Dashboard Settings → Versions, "Check now", the auto-check switch (signed), copy card, update → stage → existing review | `cd addons/dashboard && npx vitest run src/app/settings/versions.test.tsx` |
| A9 | now | Declarative UI v1 schema plus renderer; port estimate, model-routing, usage | `cd addons/dashboard && npx vitest run src/app/addons/declarative.test.tsx` |
| A10 | later | Add-on frames and the `postMessage` bridge (Opus design review first) | (later) |
| A11 | later | Runner-side add-on code under tier B; waves 2 and 3 | (later) |

Order: after install-update U2: A1 → A2, A3 (parallel) → A4 → A5, A6, A7 (parallel) → A8 → A9. Opus security review:
A2, A3, A4, A5, A8. A4 and A7 add catalog codes and ops: run all of `tests/cli` and `tests/host/test_hostclient.py`
before pushing.

## 12. Owner questions

### Decisions (owner, 22 Oct)

1. **Background update check off by default.** New versions show up through `orch update --check` and "Check now"
   in Settings → Versions. The 6-hourly check is a human-only signed setting (§8; tasks A7, A8).
2. **Linux from the start** and **root key in Proton Pass "Agents"**: see install-update §12. The catalog key is
   certified by the same ceremony (install-update §4.5).

No open questions.
