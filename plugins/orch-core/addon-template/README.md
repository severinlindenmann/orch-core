# hello-status (orch addon template)

A minimal, runnable orch addon. It shows the harness repo's branch, upstream and number of changed files on its own Mission Control page, from one `git status` per refresh.

- **Capabilities:** provider (`git-status`, kind `status`), page (menu "Hello status"), settings (`greeting`).
- **Binaries:** `git`, run through `ctx.run` only.
- **Settings:** Greeting (text, default "Hello"), saved per workspace in Workspace & addons.

## Make your own

1. Copy this folder, e.g. to `~/code/my-status/`.
2. Rename the package folder `hello_status/` to `my_status/`.
3. In `orch-addon.json` change `name` (`my-status`), `title`, `entry` (`my_status:create`), `menu.title` and `description`; start at version `0.1.0`.
4. In the tests, change the `hello_status` imports to `my_status`.
5. Run `orch addon check ~/code/my-status --strict` and `uv run --project <orch-core plugin folder> pytest ~/code/my-status/tests` until both pass.
6. Install it: `orch addon install ~/code/my-status`, then trust and enable it in Mission Control → Workspace & addons (or `orch addon trust my-status` and `orch addon enable my-status` in your own terminal).

Read `ADDONS.md` in the orch-core plugin folder for the full API, the item formats and the security rules, and `DESIGN.md` next to it before you draw anything: which widget fits which data, the copy rules and the design warnings `--strict` turns into failures. Mission Control's `/design` page shows every widget as core draws it.
