# github-reviews (default orch addon)

Open pull requests of the harness repo and every repo in `git.repos`, on Mission Control's **Code reviews** page.

- **Page:** repo cards (role, provider, local branch, ahead/behind the default branch, uncommitted files, commit check, open / need review / failing), filters by state (default "Needs your review") and repo, pull requests grouped by repo with checks, review, size and linked ticket, and "One ticket, several repos" with a suggested merge order from `blocked_by`.
- **Today:** a "Checks failing" tile, and "Review requested on …" and "Checks failing on … · Ask agent to fix" under From addons.
- **Ticket:** the ticket's pull requests across repos in the Code panel. Failing checks switch Start agent to "Fix failing checks".
- **Actions** (human clicks, with a confirm dialog): Rerun failed (`gh run rerun <run> --failed`), Mark ready (`gh pr ready`).
- **Binaries:** `gh` (signed in with `gh auth login`) and `git`, only through `ctx.run`. **Env passed through:** `GH_HOST`, `GH_TOKEN`, `GH_CONFIG_DIR`.
- Repos whose `origin` is not on github.com show "no provider for this host". No `gh` call is made for them.

Enable it per workspace in Mission Control → Workspace & addons.
