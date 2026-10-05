# Running the dashboard inside a frame

The dashboard works the same when it is shown in a sandboxed frame that has no address, cookie or storage of its
own. It never depends on them:

- **Own path.** The server renders the page's own path and query into `<html data-path>`. The scripts read it from
  there and never from the address bar. A soft page swap copies the new value in.
- **One adapter.** `static/app.js` opens with a small host adapter, published as `window.orchHost`. It is the only
  code in the dashboard's own scripts that touches the address, history, the theme cookie, web storage or the
  clipboard. A host sets `window.orchHost` before `app.js` runs; any method it provides replaces the local default.
  `static/terminal.js` uses the same object.
- **Local defaults do what the page always did.** Storage falls back to memory when the browser refuses it, and
  the theme cookie is written inside a try block. A link that opens a new tab or downloads stays a plain link
  unless the host's `openLink` or `download` returns true for it.
- **Page generation.** Every in-place page swap starts a new generation and cancels a queued live refresh, so a
  refresh requested by an older page never lands on a newer one.

`tests/test_dashboard_host_adapter.py` fails when other code in those scripts uses `location`, `history`,
`document.cookie`, web storage or `navigator.clipboard` directly.

## Routes that carry their own Content-Security-Policy

These responses are never rendered into the dashboard frame. Each one is served with its own, stricter policy
(a sandbox with no script access to the dashboard, or a frame policy for widget documents) and is opened or
embedded by the page that links it, not swapped into it. Every other route gets the dashboard's page policy
(`script-src 'self'`, no inline scripts). `/t/{ref}/raw` is an ordinary dashboard page and is not in this list.

| Route | What it serves |
| --- | --- |
| `/a/{ticket}/{name:path}` | a ticket's artifact files |
| `/w/preview/{ref}` | a widget preview document |
| `/w/{ref}/{section}/{digest}` | a ticket widget frame |
| `/wp/{addon}/{digest}` | an addon's wiki page widget frame |
| `/wpf/{addon}/{digest}` | files used by a wiki page widget |
| `/addons/{name}/files/{token}` | an addon's download |

`tests/test_dashboard_host_adapter.py` derives the routes that set their own policy from the application's route
list and fails if one is missing from this table, or if the table names a route that does not exist.
