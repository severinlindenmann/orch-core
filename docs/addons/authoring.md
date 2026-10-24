# Writing an orch add-on

This guide takes you from an empty folder to an add-on someone else can install, with one small example: **Changelog**,
which proposes a changelog line when a feature ticket is done. It asks for no permissions. The files of the example
are in [`examples/changelog/`](examples/changelog/); a test builds exactly these files, packs them, lists them in a
local test marketplace and installs them in a dev workspace, so the steps below are known to work.

Reference: the manifest and the runner protocol are in [addon-api.md](../architecture/addon-api.md), the marketplace
(index, digest, install flow) in [orch-v2-marketplace.md](../architecture/orch-v2-marketplace.md).

## The six steps

| # | Step | Command | Who |
|---|---|---|---|
| 1 | Scaffold | `orch addon new changelog` | author |
| 2 | Write it | any language: read one JSON line on stdin, write one back | author |
| 3 | Try it | `orch addon dev ./changelog` in a dev workspace | author |
| 4 | Check it | `orch addon pack` validates a commit and prints the digest and the index entry | author |
| 5 | Publish | push the commit, put the entry in a marketplace's `marketplace.json` | author or marketplace owner |
| 6 | Install | `orch addon install changelog`: review, then the owner signs | workspace owner |

## 1. Scaffold

```sh
orch addon new changelog
```

makes `./changelog/` with `orch-addon.json` (a manifest that passes the validator), `main.py` (a main file that
answers the runner) and `README.md`. It never overwrites a folder that exists. It needs no host and no key, so it
works anywhere; an agent may run it too.

## 2. Write it

An add-on is a program that the runner starts for one request and stops again. The host writes one JSON-RPC line on
stdin; the add-on writes one line back. It receives the ticket's key, type, status and its own fields, nothing else.
It holds no key: it **proposes**, and the host checks the proposal against the grant (only the add-on's own fields,
sections and artifact kinds) and signs it itself.

Everything the add-on needs is in the package, a folder of plain files (the package rules, ticket-format 8.1): at
most 256 files and directories, 4 MB per file and 16 MB in all, names of ASCII letters, digits, `.`, `_` and `-`, no
links, no submodules, no hidden files (a leading dot), and no compiled files (`.so`, `.dylib`, `.pyc`...) because
the owner must be able to read every file. `{pkg}` in `entry.cmd` is replaced by the folder the runner staged.

`orch-addon.json` of the example:

```json
{
  "schema": "orch.addon/3",
  "name": "changelog",
  "version": "0.3.1",
  "title": "Changelog",
  "api": ">=1.1,<2",
  "min_core": "2.0.0.dev0",
  "entry": {"cmd": ["{python}", "{pkg}/changelog.py"]},
  "capabilities": [],
  "fields": {
    "line": {"type": "string", "max_len": 200, "set_by": ["addon", "member"]},
    "skip": {"type": "string", "max_len": 40, "set_by": ["member"]}
  },
  "agents_md": "Changelog: the field changelog.line is the user-facing summary of a finished feature. Keep it one sentence."
}
```

`capabilities` is empty: the owner reads "no extra permissions". The closed list is `git_push`, `network`, `pty`,
`serve_http` and `spawn_agent`; ask for the least you need, because the owner signs exactly this list.

`changelog.py`, the whole add-on:

```python
"""orch add-on: one JSON-RPC request on stdin, one response on stdout.
The host sends only the ticket's key, type, status and this add-on's own fields. The add-on holds no key; it
*proposes*, the host checks the proposal against the grant and signs it itself."""
import json
import sys


def propose(params):
    ticket = params["ticket"]
    fields = ticket["fields"]
    if ticket["type"] != "feature" or ticket["status"] != "done":
        return {}  # nothing to propose
    if fields.get("line") or fields.get("skip"):
        return {}
    return {"set": {"line": "Shipped " + ticket["key"] + "."}}


request = json.loads(sys.stdin.readline())
if request["method"] == "propose":
    reply = {"jsonrpc": "2.0", "id": request["id"], "result": propose(request["params"])}
else:
    reply = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "no such method"}}
sys.stdout.write(json.dumps(reply) + "\n")
```

A marketplace add-on must be `orch.addon/3` (`api` and `min_core` are required) and must not use `runtime` yet.

## 3. Try it: `orch addon dev`

```sh
cd changelog && git init -q && git add -A && git commit -qm "Changelog 0.3.1"
orch addon dev .            # in a DEV workspace, at your own terminal
```

`addon dev` reads the **commit** (`--commit REV`, default `HEAD`; `--subdir D` for a package inside the repo), shows the
same review as an install, and asks you to sign the same `addon.grant`. Differences from an install:

- it works **only in a dev sandbox workspace on a dev host**; every other workspace refuses it (`host.dev_refused`),
  and so does a dev host with `ORCH_NO_CONVENIENCE` set;
- it is **never recorded as an install**: no origin, no publisher key, no rollback floor, and `orch addon list` shows
  it like any granted add-on while `orch addon update --check` does not know it;
- it is a person's command: an agent is refused (`market.actor_refused`) before the host is asked;
- it cannot take a built-in or official name (`market.reserved_id`), or the name of an add-on installed from a
  marketplace (`market.origin_changed`).

Uncommitted edits are **not** in the package: commit, then run `orch addon dev .` again. That is deliberate, see the
next step. To stop, `orch addon disable changelog`, then `orch addon purge changelog` for its data.

## 4. Check it: `orch addon pack`

```sh
orch addon pack ./changelog --git https://github.com/acme/orch-addon-changelog
```

validates the package at `HEAD` with the checks an install runs on the bytes (package rules, `orch.addon/3`, a manifest
that fits this core) and prints the commit, the digest and the **index entry** to paste:

```text
ok addon pack changelog 0.3.1
commit 9f2c1e7b...
package_sha256 sha256:4be1...9c07
index entry (paste into the "addons" list of your marketplace.json):
{
  "capabilities": [],
  "commit": "9f2c1e7b...",
  "id": "changelog",
  "min_orch": "2.0.0.dev0",
  "package_sha256": "sha256:4be1...9c07",
  "source": {
    "git": "https://github.com/acme/orch-addon-changelog"
  },
  "version": "0.3.1"
}
```

`--json` prints the same as data. **The digest is of the commit, not of your working tree.** It is computed by the
same plumbing-only export an install uses (no checkout: `autocrlf`, LFS smudge, `.gitattributes` and `export-ignore`
cannot change a byte), so it equals the digest an install of that commit computes. Hash the working tree by hand and you
meet `market.digest_mismatch`. `pack` refuses a repository whose own config could make git run a program, writes
nothing, and talks to no host.

`PATH` must be the root of the repository (default: the current directory) with its own `.git` directory (not a link,
a gitfile, a linked worktree, a partial clone or a repository with `objects/info/alternates`); a package inside a
repository is `--subdir`. Without `--git` the entry carries a placeholder URL that you must replace. Two limits to know: the whole
commit tree must fit the fetch cap (about 24 MB, no partial clone), and the entry's `publisher` block is not made
by `pack`: it signs for **one marketplace**, so the marketplace owner adds it (and `expires_at`, the index signature)
when the marketplace requires them.

## 5. Publish

Push the commit (a full commit id is what is pinned; a tag or branch is not) and add the entry to the `addons` list of a
marketplace's `marketplace.json` (format `orch.market/1`, entry fields in the marketplace spec section 3). The entry's
`source.git` host must be the marketplace's host or on the host allow list (`github.com`, `gitlab.com`). Raise
`version` for a new release: a version number is one set of bytes, and a lower one is refused as a rollback.

## 6. Install

The owner picks it in Browse or runs `orch addon install changelog`. The review shows the capabilities, the fields, the
digest and, labelled "per host, not signed", where it came from. Only the signed grant installs, and the signature
covers the digest of the package that was read.

## Try the whole way in a dev workspace: a local marketplace

Marketplaces other than the official one are refused in a real workspace. In a **dev sandbox workspace**, a dev host
can be told about one test marketplace, which gets the dev class: the official rules (a signed index with `expires_at`,
every entry signed by the test key) with that key. No address rule is relaxed: the test marketplace is a public repo on
an allowed host, and the add-on's source may live in the same repo (`source.subdir`).

1. **Commit and pack** the add-on (steps 3 and 4 above), with `--git` set to the test marketplace's URL if the add-on
   lives in that repo, and `--subdir` for its folder.
2. **Make the test marketplace**: a repo with `marketplace.json` listing the entry, signed with a throw-away ECDSA P-256
   key (the index, and each entry's `publisher`). The way to do it, in words: generate an ECDSA P-256 key pair and
   keep the private key outside every repository; write `marketplace.json` with `format`, `name`, `version`, `source`,
   `expires_at` and the entries; sign each entry with the key (the signature is bound to this marketplace's URL), then
   sign the index as a whole; commit and push. The orch-dev kit's generator
   [`e2e/lib/s13_fixture.py`](https://github.com/severinlindenmann/orch-dev-kit/blob/develop/e2e/lib/s13_fixture.py)
   does exactly this with orch's own signing code and is the worked example. Never commit a private key.
3. **Tell the dev host**: in the sandbox, `<root>/state/host/dev.json` gets `"market": {"url": "<repo URL>", "keys":
   ["<public key>"]}` (the dev kit writes it with `orch.devsandbox.write_dev_json`). A host without this entry, and
   every real workspace, answer `market.community_unisolated`.
4. **Add it**: `orch market add <repo URL> --commit <index commit>`; the index is fetched, verified and recorded
   (`orch market list --entries` shows `changelog` as `dev`).
5. **Install it**: `orch addon install changelog`. The review prints `changelog 0.3.1  package sha256:...`, no
   capabilities, and the market block. In the demo workspaces the scripted test human signs; in your own dev workspace you
   type the word and the passphrase.
6. **Check**: `orch addon list` shows it enabled, `orch addon update changelog --check` shows the origin, and the
   digest in the review equals the one `orch addon pack` printed.

The test `tests/host/test_market_authoring_guide.py` does steps 1 to 6 with the files of this folder.

## Updating

Change the code, commit, raise `version` in `orch-addon.json` (and the marketplace entry), run `orch addon pack`
again and publish the new entry. The owner's `orch addon update changelog` shows what changed (any widened permission is
shown first, in full) and always asks for a new signature.

## Errors you may meet

| Code | Meaning for an author |
|---|---|
| `market.package_invalid` | a file or name breaks the package rules (link, submodule, hidden or non-ASCII name, compiled file, too big or too many); the commit is judged whole before any file is read |
| `market.manifest_mismatch` | not `orch.addon/3`, no `min_core`, an unknown capability, a manifest that does not fit this core, or (in an install) the entry does not match the manifest |
| `market.digest_mismatch` | the entry's `package_sha256` is not the digest of the commit: copy it from `orch addon pack` of that commit |
| `market.reserved_id` | the name belongs to a built-in or official add-on |
| `host.dev_refused` | `addon dev` outside a dev sandbox workspace on a dev host |
| `market.actor_refused` | an agent ran a command that is a person's |
