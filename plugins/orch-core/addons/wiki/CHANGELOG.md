# Changelog

## 0.2.0

- Local folder provider (`local`, default folder `orchestrator/wiki`, inside the workspace): list, search and read Markdown pages in Mission Control through the new core `Markdown` widget; related pages and may-need-an-update hints work as with the GitHub wiki.
- Create page from ticket: writes `decisions/<KEY>.md` from the ticket's Summary and Requirements, marked as a copy (not an approval) with the requirements text hash. It does not commit: orch never commits itself.
- The local folder may not be hidden or one of orch's own record folders (`orchestrator`, `orchestrator/tickets`, `artifacts`, ...).
- Relative links between pages of the folder open the linked page; links that leave the folder stay text.
- Pages and hints now come only from the selected provider.

## 0.1.0

- First version: github-wiki pages provider, related pages on tickets, docs that may need an update, Wiki page with search, Confluence stub.
