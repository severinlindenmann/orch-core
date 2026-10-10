# orch v2: portable custody and Windows, D63–D66

Status: **draft for the owner's review**, 10 Oct 2026. Numbers continue after D58–D62 (draft PR #340). The owner has
decided that **Windows is a requirement** and that the prototype may use a simpler human credential than Face ID or
Touch ID. This amends D3, D46 and D49–D51 where noted and touches nothing else.

## 1. Why

The spec was written for macOS and Linux. Its guarantees lean on three Unix and Apple pieces:

- the Secure Enclave with user presence for human signatures (D46, `custody/`);
- a Unix socket with a peer-UID check (`getpeereid`, `SO_PEERCRED`) and modes 0600/0750 (§5.5);
- agents as a separate OS user (D3, D37, P1b).

None of them exists on Windows, and the third is not done on macOS either. Meanwhile the first prototype needs a
human gate that works the same on macOS, Windows and Linux. The aim is one design with the OS-specific part behind
a small interface.

## 2. Decisions

| # | Topic | Decision |
|---|---|---|
| D63 | Windows | Windows 10 and 11 are supported platforms for the CLI, the host and the dashboard, with Python 3.11+. The format, the protocol and the registry are identical on every OS. Mobile stays iPhone only. |
| D64 | Custody backends | Human signing goes through one interface in `custody/`: `sign(domain, payload) -> signature` and `presence() -> level`. Backends: `passphrase` (all OSes, the P1 default), `secure-enclave` (macOS), `tpm`/`windows-hello` (Windows, later), `webauthn` (dashboard, P2), `file` (VPS, no human signing). A backend is chosen per person key and recorded in the person's device certificate. |
| D65 | Prototype human credential | The `passphrase` backend. The person key is stored encrypted under a passphrase (scrypt, parameters in the key file). Human-only verbs (`approve`, `request-changes`, `answer`, `verdict`, `close`, `grant`) ask for it on a real terminal and refuse to run without a TTY. Nothing caches the passphrase or a bearer token in a file or environment variable. The recovery code (D49–D51) can re-create the key. |
| D66 | Factor in the record | Every signed event carries `auth`: `passphrase`, `secure-enclave`, `webauthn`, … A workspace policy may require a stronger factor for named verbs (for example `verdict`); the default accepts any. The weaker factor of the prototype is therefore visible in the log and can be refused later. |

## 3. What the prototype guarantees, and what it does not

| Threat | `passphrase` (D65) | `secure-enclave` / `webauthn` |
|---|---|---|
| Agent reads a key file and signs | Cannot sign without the passphrase | Cannot export the key |
| Agent approves through the CLI | Refused: human-only verb, no TTY, no passphrase | Refused: needs presence |
| You type the passphrase into a terminal an agent controls or can read | **Not protected** | Not applicable (the OS prompts) |
| Offline guessing of a stolen key file | Slowed by scrypt, as strong as the passphrase | Not possible |
| Physical presence per signature | No | Yes |

Rules that go with it:

- Human commands run in the person's **own terminal**, never through an agent. This is the existing rule; D65 makes it
  hold on every OS.
- The grant (A3) is signed with the passphrase once and handed to agents as `ORCH_GRANT`. It is the only secret an
  agent holds, it expires, and it is revocable.
- A bearer token for the human path is **not** used. If the dashboard later keeps a session after login, the token is
  short-lived, held by the host as an HttpOnly cookie, and never written to a file an agent can read.
- The prototype is declared a **dev tier** in `orch doctor` output. It must not be described as equal to hardware
  presence.

## 4. Host transport and OS differences

| Piece | macOS / Linux | Windows |
|---|---|---|
| Human path (CLI) | in-process, TTY prompt | same |
| Host connection (P2) | Unix socket, peer-UID check | named pipe with an ACL for the user and the agent user; falls back to a local socket plus a per-session token if the ACL route is not ready |
| Key store | Keychain, libsecret, 0600 files | DPAPI or Credential Manager for the workspace key; the passphrase key file is portable |
| File lock | `fcntl` | `msvcrt`/`LockFileEx` behind the same `store/` lock |
| Paths | as is | long-path and case-insensitive handling in `store/`; no symlinks in the format |

The host connection is an interface (`HostTransport`) with these implementations, so the operation registry and the
CLI do not change. No P1 code may call `socket.AF_UNIX`, `os.chmod` modes or `os.getuid` outside the transport and
custody modules.

## 5. Agent isolation across operating systems

Separate-UID agents (D37) stay a Linux-server property. For laptops on any OS, isolation is optional and in this
order of preference:

1. **A container or VM** with only the repository mounted and the host connection forwarded in (Docker or WSL2 on
   Windows, Docker or Lima on macOS). The same on every OS.
2. A harness sandbox profile where the harness offers one.
3. Nothing, with the guarantees of section 3 only.

This makes the "agents never read keys" claim exact: it holds for **signing keys** with every backend above (D65 or
hardware), and for the **workspace key** only with options 1 or 2 or on a VPS. The threat model (§4) and
[orch-v2-harnesses.md](orch-v2-harnesses.md) say so.

## 6. Phasing and tests

| Phase | What |
|---|---|
| P1 | `custody/` interface, `passphrase` backend, TTY enforcement, `auth` field in events and schema, `orch doctor` tier line, Windows in CI (the v1 README says CI already runs Ubuntu and Windows) |
| P1b | `secure-enclave` backend on Apple-silicon Macs; the macOS separate-UID work (D37) becomes optional |
| P2 | `HostTransport` with the named-pipe and Unix implementations, `webauthn` in the dashboard, the isolation recipes of section 5 |
| later | `tpm` / Windows Hello backend |

Tests, per OS in CI: agent without TTY is refused for every human-only verb; wrong passphrase leaves no event; an
event signed by `passphrase` is refused when the policy requires `secure-enclave`; key file round-trips between
macOS, Windows and Linux; no module outside `custody/` and the transport imports an OS-specific API.

## 7. Open points for the owner

- Whether verbs such as `verdict` should require a stronger factor once one exists (D66 allows it, the default does
  not).
- The scrypt parameters and a minimum passphrase length.
- Whether Windows needs the dashboard in the first prototype, or only the CLI.
