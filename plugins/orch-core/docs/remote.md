# Orch Remote: the dashboard on your other devices

`orch serve --remote` starts the dashboard as usual, on this machine only, and also lets the devices you paired reach
it through TIX, end to end encrypted. Nothing listens on the internet: the host dials out to the TIX mailbox, takes
sealed requests, answers each by running the dashboard in memory, and posts sealed answers back. The protocol is the
orch-tix bridge protocol (`docs/bridge-protocol.md` in orch-tix).

To check the whole path, `docs/remote-validation.md` has the automated end-to-end run (two dashboards, a local TIX and
one Chromium; `-m e2e_remote`, not in the default test run) and the short list to repeat on a real phone. A relay on
this machine (`http://localhost:<port>`) is accepted for that; any other relay must be `https`.

## Running it

```
orch serve --remote            # in the workspace, in your own terminal
orch serve --remote --take-over   # another host serves this workspace: take it over
```

- It keeps the loopback address (127.0.0.1) and opens the local page as usual (`--no-open` does not).
- `--remote` and `--lan` cannot be combined, and `--remote` refuses a bind address off this machine.
- The update prompt at start appears only when a terminal is attached. Nothing is ever updated over the bridge.
- Ctrl-C (or SIGTERM) stops the dashboard and the link: open streams end, TIX is told the host stopped (a goodbye),
  and the host's lease on the workspace is released.
- Without `--remote` the dashboard behaves exactly as before and loads none of the bridge code.

## What is checked before anything binds

The preflight names everything that is missing at once, each with its fix, and exits with code 8:

- the Python package `cryptography` (the dashboard extra);
- a usable orch config directory, outside every workspace: the bridge's records live in its guarded permits folder;
- the relay addon enabled in this workspace and trusted: exactly one installed addon that pairs phones and names one
  command-line tool in its settings (for TIX, the orch-tix addon and its `sharing_path` setting). orch-core itself
  holds no relay-specific code;
- that tool (an absolute path to an executable);
- this device approved on the relay, a space there for this workspace (`sharing space show`), and this device as
  that space's owner.

Then the workspace channel key is read from `sharing bridge-key`, with its output piped into this process. It is
held in memory only: never written, printed or logged. The host signing key, the device registry, the request store
and the audit log live in the config directory's permits folder (`permits/bridge/<space>/`), never in the workspace.

TIX itself is reached only through a long-running child process, `sharing bridge-host`, which holds this device's
TIX credentials. The dashboard process never holds a TIX credential: it passes sealed bytes to the child over a pipe.

## What is shown

Every remote start prints:

- the paired devices, with their scopes;
- every change to the device registry since the last start, from the audit log, and a warning when earlier audit
  entries were rewritten or removed;
- damage the owner must know about: a registry that cannot be read (every request is then dropped) or damaged
  request records (kept, never run).

While it runs, a fatal answer from TIX stops the link with a message, and the local dashboard keeps running:
another host serving the workspace (start with `--take-over` to take it over), the workspace taken over by another
host, this device revoked or not yet approved, not the owner of the space, or the space gone. Network trouble and
TIX's rate limits are retried, waiting 1 s at first and up to 10 s.

The Remote tab (a later change) reads the link's state from `app.state.bridge_link` (off, connecting, online,
reconnecting, stopped, error, with a fixed error code) and offers the kill switch: nothing more runs, open streams
end, and the link stops talking to TIX, while the local dashboard keeps running.

## The status page

Every 10 seconds the host sends TIX a heartbeat with counts only, derived from the workspace each time: the running
terminal sessions, the tickets in progress, the needs-you count, the AI Factory state (none, running, paused,
waiting, ready, stopped or done, of the factory epic that most needs you) and, for that epic, its children done and
in total and the share of its budget used. No names, titles or text travel in a heartbeat.

## What a device can do

Each request is checked in the protocol's order (signature, device, replay, sequence, time, stream owner) and
recorded before anything runs; a request id never runs twice. The route's scope is then checked against the
device's scope (Look, Decide, Operate, Type), and the dashboard's remote gate decides the route again on the request
as received. Authorisation is asked again immediately before anything runs and before every chunk of a stream is
sent, so a revoked device is cut off mid-stream with a sealed refusal.

Limits:

- at most 8 pages and 4 streams per device at once; one more is refused `busy`;
- a response is sent in chunks of at most 256 KiB, a stream's frames at most 4 per second (the latest frame wins)
  with a keepalive after 20 s of silence; a stream ends after 120 s, and the device opens it again;
- an answer of up to 64 KiB is stored for a retry; a larger one is not, and a retry of it reads "outcome unknown";
- anything that needs a fresh confirmation on the device (Face ID, Touch ID, Windows Hello or a PIN) shows the exact
  text first and is signed over it; see the next section.

### The AI Factory from a device

Deny, Revoke and Pause (which is also Stop) need Decide. Allow once, Allow for the epic, Start (the approve route
carrying the factory limits), signing an epic's done verdict and any change to a ticket under a running AI Factory
epic need Type and a fresh confirmation over the exact thing: the permission request, the charter hash, the verdict
hash the Ready report carries, or the request itself. The host builds that text from the workspace's live state when
the request arrives: if the hash the request carries is not the current one, the request is answered already, the
form names a field twice, or the text is too long to show in full, no confirmation is asked for and nothing is
written. The write is the dashboard's own signed ledger write, run after the confirmation, which checks the same
hash again; the paired device is named in the ledger entry and, for a Start, in the armed marker. Nothing waits
while the computer is away. Reading and commenting stay at Operate; a typing lease never covers these.

## The limits of the protection

The operating-system user is shared with every agent on this computer. File permissions therefore protect nothing
against a local agent; orch's command guard refuses agents' commands and file-tool calls on the bridge's records and
refuses agents running `sharing bridge-key` or `sharing bridge-host`, also when the name is split by quotes,
escapes, variables, substitutions or globs, or wrapped in another shell. The guard reads command text, so it deters
careless or accidental access; it is a deterrent, not a wall: a command that assembles the name at run time, or a
program outside the agent's tools, gets past it. The start-up listing and the audit log expose registry changes made
that way, as long as the audit log itself was not rewritten too.

What this change does not guard:

- The relay tool's own files. For TIX, the master key and the device token sit in the workspace's sharing
  configuration, which orch-core does not guard. An agent that can read them can derive the workspace channel key
  without `bridge-key` at all, so the `bridge-key` rule protects less than it may seem. A stolen channel key lets its
  holder read every bridge envelope it obtains, terminal output included (spec D1); it cannot act as a device,
  because every request still needs a registered device's signature. Relay access without the key gives denial of
  service and traffic metadata.
- The relay tool itself. When it lies inside the workspace (as the sharing skill usually does), an agent can change
  it, and `--remote` then runs that code with this device's relay credentials. The start warns about this; keeping
  the tool outside every workspace avoids it.

The rule also refuses more than it needs: any agent command with `bridge-key` or `bridge-host` as a word is refused,
a search such as `grep -rn bridge-key docs/` or a commit message that names the command included. Agents can use the
file tools (Read, Grep) for such searches.

Known limits of the host loop itself: a response chunk retried after a lost answer may reach the mailbox twice (the
device keeps the first by its index); a page's chunks are sealed after its outcome is stored and posted over the next
seconds without another authorisation check, as the protocol specifies; a cancelled stream's final, empty chunk is
posted without one; and the host's record writes run on the dashboard's event loop, so a slow disk (or a record lock
held elsewhere, up to 10 s) delays pages for that moment.

## Damaged records

The host never replaces a damaged host key or registry silently: that would unpin or drop every paired device. A
start with a damaged host key refuses; a damaged registry is shown at start and answers nothing. Pairing again from
scratch is the owner's decision, made in the Remote tab.

## Terminals over the bridge

Terminals work from a paired device on the same routes as at the desk, each tagged in the remote gate's table:

- Watching (the list, a session, its stream and its JSON snapshot) needs Operate: live output can hold secrets.
  Look and Decide devices cannot watch.
- Typing needs Type and a typing lease: sending keys, resizing and ending a session. The host asks the device for a
  fresh platform-authenticator assertion (Face ID, Touch ID, Windows Hello or a PIN), checks it with the host
  library, and binds the lease to that device. The sheet reads "Type into terminal NAME for 15 minutes", naming the
  terminal the request is for, and says in the same text that the unlock lets this device type into every terminal on
  this computer for 15 minutes. The lease ends 15 minutes from the unlock: a key post inside it does not extend it.
  When it ends the host answers `lease_required` and the device client must ask the person for the assertion again.
  The lease covers only input sent on a stream the device itself opened; a revoke or a change of scope ends it, and
  so does a restart. A device with no platform authenticator never gets one, so it can watch but not type. Limit: the
  binding is to a stream this device opened, not to a person pressing a key, so a hostile page served to the device
  can open a hidden stream for any terminal itself and type into it for the rest of the lease.
- Starting is never on the lease. A new session (`POST /terminals/new`) and Start agent on a ticket or a quick task
  each need Type and their own fresh assertion, every time, over a sheet that says what starts: "Start a new
  terminal session NAME running HARNESS with no prompt, in FOLDER. Command: ..." or "Start an agent. Harness H, mode M, in WHERE as session NAME. Ticket REF titled: "TITLE"" (the title is
  quoted and last, because an agent can write it; the quick-task sheet is the same with the task). The
  sheet's digest covers the fields the route reads from the request (ticket or task and its title, harness, mode,
  `where`, `another`, `next`), the terminal it will use, the free session name and, for a new session, the command
  and folder, plus what an addon with a launch plan chooses (its model, environment, prompt note and label: the sheet
  prints the model, the environment variable names and that a note is added, never the values), all from one look at
  the workspace and the urlencoded body only, the
  way the route reads it: a field given twice, a query-only field, a body that is not ASCII form data or holds a
  ";" gets no sheet and nothing starts. The request waits parked; when the assertion arrives the host builds the
  sheet again and runs the request, once, only if the text and digest are unchanged (a session name taken or a title
  edited meanwhile is refused as `assertion_failed` ("changed"), audited as a failed assertion, and starts nothing).
  Not bound by the sheet, because the route reads them from the host when it runs: the mode's prompt text and a linked
  pull request, and the release of a stale claim on the ticket, which the route does before it starts. The session
  name can also still move after that last check (the route picks the free name again when it runs, so a sheet
  naming `scratch` can start `scratch-2`); nothing else about the start changes. An unlock for typing never authorises a start, and a start's assertion opens no
  lease. A Decide or Operate device cannot start.
- Watching is read-only. The terminal page served through a host never posts `/size` for the page opening, a
  resize, a rotation or the phone keyboard (asking for a size is a lease route, so it would pop an unlock sheet
  nobody asked for); only the page's view and zoom buttons do, as something the person pressed. Locally the page
  fits as before. The consequence: a phone that only watches leaves the session at the size the desktop gave it
  until the person taps a view or zoom button. Other Type routes (a schedule's Run now, an addon action) are unchanged: Type alone.
- Key posts carry `seq` (the keys, in order), `page` (a name the page picks for itself) and `n`, a number that must
  rise with every post of that page. A post whose `n` was already used or is older than one already taken answers
  409 and types nothing, so a retry or a late arrival never types twice; two tabs of one device count apart. At
  most 64 items and 2048 characters of text per post (a longer paste is cut into several posts by the page), and 11
  posts per 10 seconds per device (429, without using the number).
- The bridge host keeps at most 1024 request records per device for 900 seconds, so a device can sustain about 1.14
  requests a second. The page therefore sends at most one key post a second (`window.orchHost.remote` is true when a
  host serves the page; the batch interval is 1000 ms), one at a time and in the order typed; a post that did not
  arrive, or was rate limited, is sent again unchanged with the same `n`. Polling the snapshot adds to the count, so
  it runs only while the stream is not open, at most every 2 seconds, and never while keys are waiting or out: the
  sustained rate stays under 1.1 a second either way. A host that serves the page sets `remote: true` in its adapter.
- Browser EventSource reconnects while the stream is bad also count as bridged requests (about one every 3
  seconds), so typing during an outage can reach the per-device record quota after about 13 minutes; the device client
  must back off its reconnects (tracked in #239).
- If the stream is not open (it errored, or is reconnecting), the page shows a snapshot from
  `/terminals/<name>/snapshot`. Each poll's answer holds the screen (up to 64 KiB) and is written to the host's
  on-disk replay store for 15 minutes.
- A revoke or a change of scope ends the device's terminal streams at once, with a sealed `revoked` or
  `scope_changed` refusal.

Sessions are matched to a workspace by directory: a session belongs to the workspace when its start folder is the
workspace root or below it. Where one workspace sits inside another, the outer one lists the inner one's sessions as
well (the inner one never lists the outer one's).
