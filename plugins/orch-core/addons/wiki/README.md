# wiki (orch default addon)

Shows wiki pages next to the work: related pages on each ticket, a hint when a ticket's branch changed files a page documents, and a Wiki page with recent changes, pages linked from open tickets and search. It reads a GitHub wiki or a Markdown folder in the workspace; the only thing it writes is one new page when you press Create page from ticket.

- **Capabilities:** providers (`github-wiki`, `local` and `confluence`, kind `pages`; `branch-diffs`, kind `status`), page (menu "Wiki"), panel `ticket.pages`, decisions ("may need an update" on Today, Dismiss), settings, actions Dismiss and Create page from ticket.
- **Binaries:** `git`, through `ctx.run` only: `clone`/`fetch`/`reset`/`log` inside the addon's own clone of `https://github.com/<owner>/<name>.wiki.git` (in `orchestrator/.state/addons/wiki/git/`), and `symbolic-ref`, `rev-parse`, `diff --name-only` in your local repos (never fetch, checkout or write there). The local provider and Create page from ticket use no git.
- **Settings:** Wiki provider (`github-wiki`, `local`; `confluence` is a stub that says it is not available yet), GitHub repos (default: the harness repo's GitHub origin), Local wiki folder (default `orchestrator/wiki`, used by `local`).
- **Local folder (`local`):** pages are the `.md` files in the folder, nested folders included (the page id is the path without `.md`, so a folder can be pushed to a GitHub wiki later). The folder must be inside the workspace: absolute paths, `..`, hidden folders, symlinks and orch's own record folders (`orchestrator` itself, `orchestrator/tickets`, `artifacts`, `temporary`, `static`, `share`) are refused. Pages are read with Python on Refresh, with no git; the "updated" time is the file's modification time. The Wiki page lists them with search and opens one full width, drawn by the core `Markdown` widget (raw HTML in a page is shown as text; links follow the ticket-text rules, and a link to a ticket artifact, `/a/...`, `artifacts/...` or `artifact:`, is shown as text, since no approval binds a wiki page). A relative link to another listed page (`other.md`, `sub/other.md`, `../Home.md#part`) opens that page; one that leaves the folder, reaches a symlinked or unlisted file or is not a page stays text. `[[Wiki links]]` are not supported. Reading never writes: an empty or missing folder shows an empty state and nothing is created.
- **Create page from ticket:** on a ticket without a page `decisions/<KEY>`, the Wiki panel offers it (provider `local` only). After you confirm, it writes `<folder>/decisions/<KEY>.md` (front matter title, the ticket's Summary and Requirements, an empty Decisions heading), never over an existing file. The page opens with a note that it is a copy of the ticket text on that day, pinned by the requirements hash, and not a record of approval: approval is shown only on the ticket. It is not committed (orch never commits itself); review it and commit it with your own changes.
- **Page front matter:** a page may list the files it documents, repo-relative, optionally prefixed with a `git.repos` name:

  ```yaml
  ---
  title: Architecture overview
  documents:
    - src/acme/ingest/**
    - acme-energy-data:sql/*.sql
  ---
  ```

  When a ticket in testing or done has a branch that changed such a file (or a file the page links to on GitHub), the ticket and Today show "<page> documents files this ticket changed — may need an update".
- **Limits:** shallow clone (`--depth 200`), at most 500 pages, 256 KiB per page, 5 repos. The local folder has the same page limits. Symlinks in the wiki are ignored.

Tests: `uv run pytest -q addons/wiki/tests` from the orch-core plugin folder.
