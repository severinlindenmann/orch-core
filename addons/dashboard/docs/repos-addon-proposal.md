# Repos addon — preview proposal

Owner request, 2026-10-10 (U3); aligned with the v2 format in the review round of 2026-10-11. A workspace is often a
harness folder holding several independent repositories. Repos shows that structure, checks it, and clones what is
missing. All git operations in the mockup are simulated; no network, no credentials.

## Declared and observed

**Declared = the workspace's `settings.repos`** (`config.json`, ticket format §2): repo name → working copy
`{path}`, the path absolute or relative to the workspace folder. It is owner-signed (`settings.changed` with
`set.repos`, §5.4.2; `null` removes a name; two repos resolving to one path are refused) and it is the list
`links.repos` must name (§5.11). There is no second list: the addon's state holds only what it **observed** at the last
check (git or not, origin, current branch, ahead/behind, uncommitted changes, last fetch, size, worktrees), clone jobs,
its activity log, its settings and an owner's unsigned draft. A git folder in the workspace folder that no entry names
is **untracked**. Repo identity is read from git (§5.7), never from the declaration.

States: `present`, `missing` (declared, no folder), `not a repo`, `remote differs` (only when a remote is declared),
`untracked`; a running or failed clone shows `queued`, `cloning n%`, `clone failed`.

### Proposal (format amendment): `remote` and `default_branch`

Cloning needs a source the format lacks. Proposed: two optional keys per entry,

```json
"repos": {"billing-api": {"path": "billing-api", "remote": "https://git.example.test/acme/billing-api.git", "default_branch": "main"}}
```

`remote` is credential-free (HTTPS, `ssh://host/path` or `git@host:path`; any userinfo, query, fragment, non-ASCII or
a part starting with `-` is refused) and is not identity: identity stays what git reports. An entry without `remote`
is valid (the format's own shape, e.g. DEMO's `acme-energy-dbt`): it is shown and checked but cannot be cloned.
Until the owner ratifies the amendment, the mock writes the two keys into `settings.changed` as proposed fields.

## Who does what

| Action | Who | Signed | What changes |
|---|---|---|---|
| Declare a repo, declare an untracked folder, remove a declaration | **owners only**, a person (never an agent, never the addon on its own) | yes (`sign`; remove: `destructive`, or `options` "Remove anyway") | core's `settings.changed` on `settings.repos`, signed by the owner |
| Clone, clone all missing, the Today clone decision | maintainers and owners | yes (`sign`): every remote, target folder and the git login in full | the folder at the declared path only |
| Fetch, fetch all, check now | members and up | no | tracking data / the observed snapshot |
| Status, ahead/behind, dirty, activity | anyone who can see the workspace | — | read-only |

Removal never touches the disk; core's dialog says so in core's words ("The folder and its files stay on disk."),
because the removal is core's own settings change. Removal is refused (409, with the count) while open tickets **the
signer can see** link the repo; "Remove anyway" is a separate options confirm. Restricted tickets the signer cannot see
are never counted or revealed; per §5.11 a stale repo then blocks only edits that touch links.

## Clone identity (D56 A, D55)

Clone and fetch run with the CLI's own login of the user running orch, declared as a connection: on the dev machine
the workspace bot account (owner's answer: "workspace bot account"), in a real workspace the owner's (or, from P2,
the agent user's) git/gh login. The Structure tab shows "Clones as: gh · orch-agent-acme on github.com · OS user
orch-agent"; the signature binds the connection name (`clone_as`). **orch stores no git credentials**: no tokens in
remotes, drafts, events or signatures. Without a git-login connection nothing clones (409 `repos.no_login`).

## What the real host must implement

- `settings.changed` for `set.repos` exactly as §5.4.2 (owner-only, same-path refusal), plus the two proposed keys.
- Clone as `git clone -- <remote> <path>` (with `--`), never through a shell, into the resolved declared path only;
  refuse an existing folder, symlink escapes and races (lock the destination); durable progress and cancel.
- Fetch (`git fetch` only: never pull, merge or touch local changes), the scheduled check (off / 15 min / 1 h / daily,
  independent of any browser), worktree and size measurement, and a dock shell with `cd -- '<path>'` typed, not run.
- Provisional addon events, actor `addon:repos`, the requesting person recorded separately: `repos.checked`,
  `repos.clone_queued`, `repos.cloned`, `repos.clone_failed`, `repos.fetched`, `repos.terminal_opened`. Declaration
  changes are core's `settings.changed`, not addon events.

## Open points for the owner

- Ratify `remote` / `default_branch` as a format amendment (or keep sources outside the format).
- Should a "primary" repo exist (the brief asked for it; it is not in the format and was left out)?
- Remote canonicalisation for duplicate checks (host lowercase, `.git` and default ports ignored, scp ≡ ssh ≡ https).
- Whether `remote differs` should block agent starts on tickets that link the repo.
- Ratify the provisional event names and the `network`/`pty` capability boundary.
