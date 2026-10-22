# orch add-on marketplace: spec and contract (`orch.market/1`)

Status: G0 draft for the L2 gate (orch-core #847), 22 Oct 2026. Stream G (#917), task #918. Runs standalone (D79): nothing
here needs a hub.

- **Builds on, and does not weaken:** the add-on manifest (`orch.addon/3`, [addon-api.md](addon-api.md)), the package
  rules and package digest `package_sha256` (ticket-format §8.1, `orch.addons.package`), the owner-signed
  `addon.granted` event (ticket-format §5.4.2), the out-of-process runner that re-checks "digest equals grant" on every
  run, and the staged update and grant design of [addon-platform.md](../design/addon-platform.md) §4.
- **Decisions this implements** (owner, 22 Oct 2026): 1. GitHub and Landing leave the built-in set for the official
  marketplace. 2. A marketplace is a plain git repo with a `marketplace.json`; there is no hosted registry. 3.
  Dependencies are allowed from v1, as lockfiles with pinned hashes, inside the digest, installed at grant time. 4.
  Community marketplaces only after per-add-on OS-user isolation (#287). 5. With a hub, only hub-allowed marketplaces.
  6. Publisher signatures are optional, the key is remembered on first use.
- **Contract files:** schema `plugins/orch-core/src/orch/addons/schemas/market.v1.json`, vectors
  `plugins/orch-core/tests/addons/vectors_market.json`, reference checker `tests/addons/market_ref.py` (test code),
  tests `tests/addons/test_market_vectors.py`. Where the checker and this document disagree, this document wins.
- **Concept draft** (screens, not binding): [`docs/vision/marketplace/index.html`](../vision/marketplace/index.html).
- **Not in this task:** any product code. `orch market`, `orch addon install/update` (G2, G3), lockfile install (G4),
  author tools (G5), the move of GitHub and Landing (G6), core enforcement of the hub policy (G7). Words "MUST" and
  "MUST NOT" are requirements on those tasks.

## 1. The three shelves

| Shelf | What is on it | Where the bytes come from | Gate |
|---|---|---|---|
| **Built in** | Tech-agnostic add-ons only: dashboard, start agent, terminals, worktrees, wiki, widgets, schedules, explorer, usage, estimate, model routing, quick tasks, activity, publish | the orch-core wheel (`orch.addons.manifests`); no download | the existing owner-signed `addon.granted` |
| **Official marketplace** `orch-marketplace` | orch-maintained, tool-specific add-ons: GitHub, Landing, later Jira, Slack, … | a git repo, preinstalled as a known marketplace | the marketplace gate (§5) |
| **Other marketplaces** | Any git repo with a `marketplace.json`, or a single add-on repo (a direct source) | a git repo the owner added | the marketplace gate (§5), refused until #287 (§10) |

Rule of thumb: an add-on that needs only orch itself (tickets, agents, the workspace folder) is built in. One that talks
to a specific outside product, or encodes one team's workflow, comes from a marketplace.

A marketplace **never runs anything and is never trusted for what it names**. It tells orch where a package is and which
bytes to expect. What runs is decided by the owner's signature over the digest of the bytes orch read itself.

**Reserved ids.** An id is reserved when it is a built-in id or an official id. No non-official marketplace may publish
one (`market.reserved_id`). The reserved set is **shipped in the core release** (built-in ids plus the official ids at
release time) and only *added to* by a verified official index (§3.1). So the rule holds offline, on a first run, and
whatever the official repo or an unverified copy of its index says: with no usable official index the shipped set is
used, never an empty one (fail closed). The other direction holds too: an official entry may carry a built-in id only
if core lists that id as *moved to the marketplace* (G6 moves `github` and `landing`); otherwise it is
`market.reserved_id`, so the official shelf can never install a second package under the name of a built-in.

## 2. Vocabulary

| Term | Meaning |
|---|---|
| marketplace | a git repo with `marketplace.json` (§3) at its root |
| index | the parsed `marketplace.json` |
| entry | one add-on in `addons` |
| official | the marketplace whose source URL equals the URL pinned in the core release |
| origin | the marketplace (normalized source URL) an installed add-on came from, kept in the local install record |
| direct source | one add-on repo added without an index. Treated as a one-entry marketplace of the "other" class; the owner supplies commit and digest, so there is no index to trust |
| install record | host-private, unsigned state (`<state>/market/`): added marketplaces, last index version seen, known publisher keys, origin per installed add-on. It is shown in reviews as "per host, not signed" |
| staging | a fresh, private directory outside the workspace, owned by the host user, where fetched bytes live until the grant |
| the human | a person acting with their own device key or in their own terminal. Never an agent, never a mandate |

## 3. `marketplace.json`, format `orch.market/1`

Strict JSON (the `orch.canon` strict parser: duplicate keys, floats, NaN, non-ASCII keys, nesting over 16 are refused).
**Unknown keys are refused at every level** (`market.malformed`): a new field is a new format version. The schema file is
authoritative for types and patterns; this table says what the fields mean.

Top level (all required except `title`):

| Field | Type | Meaning |
|---|---|---|
| `format` | `"orch.market/1"` | exactly this string |
| `name` | add-on-name pattern `[a-z][a-z0-9-]{0,39}` | a label for people. **Never an identity**: identity is `source.git` (§4) |
| `title` | one line, at most 140 chars | display only |
| `version` | integer ≥ 1 | the index version, raised at every change. Lower than the last one seen = rollback (`market.rollback`) |
| `source.git` | https URL | where this index lives. MUST equal the URL the owner added it from (`market.source_mismatch`) |
| `addons` | array of entries, at most 500 | |

Entry (required unless noted):

| Field | Meaning |
|---|---|
| `id` | the add-on name. Unique in the index (`market.duplicate_id`). Must equal the manifest `name` |
| `version` | `major.minor.patch`. Must equal the manifest `version` |
| `summary` (optional) | one line, display only, labelled "from the marketplace, not part of what you sign" |
| `source.git` | https URL of the add-on repo. Its host MUST be the host of the marketplace itself or on the host allow list (§5.1); the schema already refuses IP literals, `localhost`, ports and credentials |
| `source.subdir` (optional) | relative path to the package directory; every segment follows the §8.1 file-name rule, so no `..`, no leading dot |
| `commit` | a **full** git object id (40 or 64 lower-case hex). A branch, a tag or an abbreviation is `market.unpinned` |
| `package_sha256` | the §8.1 package digest (`sha256:` + 64 lower-case hex) of the directory `source.subdir` at `commit`. A malformed value is `market.bad_digest_format` |
| `capabilities` | exactly the manifest's capabilities, sorted, from the closed list (`git_push`, `network`, `pty`, `serve_http`, `spawn_agent`) |
| `min_orch` | lowest orch core version that runs it, grammar of `min_core` in addon-api §2. Must equal the manifest's `min_core` |
| `api` (optional) | display copy of the manifest's api range. The manifest decides |
| `runtime` (optional) | `{kind, lock}`: the lockfile kind (`python-requirements` or `node-package-lock`) and its path inside the package (§8) |
| `publisher` (optional) | `{key, sig}`: an ECDSA P-256 public key (65-byte uncompressed point) and a signature, canonical unpadded base64url. The signature is bound to this marketplace's URL (§4.3) |

A marketplace add-on MUST ship `orch.addon/3` (it needs `min_core`). One entry per id: the index states the current
pinned release, history is the git history of the index. There is no `latest`, no range, no floating reference anywhere
in the format.

Example (the digest is shortened):

```json
{
  "format": "orch.market/1",
  "name": "orch-marketplace",
  "version": 12,
  "source": {"git": "https://github.com/severinlindenmann/orch-marketplace"},
  "addons": [{
    "id": "github", "version": "1.2.0", "summary": "Import GitHub issues as tickets",
    "source": {"git": "https://github.com/severinlindenmann/orch-addon-github", "subdir": "pkg"},
    "commit": "0123456789abcdef0123456789abcdef01234567",
    "package_sha256": "sha256:9f2c…",
    "capabilities": ["git_push", "network"],
    "min_orch": "2.0.0",
    "publisher": {"key": "BI_UM…", "sig": "Ikg…"}
  }]
}
```

## 4. Identity, trust classes and the publisher key

### 4.1 Identity and the canonical URL

A marketplace is identified by its **canonical source URL**, never by its `name`. Normal form: `https`, lower-case ASCII
host (an internationalised name is its `xn--` form, and the review shows only that ASCII form, never a Unicode
rendering), no port, credentials, query or fragment, no trailing slash, **no trailing `.git`**, and a **lower-case path
on hosts that ignore case** (`github.com`, `gitlab.com`, `bitbucket.org`, where `ACME/m` and `acme/m` are one
repository). The same form applies to every add-on `source.git`.

One function, `canonical_market_url()`, decides this, and it is the only place that does. It is used wherever the URL
is a key or an input: `market add`, comparing the index's `source.git` with the added URL, the publisher signature
(§4.3), the TOFU store, the rollback floors, the origin record, hub allow and block lists, and the install record.
**It rejects; it does not repair.** `market add acme/m.git` is refused (`market.malformed`) and the person is shown the
canonical form to use, because a silent normalisation would let two spellings reach different stored keys. The schema
refuses a non-canonical `source.git` in an index, and the installer refuses a non-canonical URL handed to it. So one
repository can never hold two TOFU slots, two rollback floors, two origins, or slip past an exact-match block entry.

### 4.2 Classes

| Class | Test | Extra rule |
|---|---|---|
| official | URL equals the pinned official URL | every entry MUST carry a `publisher` whose key equals the official key pinned in the core release (`market.official_unsigned`), and verify |
| other | any other marketplace or direct source | refused unless OS-user isolation is in place (§10); a `publisher` is optional |

The pinned official URL and key are constants of the core release, not read from disk or from any index. Changing them
is a core release.

### 4.3 Publisher signature

`sig` is ECDSA P-256 / SHA-256, raw 64-byte `r || s` with `1 ≤ r, s ≤ n−1` (suite 2 of the relay protocol), over

```
"orch/market/1/entry|" || cj({"entry": <entry with "publisher" reduced to {"key": <key>}>,
                              "market": <the marketplace's normalized source URL>})
```

where `cj` is the canonical JSON of `orch.canon.dumps`. It covers every field of the entry, including `version`,
`commit`, `package_sha256`, `capabilities` and `source`, **and the marketplace the entry is published in**. So a signed
entry copied into another marketplace (a mirror, or a hostile index that wants to borrow a good publisher's key) does
not verify there (`market.bad_signature`); a publisher who lists an add-on in several marketplaces signs once for each.
The signed `version` is what makes rollback detectable (§6, I4): an old signed entry stays validly signed forever, so a
verifier MUST also refuse an entry whose `version` is lower than the highest one it has installed or seen for this
`(URL, id)` (`market.rollback`). A key that is not a valid point or a signature that does not verify is
`market.bad_signature`.

### 4.4 Trust on first use (TOFU)

The store maps `(marketplace URL, add-on id)` to a publisher key.

| Situation | Result |
|---|---|
| no key stored, entry has none | install proceeds, review says "unsigned" |
| no key stored, entry has one | install proceeds; the review shows the key's fingerprint (`SHA256:` of the 65 bytes, as `orch.land.key.fingerprint` does) and "first use: remembered when you sign". The key is stored only after the grant is signed |
| key stored, entry has the same key | proceeds, review says "same publisher as before" |
| key stored, entry has another key **or none** | `market.publisher_key_changed`. Only `orch market trust --replace <url> <id>` (human-only, shows both fingerprints, typed confirmation) changes it. Removing a key is a change, so a signature cannot be silently dropped |

A key proves continuity ("the same publisher as last time"), not identity. The review says so in those words.

## 5. Install flow

Human-only (§11). The order below is the order of refusal: the first failing check is the answer. Steps 1 to 10 touch no
network and write nothing; the first network access is step 11.

| # | Step | Refusal |
|---|---|---|
| 0 | The caller is the human: no `ORCH_GRANT`/`ORCH_SESSION`, a terminal or a device key, not a mandate | `market.actor_refused` |
| 1 | Read the cached index with the strict parser. The pin and digest fields are checked first (a missing or non-hex `commit`, a bad `package_sha256`), then the schema, then sorted capabilities and unique ids | `market.unpinned`, `market.bad_digest_format`, `market.malformed`, `market.duplicate_id` |
| 2 | `source.git` of the index equals the URL it was added from | `market.source_mismatch` |
| 3 | `version` is not lower than the last version seen for this URL | `market.rollback` |
| 4 | Class (§4.2): an "other" marketplace needs OS-user isolation (§10) | `market.community_unisolated` |
| 5 | Hub policy, when a hub is joined (§9): block list, then allow list | `market.blocked`, `market.not_allowed` |
| 6 | Select the entry by `id`; its git host is the marketplace's host or on the allow list (§5.1); reserved ids; an installed add-on of this id came from this URL; the entry's `version` is not lower than the installed one or the highest seen for this `(URL, id)` | `market.unknown_addon`, `market.host_not_allowed`, `market.reserved_id`, `market.origin_changed`, `market.rollback` |
| 7 | Publisher: official key rule, signature, TOFU (§4) | `market.official_unsigned`, `market.bad_signature`, `market.publisher_key_changed` |
| 8 | Hub capability ceiling (§9): entry capabilities ⊆ ceiling | `market.capability_ceiling` |
| 9 | `min_orch` ≤ the running core version (a `.devN` build sorts before its release; an unparsable version fails) | `market.min_orch` |
| 10 | (the checks above need no package) | |
| 11 | **Fetch** into a fresh staging directory (§5.1): every connection address is checked, then the fetched object id must equal `commit` | `market.address_refused`, `market.fetch_failed`, `market.commit_mismatch` |
| 12 | **Read once**: `read_package` on the staged subdirectory (all §8.1 rules). From here every decision uses those bytes, never the directory | `market.package_invalid` |
| 13 | The package digest equals `package_sha256` of the entry | `market.digest_mismatch` |
| 14 | The manifest agrees with the entry: schema `orch.addon/3`, `name` = `id`, `version`, sorted `capabilities`, `min_core` = `min_orch` | `market.manifest_mismatch` |
| 15 | If `runtime` is set: the lockfile exists in the package and passes the lockfile rules (§8) | `market.lock_missing`, `market.lock_invalid` |
| 16 | Compatibility with this core (`incompatibility()` of addon-api §3) | existing `addon.incompatible` |
| 17 | **Review**, then the human signs `addon.granted` for exactly `grant_event(manifest, package)` of the staged bytes | the owner declines |
| 18 | Under the store lock, the host re-reads the staged package, checks its digest equals the digest in the signed grant, appends `addon.granted`, swaps the directory by rename, records origin and (now) the TOFU key | `market.staged_changed` |
| 19 | If `runtime` is set: install the dependencies (§8.2) into the add-on's own environment. The add-on stays unavailable until this has succeeded | `market.env_failed` |

Steps 12 to 15 are repeated by the host at step 18 on the staged bytes; the checks of 13 and 18 are the safety, the
others are early, readable errors.

### 5.1 Fetching (what MUST hold)

**Where the installer may connect.** An index is attacker-controlled text, and `source.git` makes the host connect to the
URL in it, so the target is restricted three ways:

1. *Shape* (schema): https only, a dotted DNS name ending in letters, so no IP literal in any notation (dotted, decimal,
   hex, IPv6) and no `localhost`; no port (443 only); no credentials, query or fragment.
2. *Host allow list* (`market.host_not_allowed`, step 6): the entry's host MUST equal the host of the marketplace the
   entry came from, or be on the allow list. The allow list is a constant of the core release (`github.com`,
   `gitlab.com`) plus hosts the owner adds with `orch market allow-host` (human-only, §11). **The index can never extend
   it**: a list inside the index would be the attacker's. A company that hosts its marketplace and its add-ons on one git
   server needs no configuration.
3. *Address* (`market.address_refused`, step 11): the installer resolves the name itself, checks every resulting address
   and connects only to a checked address (§5.1.1, no second resolution between check and connect). IPv4 must be global
   unicast: not loopback, link-local (including the cloud metadata address 169.254.169.254), private (10/8, 172.16/12,
   192.168/16), carrier-grade NAT (100.64/10), unspecified, multicast or reserved. **IPv6 is an allow list**: only
   `2000::/3`, minus Teredo and the rest of `2001::/23`, documentation (`2001:db8::/32`, `3fff::/20`) and 6to4
   (`2002::/16`). That refuses, without decoding them, every form that embeds or translates IPv4: NAT64
   (`64:ff9b::/96`, `64:ff9b:1::/48`, which a DNS64 resolver synthesises from any A record), IPv4-compatible (`::/96`),
   SIIT (`::ffff:0:0/96`) and site-local (`fec0::/10`). IPv4-mapped (`::ffff:a.b.c.d`) is decoded and judged as the IPv4
   address it carries. Anything that is not an address is refused. HTTP redirects are refused; the same checks apply to
   the marketplace's own URL when an index is added or refreshed, and to every dependency download (§8.2).

**How git is run.**

- `git` is run with a private empty config (`GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM` and `GIT_CONFIG_NOSYSTEM` set so
  the person's config, hooks, credential helpers and `url.<x>.insteadOf` rewrites do not apply), no terminal prompt,
  protocols limited to https, no submodule recursion, no LFS, and a size and time cap.
- Fetch the commit by its full id (`git fetch --depth 1 <url> <commit>`). Then `git rev-parse FETCH_HEAD` MUST equal
  `commit`.
- Build the package directory with plumbing only: `git ls-tree -r -z <commit>:<subdir>` and `git cat-file blob`, writing
  regular files with the modes 0644 or 0755. No checkout, no archive, so no `.gitattributes` filter, `export-subst`,
  `export-ignore`, line-ending conversion or hook can change a byte. Entries of mode `120000` (symbolic link) or
  `160000` (submodule) are `market.package_invalid`. No `.git` ever exists inside the package.
- The result is a plain directory that `read_package` reads with `O_NOFOLLOW` once; after that it is only compared.
  A SHA-1 object id is a pin for fetching, not a security claim: **the digest is** (§12, I1).

### 5.2 The review

The review screen is the existing grant review (`install.review_lines`) plus a marketplace block. Everything from the
package is escaped data; everything from the index is labelled.

| Line | Source | Signed? |
|---|---|---|
| name, version, command, capabilities, fields, sections, agents' hint, package digest | the staged package (existing `review_lines`) | yes: they are `grant_event` |
| marketplace (URL), class, `commit`, publisher key fingerprint and TOFU status, `summary` | the index and the install record | **no: "per host, not signed"** |
| dependency lockfile kind, number of locked packages, "installs these packages after you sign" | the package | yes (inside the digest) |
| hub policy result | the hub card | no |

The signed body is only `grant_event(manifest, package)`. The marketplace adds nothing to what is signed; a later
`addon.granted` from another source with the same digest is the same grant.

## 6. Update flow

Updates are never automatic. `orch market refresh` (read-only, allowed to agents) fetches indexes; it installs nothing.
`orch addon update <name>` (human-only) does the following.

1. Refresh the index of the add-on's **origin** (an update never moves to another marketplace: `market.origin_changed`).
2. Run §5 steps 0 to 16 for the new entry. Additionally refuse a lower `version` than the installed one
   (`market.rollback`; an explicit `--allow-downgrade` is a separate human-only confirmation, never the default).
3. Stage the new package as in addon-platform §4: the old granted version keeps running, with exactly its granted
   capabilities, until the new grant is signed. (A paused state exists only for `incompatible` and `revoked` as there.)
4. Show a **diff** against the current grant:

   | Line | Shown as |
   |---|---|
   | version | `1.1.0 -> 1.2.0` |
   | capabilities | `+ network` as **new**, `- pty` as removed |
   | fields, sections, needs, cli, artifact kinds | the existing `_changes` lines |
   | package digest | old and new |
   | commit | old and new; **"not a fast-forward of the old commit"** when the old commit is not an ancestor (a rewritten history is shown, not blocked: the digest decides) |
   | publisher | same key / **new key** / **key removed** (the last two refuse, §4.4) |
   | dependencies | lockfile added/removed, packages added/removed/changed with versions |

5. A new grant is **always** needed: the digest changed, so the old grant no longer covers the bytes. There is no
   "update that keeps the grant".
6. Steps 17 to 19 of §5. On failure at any step the old version stays installed and active.

**Several workspaces:** one grant per workspace, each its own review, as in addon-platform §4.

## 7. Remove flow

Human-only. `orch addon remove <name>`:

1. `addon.disable` (existing): the add-on stops, its data stays.
2. The host deletes the add-on's package directory and its dependency environment (§8.2).
3. The install record drops the origin. **The TOFU key stays** (a later reinstall from the same URL still has to match).
4. Data is deleted only by a separate `addon.purge`, with its own confirmation, as today.

Removal does not need the marketplace to be reachable, and a marketplace that disappears or removes the entry does not
remove or revoke anything installed. Yank and advisory handling is the backlog item #932.

## 8. Dependencies

### 8.1 What a package may declare

A package that needs libraries or another runtime declares one lockfile in the index (`runtime.kind`, `runtime.lock`)
and ships it in the package. **The lockfile is a file of the package, so it is inside `package_sha256`** (§8.1 digest
covers every file). Changing one hash in it changes the digest and needs a new grant. The manifest itself does not
change in G0; G4 adds the declaration that the manifest and the index agree on.

| `runtime.kind` | File | Rules (all `market.lock_invalid`) |
|---|---|---|
| `python-requirements` | pip `requirements.txt` in hash mode (`uv export --format requirements-txt` produces it) | every logical line is a requirement followed by one or more `--hash=sha256:<64 hex>` and nothing else. The requirement part MUST parse as a real PEP 508 requirement (not a regular expression over text, so an option cannot hide in the marker): one name, exactly one specifier `==` with a full version (no `*`), an optional marker, no URL. Option lines (`--index-url`, `--extra-index-url`, `--find-links`, `-e`, `-r`) and VCS or URL requirements are therefore refused |
| `node-package-lock` | `package-lock.json`, `lockfileVersion` the integer 3 (not `3.0`) | strict parse. Every package key is `node_modules/<name>` repeated for nesting (names by the npm grammar, so no `..`, no empty or dot segment, nothing outside `node_modules/`), has a `version`, is not a `link`, and its `resolved` is **exactly** `https://registry.npmjs.org/<name>/-/<basename>-<version>.tgz` for the key's name and the entry's version (registry host pinned in core per ecosystem). `integrity` is exactly one `sha512-` plus the base64 of a 64-byte digest, nothing more. The root entry (`""`, the add-on itself) MUST NOT name a `resolved`, `integrity`, `link` or `inBundle`, and every dependency it declares MUST be a locked entry. The names and versions the review shows are derived from these checked keys only |

**Scripts are not judged by the lockfile.** Flags such as `hasInstallScript` are the publisher's claim, so a lock is neither accepted nor refused for them. The rule is that no dependency script ever runs (§8.2).

More kinds (other runtimes) are an additive change to the schema's enum with their own rules and vectors.

### 8.2 Installing

Dependencies are installed **after the grant is signed and never before** (§5, step 19), by the host and not by the
add-on, into an environment owned by that add-on alone:

- Location `<state>/addon-env/<workspace id>/<name>/<digest hex prefix>/`, outside the workspace and the package. Not
  shared between add-ons, workspaces, or versions.
- The installer is called so that it **cannot resolve anything and cannot run dependency code**. Python: `pip install
  --require-hashes --no-deps --only-binary=:all:`, with the index fixed by the installer (`https://pypi.org/simple`),
  never by the lockfile. Node: `npm ci --ignore-scripts` with the registry fixed by the installer. Another package
  manager MUST use its equivalents: exact versions from the lock, hash verification, no source builds, no lifecycle
  or install scripts, no resolution. Every downloaded artifact is hashed and compared with the lockfile before it is
  unpacked, and every connection obeys the address checks of §5.1. A dependency whose only form is a source
  distribution cannot be installed, which is a refusal (`market.env_failed`), not a fallback.
- The environment gets a stamp file `{digest, lock hash, installer, time}`. The runner refuses to start an add-on whose
  environment has no stamp or a stamp for another digest (`addon.incompatible`-style state "environment missing"), so a
  half-built or replaced environment never runs.
- A failure leaves no environment and no running add-on (`market.env_failed`); the grant stays signed, and the owner can
  retry or remove.
- At run time the add-on's capabilities are unchanged by having dependencies. `network` stays a capability; installing
  dependencies is the host's act and is not available to the add-on.

## 9. Hub policy hook (interface only)

With a hub joined, the workspace host MUST ask the hub's policy before any marketplace use. The hub side is D14 (#927),
the enforcement in core is G7 (#928); this section fixes only what the policy looks like so the vectors and the order of
checks are stable.

```
{ "allow":   [<marketplace URL>, …],     // normalized source URLs
  "block":   [<marketplace URL>, …],
  "ceiling": [<capability>, …] | null }   // null = no ceiling
```

| Rule | |
|---|---|
| Without a hub the policy is `none` and the owner decides alone (D79). Standalone must not call a hub | |
| With a hub, a marketplace that is not in `allow` is refused, **including the official one** | `market.not_allowed` |
| `block` beats `allow` | `market.blocked` |
| A policy that cannot be read, is expired or is malformed is **an empty allow list** (fail closed) | `market.not_allowed` |
| `ceiling` is checked on the index entry (step 8) **and again on the staged manifest and on the grant event** at step 17/18. The index is only a claim | `market.capability_ceiling` |
| A hub never installs or grants for a workspace; it can only refuse | |
| Policy does not override the human gate: even an allowed marketplace needs the signed grant | |

Whether the ceiling also applies to built-in add-ons and to grants that already exist is a D14/G7 decision (§15).

## 10. Community marketplaces

Until per-add-on OS-user isolation (#287) exists, any marketplace other than the official one, and any direct source,
is refused (`market.community_unisolated`) and `orch market add` for such a URL is refused the same way. The reason: a
granted add-on today runs as the person, and a third-party add-on with `network` or `spawn_agent` would run its
publisher's code with the person's files and keys within reach. A review cannot make that safe; isolation can.

Isolation is a property core detects (a configured, working per-add-on user), not a setting the owner or an index can
claim. When #287 lands, G-follow-up #929 flips this check; the format and the flow do not change. The local folder used
by `orch addon dev` is not a marketplace source: it works only in workspaces marked `dev`.

## 11. Who may do what

| Action | Human | Agent (`ORCH_GRANT`/`ORCH_SESSION`) | Mandate | Hub |
|---|---|---|---|---|
| `market list`, `market search`, `market refresh`, `addon update --check` | yes | yes (read-only, no install, no writes except the index cache) | no | n/a |
| `market add`, `market remove`, `market trust` | yes | **refused** | **refused** | refuses, never adds |
| `addon install`, `addon update`, `addon remove`, `addon.grant` | yes, with the signature | **refused** | **refused** | refuses, never installs |

Enforcement is by custody, not label: install, update and grant end in a signature with the owner's device key that an
agent cannot reach, and the terminal verbs refuse when the agent environment is set, as `orch dashboard` already does
(`human_only`). `market.actor_refused` is the extra, early refusal so nothing is fetched on an agent's behalf.

## 12. Threat model and invariants

The invariants are numbered; the tests and the review point at them.

| Invariant | Statement |
|---|---|
| **I1 Digest binding** | What runs is exactly the bytes whose §8.1 digest equals both the index entry and the digest inside the owner-signed `addon.granted`; the runner re-checks it on every run. No commit, tag, signature, index version or marketplace name is a substitute for it |
| **I2 Human gate by custody** | Adding a marketplace, installing, updating, removing and granting need a human; an agent or mandate is refused before any network access, and cannot sign |
| **I3 Read once, in staging** | Fetched bytes live in a private staging directory, are read once into memory with `read_package`, and every later step (review, grant, swap) uses those bytes or re-compares their digest under the store lock |
| **I4 The pin does not move, and signatures name their place** | An entry names a full object id and a digest; nothing floats. A different release is a different index state, a new review and a new grant. Index and add-on versions cannot go backwards (a validly signed old entry is still refused), an add-on cannot change marketplace, and a publisher signature is valid only in the marketplace URL it signed |
| **I5 Dependencies are inside the digest and inert until granted** | Lockfiles are package files; they are hash-pinned, resolved by nothing, and parsed by a real grammar, not by pattern. They are installed only after the signed grant, with dependency scripts disabled by the installer (never by trusting lock metadata), from allow-listed hosts, into an environment of the add-on's own that the runner checks against the digest |
| **I6 Policy only restricts, and fails closed** | Hub policy, the community gate and the ceiling can only refuse; unreadable or missing policy under a hub means nothing is allowed; block beats allow; marketplaces are identified by URL, never by self-declared name; the ceiling is checked on the index, the manifest and the grant |
| **I7 The installer connects only to public, expected places** | Fetch and download targets are https, standard port, a host that is the marketplace's own or on a list the index cannot extend, and a public unicast address checked after name resolution; redirects are refused |

| Threat | What happens | Answered by |
|---|---|---|
| **Malicious or compromised index** (lies about digest, capabilities, commit) | The index can only point at bytes. Digest, capabilities and manifest facts are re-derived from the package that was read (steps 13, 14), and the owner signs what the review derives from the package, not what the index says | I1, I3 |
| **Compromised publisher** (pushes a bad release, or signs one) | It reaches a staged update that needs a new review and signature, showing the capability and dependency diff and "new key" if the key changed. The old version keeps running and nothing is widened without the signature | I1, I4, §6 |
| **Signature replay** (a good publisher's signed entry copied into a hostile or mirror marketplace) | The signature covers the marketplace URL, so it fails there (`bad_signature`) | I4 |
| **Stolen or replaced publisher key** | `market.publisher_key_changed`; replacing needs `trust --replace` by a human with both fingerprints. A key is continuity, not identity, and the review says so | §4.4, I2 |
| **Git history rewrite, force-pushed tags, moved branches** | Fetch is by full object id and the digest is the binding. A rewritten history cannot change what is accepted. A removed commit means `market.fetch_failed`, never a silent fallback to a branch head. The diff flags "not a fast-forward" | I1, I4 |
| **Index and entry rollback** (an old index, or an old validly signed entry, replayed to bring back a vulnerable release) | An index `version` lower than the last seen is refused; an entry `version` lower than the installed one or the highest seen for the `(URL, id)` is refused even when its signature is valid | I4 |
| **SSRF through the index** (`source.git` pointing at `127.0.0.1`, the cloud metadata address, an internal host, another port, or a public host that redirects inward) | Schema shape (no IP literal, no port, no `localhost`), the host rule, address checks after resolution with no second lookup, no redirects; the allow list cannot be extended by the index | I7 |
| **TOCTOU between fetch and grant** (directory changed, symlink swapped, tag moved after the review) | Plumbing-only export into private staging; `read_package` with `O_NOFOLLOW`; the grant is for the digest of the staged bytes and the host re-compares inside the lock before the swap; a change gives `market.staged_changed` | I3, I1 |
| **Hostile git content** (filters, hooks, `.gitattributes`, symlinks, submodules, LFS pointers, `.git` inside the tree) | Private git config, no checkout, blobs written as plain files, link and submodule modes refused, `.`-leading names refused by §8.1 | I3 |
| **Dependency confusion** (a private name resolved from a public index; a lookalike package) | Nothing is resolved at install: the lockfile names exact versions, URLs and hashes, and option lines that add indexes are refused. The same add-on id from another marketplace is refused (`origin_changed`); other marketplaces cannot claim reserved ids | I5, I4 |
| **Lockfile tampering** (changed after review, or in transit) | The lockfile is inside the digest, so a change is a new digest and a new grant; each downloaded artifact is hash-checked before it is unpacked; the environment is stamped with the digest | I5, I1 |
| **Malicious dependency code at install time** (an install script, a build backend, a metadata flag claiming there is none) | Binary-only, hash-checked installs with scripts disabled by the installer; lock metadata is never trusted for safety, so nothing from a dependency runs before the add-on does, and the add-on runs under its granted capabilities only | I5 |
| **Option smuggling in a requirements line** (`; --index-url …` inside a marker) | The requirement part must parse as PEP 508; anything else is `lock_invalid` | I5 |
| **A lock pointing at another registry or a look-alike host** | `resolved` must be on the ecosystem's pinned host list, compared as a whole host name | I5, I7 |
| **Malicious text in an index** (summary, names, control characters) | One-line, length-capped, control characters refused by the schema; shown labelled "from the marketplace, not part of what you sign" and escaped like package text | I1 |
| **Typosquatting and name squatting** | The review shows id, title, source URL and capabilities. Reserved ids protect the built-in and official names. Beyond that, the owner chooses the marketplace | I2, §1 |
| **A marketplace owner who becomes hostile** | Same as a compromised publisher. A removed entry does not remove an installed add-on; revocation is #932 | I4 |
| **Agent tries to install, add or trust** | `market.actor_refused`; no key for the signature | I2 |
| **Hub misconfiguration or compromise** | A hub can only refuse (it holds no key that grants), and a broken policy refuses everything | I6, I2 |
| **Add-on needs more than the ceiling after update** | Checked on index, manifest and event; refused | I6 |
| **Third-party code running as the person** | Not fixable by review. Third-party marketplaces are refused until #287 | §10 |

## 13. Refusal codes

All refusals are fail-closed, carry the code and a short fixed text, and never echo index or package content. The codes
are new in the v1 catalog; G2/G3 declare them per op and regenerate the CLI goldens.

| Code | Meaning | Step |
|---|---|---|
| `market.malformed` | not strict JSON, unknown field, wrong type or pattern, unsorted capabilities | 1 |
| `market.unpinned` | `commit` missing or not a full lower-case object id | 1 |
| `market.bad_digest_format` | `package_sha256` missing or not `sha256:` + 64 lower-case hex | 1 |
| `market.duplicate_id` | two entries with the same `id` | 1 |
| `market.source_mismatch` | the index's `source.git` is not the URL it was added from | 2 |
| `market.rollback` | index version lower than the last seen, or entry version lower than the installed or highest seen one | 3, 6 |
| `market.community_unisolated` | a non-official marketplace or direct source without OS-user isolation | 4 |
| `market.blocked` | the hub's block list names this marketplace | 5 |
| `market.not_allowed` | with a hub, the marketplace is not on the allow list (or the policy is unreadable) | 5 |
| `market.unknown_addon` | the id is not in the index | 6 |
| `market.host_not_allowed` | the entry's git host is neither the marketplace's host nor on the host allow list | 6 |
| `market.reserved_id` | a non-official marketplace claims a built-in id | 6 |
| `market.origin_changed` | the installed add-on came from a different marketplace | 6 |
| `market.official_unsigned` | an official entry without a valid publisher entry for the pinned key | 7 |
| `market.bad_signature` | the publisher signature does not verify | 7 |
| `market.publisher_key_changed` | the key differs from, or is missing against, the remembered one | 7 |
| `market.capability_ceiling` | the add-on asks for a capability outside the hub's ceiling | 8 |
| `market.min_orch` | this core is older than the entry's `min_orch` | 9 |
| `market.address_refused` | a connection address is loopback, private, link-local, multicast, carrier-grade NAT or not an address | 11 |
| `market.fetch_failed` | the repo, the commit or the subdirectory cannot be fetched within the caps | 11 |
| `market.commit_mismatch` | the fetched object id is not the pinned `commit` | 11 |
| `market.package_invalid` | the package breaks a §8.1 rule, or contains a link or submodule | 12 |
| `market.digest_mismatch` | the package digest is not `package_sha256` | 13 |
| `market.manifest_mismatch` | manifest and entry disagree on schema, name, version, capabilities or `min_core` | 14 |
| `market.lock_missing` | `runtime.lock` is not in the package | 15 |
| `market.lock_invalid` | the lockfile breaks a rule of §8.1 | 15 |
| `market.staged_changed` | the staged package no longer has the signed digest at grant time | 18 |
| `market.env_failed` | the dependency environment could not be built | 19 |
| `market.actor_refused` | an agent or mandate asked for a human-only action | 0 |

Rows without a vector in `vectors_market.json` (`fetch_failed`, `commit_mismatch`, `staged_changed`, `env_failed`) need
git, a network or a running host; G2 to G4 test them with a local git repo and a fake runner.

## 14. Files and checks in this task

| File | What |
|---|---|
| `src/orch/addons/schemas/market.v1.json` | strict JSON Schema 2020-12 for the index |
| `tests/addons/vectors_market.json` | 114 cases: `index` (schema and index rules), `install` (steps 0 to 15 with a context), `lock`, `address` |
| `tests/addons/market_ref.py` | the reference checker and the entry signing helpers (test code) |
| `tests/addons/make_vectors_market.py` | builds the vectors; run again to change them. Signatures are random, so the file is the artefact |
| `tests/addons/test_market_vectors.py` | runs every vector, checks the schema, that every code in §13 has a vector or is listed as untestable here, and that signatures verify only for the signed bytes |

A vector's `install` context is `{actor, core_version, market_repo, isolation, official{repo,key}, hub|null,
builtin[], git_hosts[], installed{id->{origin,version}}, known_keys{"<url>|<id>"->key}, seen{"<url>|<id>"->version}, last_version}`. A product implementation is
conformant when it gives the same `ok` or `refuse` for every case.

## 15. Open points (need an owner decision)

1. **Should `addon.granted` carry provenance** (`source`: marketplace URL and commit)? This spec says no: the digest is
   the binding, provenance stays in the unsigned install record and is shown as "per host, not signed". Putting it in
   the event makes replay and audit know the origin, at the cost of an event schema change in a Tier S area.
2. **Official marketplace key custody.** The pinned official key signs every official entry. Same root and a separate
   purpose, as the add-on catalog key in addon-platform §3, is the obvious choice; the ceremony and rotation are the
   owner's. Until then official entries cannot be signed and the official shelf cannot ship.
3. **Relation to the signed catalog** (addon-platform §3, `orch.addon-catalog/1`, tar archives). This spec makes git
   plus `marketplace.json` the way to ship non-built-in add-ons and keeps the staged update and grant machinery of
   addon-platform §4 unchanged. Does the catalog remain only for the add-ons built into the wheel, or is it retired once
   G6 lands? The staged grant (`addon.stage`, `addon.grant staged`) is designed there; G3 must find it built or build
   it.
4. **Ceiling scope** (D14/G7): does the hub ceiling also bind built-in add-ons and already granted ones (an already
   granted add-on over the ceiling would have to pause)? This document only fixes the check for marketplace installs.
5. **Direct sources.** A single add-on repo has no index. This spec treats it as a one-entry "other" marketplace that the
   owner fills in (commit and digest on the command line). Allowed at all before #287? As written: no.
6. **Dependency kinds beyond Python and Node**, and whether wheels with compiled code are acceptable (`--only-binary`
   allows them; §8.1 refuses `.so` inside a package, not inside an environment). A stricter v1 would be pure-Python
   wheels only.
7. **Index signature.** Only entries are signed. A signed index would also protect the set of entries and the version
   counter against a hostile mirror; not needed while the official URL is one pinned git host over https.
8. **Yank and advisories** (#932) and **add-on secrets** (#930): no field is reserved for them. Both will need an
   `orch.market/2` or an advisory file next to the index.
9. **Index freshness.** `version` stops rollbacks within one host, but a host that was offline never sees a newer index.
   Is a maximum age ("refresh older than N days is shown as stale") wanted, and does a stale index block installs?
10. **TOFU scope.** The key is stored per (marketplace URL, id). The same publisher key under another marketplace is
    "new" there. A per-key (not per-URL) store would carry trust across marketplaces and mirrors; it is weaker against
    a marketplace swap. This spec chose the stricter one.
11. **Parsers in the product.** The reference uses `packaging` for PEP 508. It is not a runtime dependency of core today; G4 either adds it or writes a strict marker grammar with the same vectors. Likewise the registry host lists per ecosystem and the default git host allow list (`github.com`, `gitlab.com`) are owner choices.
