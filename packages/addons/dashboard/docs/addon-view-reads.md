# Declarative add-on views: named reads (design D7)

A declarative node never carries a URL or code. `tree` and `search` name a **read**, `<addon>.<name>`, and the dashboard
server answers it from a registry (`server/src/orch_dashboard/addon_reads.py`). Add-on JS never runs in the dashboard
and no capability is needed.

| Part | Rule |
| --- | --- |
| Name | `^[a-z][a-z0-9_]{0,31}\.[a-z][a-z0-9_]{0,31}$`; the node's add-on must own the prefix (checked in `useViewRead`, and the server only knows its own registry). |
| Transport | `GET /api/w/{ws}/view/{name}?arg=...`. Read only. Args are a closed pydantic model (`extra=forbid`); a bad arg is `invalid.input`, an unknown name `not_found`. |
| Access | Checked on every call: the add-on is ON in the workspace, the caller is a member, the repository is confirmed (`wiki.scope.open_wiki`). Files are read at `HEAD` through the existing path-safety helpers. |
| Limits | Declared per read (`Read.limits`) and enforced by it: tree 500 nodes, page 200 KiB (larger: `markdown: null`), search 50 hits and a 200-character query. |
| Results | Validated again in the browser (`addon-ui/viewRead.ts`); a wrong shape is an error state. |

Reads of the Wiki add-on:

* `wiki.tree` args `{parent?}` -> `{state, message, nodes:[{id,label,hint?,badge?,children?}], total, cut, offer?}`.
  `children: true` means a further read with `parent=<id>` lists them (lazy). The wiki folder is flat, so none do.
  `offer` is one plain add-on action (`create`) shown to owners and maintainers when the project has no wiki folder.
* `wiki.page` args `{id}` -> `{id, title, markdown|null, updated, by}`.
* `wiki.search` args `{q}` -> `{results:[{id,label,snippet}], total, cut}`.

The view itself is `packages/addons/wiki/views/wiki.json` (nav slot). The dashboard server carries a byte-identical copy
(`orch_dashboard/wiki/view.json`, pinned by a test) and lists it at `GET /api/addons`.
