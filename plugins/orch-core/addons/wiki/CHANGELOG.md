# Changelog

## Unreleased

- Search shows the passage that matched, with the matched words in bold, and a Clear search link; quotes around words are ignored. A page is no longer "related" to a ticket because of a word that is on most pages (such as the harness repo's name). `confluence`, a stub, is no longer offered in the provider select. The search index now keeps the page text in its own case (indexes from earlier versions keep working, with lowercase excerpts until the next refresh).
- Core: a provider's snapshots for scopes it no longer lists are dropped, so a failure from a provider that is no longer selected does not stay on the page.

## 0.3.0

- Pages show ticket widgets: an ```orch block on a local page is drawn like on a ticket (same chrome, text alternative and sandboxed frame; an invalid block shows an error naming the page path and line). Page files are pinned by digest and live in `_files/` of the wiki folder. Search, excerpts and ticket mentions read each block's text alternative, not its JSON. GitHub wiki pages are not drawn in orch, so they are unchanged.
- Create page from ticket now copies the widgets of the ticket's Verification and Findings (with the files they pin, digest-checked, into `_files/`) and COMMITS the new files, and nothing else, when the wiki folder is in a git repository. This is the one thing orch commits on its own, by the owner's decision. Failures (no repo, hook, ignored path, no identity) are reported on the page and leave the index as it was.
- Needs orch-core with API 2.3 (`Markdown(widgets=True, files=...)`, `ctx.page_widget_text`, `ctx.ticket_widget_copy`).

## 0.2.0

- Local folder provider (`local`, default folder `orchestrator/wiki`, inside the workspace): list, search and read Markdown pages in Mission Control through the new core `Markdown` widget; related pages and may-need-an-update hints work as with the GitHub wiki.
- Create page from ticket: writes `decisions/<KEY>.md` from the ticket's Summary and Requirements, marked as a copy (not an approval) with the requirements text hash. It does not commit: orch never commits itself.
- The local folder may not be hidden or one of orch's own record folders (`orchestrator`, `orchestrator/tickets`, `artifacts`, ...).
- Relative links between pages of the folder open the linked page; links that leave the folder stay text.
- Pages and hints now come only from the selected provider.

## 0.1.0

- First version: github-wiki pages provider, related pages on tickets, docs that may need an update, Wiki page with search, Confluence stub.
