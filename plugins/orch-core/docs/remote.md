# Orch Remote: the dashboard on your other devices

`orch serve --remote` starts the dashboard as usual, on this machine only, and also lets the devices you paired reach
it through TIX, end to end encrypted. Nothing listens on the internet: the host dials out to the TIX mailbox, takes
sealed requests, answers each by running the dashboard in memory, and posts sealed answers back. The protocol is the
orch-tix bridge protocol (`docs/bridge-protocol.md` in orch-tix).

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
- anything that needs a fresh confirmation on the device (Face ID, Touch ID, Windows Hello or a PIN) is refused
  until the AI Factory change adds those confirmations; that includes every change to a ticket under a running
  AI Factory epic.

## The limits of the protection

The operating-system user is shared with every agent on this computer. File permissions therefore protect nothing
against a local agent; orch's command guard refuses agents' commands and file-tool calls on the bridge's records and
refuses agents running `sharing bridge-key` or `sharing bridge-host`, also when the name is split by quotes,
escapes, variables, substitutions or globs, or wrapped in another shell. The guard reads command text, so it deters
careless or accidental access; it is a deterrent, not a wall: a command that assembles the name at run time, or a
program outside the agent's tools, gets past it. The start-up
listing and the audit log expose registry changes made that way, as long as the audit log itself was not rewritten
too.

## Damaged records

The host never replaces a damaged host key or registry silently: that would unpin or drop every paired device. A
start with a damaged host key refuses; a damaged registry is shown at start and answers nothing. Pairing again from
scratch is the owner's decision, made in the Remote tab.
