# orch add-on marketplace: spec and contract (`orch.market/1`)

Status: G0, 22 Oct 2026; revised after the L2 security pass (findings L2M-1 to L2M-10 folded in, answers to the open points marked provisional in §15.2). Stream G (#917), task #918. Runs standalone (D79): nothing
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
| `expires_at` | integer unix seconds | required for the official index, optional otherwise. Covered by the index signature. A stale index (now past `expires_at`) is shown as stale; it does not block installing a pinned current entry (§3.1) |
| `revoked` (optional) | array of `id@version` | covered by the index signature. An entry whose `id@version` is listed cannot be installed, and an installed one is shown as revoked (§3.1). The pause/disable behaviour of an installed revoked add-on is #932 |
| `signature` (optional) | `{key, sig}` | a signature over the index (§3.1). **Required for the official index**, with the key pinned in core; optional for others |

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
| `capabilities` | exactly the manifest's capabilities, sorted, from the closed list (`choose_model`, `credential`, `git_push`, `network`, `pty`, `serve_http`, `spawn_agent`) |
| `min_orch` | lowest orch core version that runs it, grammar of `min_core` in addon-api §2. Must equal the manifest's `min_core` |
| `api` (optional) | display copy of the manifest's api range. The manifest decides |
| `runtime` (optional) | `{kind, lock}`: the lockfile kind (`python-requirements` or `node-package-lock`) and its path inside the package. **A display copy:** the manifest's `runtime` is the truth and the two MUST be equal, both present or both absent (`market.manifest_mismatch`, §8.1) |
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

### 3.1 The signed index

The official marketplace replaces the signed add-on catalog (addon-platform §3), which has a sequence number, an expiry
and revocation. The official index keeps all three, so it is signed as a whole:

```
sig = Sign(key, "orch/market/1/index|" || cj({
        "market":     <canonical source URL>,
        "version":    <index version>,
        "expires_at": <integer>,
        "entries":    [[id, version, package_sha256], …]   // sorted by id
        "revoked":    [id@version, …]}))                   // sorted
```

(suite 2, as §4.3). Titles, summaries and the other display fields are not covered; everything a verifier acts on
is. The key is the official marketplace key pinned in core (§4.2), never one read from the index.

- **Verify first.** For the official index the signature is checked right after the strict parse and the URL check
  (`market.index_unsigned`), before any value is taken from it. A host MUST NOT record `last_version`, the highest
  seen versions, the reserved-id additions or the revocations from an index it has not verified. So an unsigned file
  that claims version 2^53−1 cannot freeze later honest indexes, and an old validly signed index replayed to a host
  that never saw a newer one still carries its old `expires_at`.
- **Expiry.** `expires_at` is set by the official CI (about 30 days ahead; provisional). A stale index is a warning in the review and
  in `market list`. It does not block installing the pinned current entries, but it blocks `--allow-downgrade`. A
  host that cannot refresh keeps working on what it has.
- **Rollback floor.** `last_version` is the version of the last *verified* official index. The per-add-on floor is the
  installed version, plus the highest version of an entry that a human reviewed in an install or update; merely seeing
  an entry (an agent may run `market refresh`) never raises it, so a hostile or compromised source cannot block later
  versions of an id by showing a huge version.
- **Revocation.** Listing `id@version` in `revoked` refuses new installs of it (`market.revoked`). Tracking and
  pausing installed revoked add-ons is #932; the field and its signature coverage are fixed now so that it needs no new
  format.
- **Other marketplaces** may sign their index with any key they like. A present signature MUST verify
  (`market.index_unsigned` otherwise); it is remembered by TOFU like a publisher key (§4.4). It is not required.

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
| official | URL equals the pinned official URL | the **index** MUST carry `expires_at` and a `signature` by the official key pinned in the core release, and verify (`market.index_unsigned`, §3.1); and every **entry** MUST carry a `publisher` whose key equals that official key and verify (`market.official_unsigned`). Both are required: the index signature protects the set, the version and the revocations; the entry signature protects each entry when it is mirrored |
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
| 2 | `source.git` of the index equals the URL it was added from. For the official index (and for any index that carries one) the index signature, `expires_at` and key are verified now, before anything is taken from the index | `market.source_mismatch`, `market.index_unsigned` |
| 3 | `version` is not lower than the last version of a *verified* index for this URL | `market.rollback` |
| 4 | Class (§4.2): an "other" marketplace needs OS-user isolation (§10) | `market.community_unisolated` |
| 5 | Hub policy, when a hub is joined (§9): block list, then allow list | `market.blocked`, `market.not_allowed` |
| 6 | Select the entry by `id`; the index does not list `id@version` as revoked; its git host is the marketplace's host or on the allow list (§5.1); reserved ids; an installed add-on of this id came from this URL; the entry's `version` is not lower than the installed one or the highest seen for this `(URL, id)` | `market.unknown_addon`, `market.revoked`, `market.host_not_allowed`, `market.reserved_id`, `market.origin_changed`, `market.rollback` |
| 7 | Publisher: official key rule, signature, TOFU (§4) | `market.official_unsigned`, `market.bad_signature`, `market.publisher_key_changed` |
| 8 | Hub capability ceiling (§9): entry capabilities ⊆ ceiling | `market.capability_ceiling` |
| 9 | `min_orch` ≤ the running core version (a `.devN` build sorts before its release; an unparsable version fails) | `market.min_orch` |
| 10 | (the checks above need no package) | |
| 11 | **Fetch** into a fresh staging directory (§5.1): every connection address is checked, then the fetched object id must equal `commit` | `market.address_refused`, `market.fetch_failed`, `market.commit_mismatch` |
| 12 | **Read once**: `read_package` on the staged subdirectory (all §8.1 rules). From here every decision uses those bytes, never the directory | `market.package_invalid` |
| 13 | The package digest equals `package_sha256` of the entry | `market.digest_mismatch` |
| 14 | The manifest agrees with the entry: schema `orch.addon/3`, `name` = `id`, `version`, sorted `capabilities`, `min_core` = `min_orch`, and `runtime` equal (or both absent) | `market.manifest_mismatch` |
| 15 | If the **manifest** declares `runtime`: the lockfile it names exists in the package and passes the lockfile rules (§8). No other file is ever treated as a lockfile | `market.lock_missing`, `market.lock_invalid` |
| 16 | Compatibility with this core (`incompatibility()` of addon-api §3) | existing `addon.incompatible` |
| 17 | **Review**, then the human signs `addon.granted` for exactly `grant_event(manifest, package)` of the staged bytes | the owner declines |
| 18 | Under the store lock, the host re-reads the staged package, checks its digest equals the digest in the signed grant, appends `addon.granted`, swaps the directory by rename, records origin and (now) the TOFU key | `market.staged_changed` |
| 19 | If the granted manifest declares `runtime`: build the dependency environment (§8.2) from the lockfile bytes of the granted package, by the human-only op `market.env_build {name}` (`orch addon install`, `update` and `setup` call it right after the signature). The add-on stays unavailable ("needs setup") until this has succeeded; from then on the environment is re-verified before every run | `market.env_failed`, `market.env_changed` |

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

#### 5.1.1 Git transport: closed operations in `orch.gitsafe`

Fetching and exporting are **not** done by calling `git` from the marketplace code. Per the rule "the class of config
that runs a program is open-ended" (#541, #565, #584), they are two closed operations in `orch.gitsafe`, the one durable
place that builds git environments and arguments, and G2 MUST implement them as such. The marketplace code calls them
with a URL, a commit and a subdirectory, and cannot pass an option.

**`fetch_commit(url, commit, staging)`**

- *Environment:* `gitsafe.isolated_env`: nothing inherited from the caller (no `GIT_*`, no `HTTP_PROXY`/`HTTPS_PROXY`/
  `ALL_PROXY`/`NO_PROXY`, no `GIT_SSH*`, no `GIT_ASKPASS`), a throw-away `HOME`, `GIT_CONFIG_GLOBAL` and
  `GIT_CONFIG_SYSTEM` pointing at an empty file, `GIT_CONFIG_NOSYSTEM=1`, `GIT_TERMINAL_PROMPT=0`, `GIT_NO_LAZY_FETCH=1`.
  So no proxy can resolve names for us, and no `url.<x>.insteadOf` can redirect.
- *Repository:* `git init --template= --object-format=<sha1|sha256>`, chosen by the length of `commit` (40 or 64).
- *Settings, all given with `-c` on the command:* `protocol.allow=never`, `protocol.https.allow=always`,
  `http.followRedirects=false`, `http.proxy=` (empty), `transfer.fsckObjects=true`, `fetch.fsckObjects=true`,
  `core.hooksPath=/dev/null`, `core.symlinks=false`, `submodule.recurse=false`, and
  **`http.curloptResolve=<host>:443:<checked address>`**, so libcurl connects to exactly the address that passed
  §5.1 and does not resolve again (this is what makes "no second resolution" true; it needs git ≥ 2.37, and a git
  without it is refused: `market.fetch_failed`). A redirect is a refusal (`market.address_refused`).
- *Command:* `git fetch --depth 1 --no-tags --no-recurse-submodules <url> <commit>`, with a wall-clock limit and a
  byte limit on the pack (the §8.1 total plus a margin). Then `git rev-parse FETCH_HEAD` MUST equal `commit`
  (`market.commit_mismatch`).
- *Cross-fork note:* GitHub serves a commit that exists only in a fork under the parent's URL. The digest still binds
  the bytes, but the source line in the review reads "source as claimed by the marketplace", never "from <repo>".

**`export_package(staging, commit, subdir)`** returns a `Package` **in memory**; nothing from the repository is
written to disk.

1. `git ls-tree -r -z -l <commit>:<subdir>` (plumbing: no checkout, so no `.gitattributes` filter, `export-subst`,
   `export-ignore`, end-of-line conversion, LFS smudge or hook can touch a byte).
2. **Judge the listing before reading any blob** (`market.package_invalid`, vectors `tree_*`): every mode is `100644`
   or `100755` (so no symbolic link `120000`, no submodule `160000`); every path passes the §8.1 `check_path` (so no
   `..`, no absolute path, no empty or `.`-leading segment, therefore no `.git`, no non-ASCII, no `.so`/`.pyc`);
   names are unique after NFC and case folding, and no name is both a file and a directory; the entry count, each
   size and the total from `-l` are within the §8.1 caps; `orch-addon.json` is at the top.
3. `git cat-file blob <id>` for each listed entry, with the size checked against the listing, building the
   `{path: bytes}` map. A blob is never written to a path derived from the tree.
4. `package_digest` of that map is what steps 13 to 15 compare. The staging directory holds orch's own writes only
   (the temporary repository), and a hostile tree never reaches the disk.

Tests G2 MUST add (a local https server and a local repository): a redirect gives `address_refused`; a name that
resolves to a public address and then to `127.0.0.1` connects to the first only; a tree with `..`, `.git`,
`A` and `a`, an oversized blob or 10^5 entries gives `package_invalid` with nothing written; an environment with
`GIT_CONFIG_*`, `HTTPS_PROXY` or a repository-local config set by the caller changes nothing.

**Digest of the author's working tree.** `orch addon pack/dev` (G5) MUST compute `package_sha256` with the same
`export_package` on a commit, not from the author's working tree: `autocrlf`, LFS smudge and `export-ignore` change
working-tree bytes, and authors would meet a systematic `market.digest_mismatch`.

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
6. Steps 17 to 19 of §5. On failure at any step up to and including step 18 the old version stays installed and active.
   **Step 19 is after the swap (G4, D5):** the signed grant and the new package are in place when the environment is
   built, so a failure at step 19 (a dependency that was fine at stage time and is not now) leaves the new version
   signed but "needs setup", not running; the old version does not come back by itself. Staging downloads and verifies
   every artifact first (§8.2, D3), so this is rare: a missing or wrong dependency refuses the stage while the old
   version still runs. The person retries with `orch addon setup NAME` or removes the add-on.

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

A package that needs libraries or another runtime declares one lockfile in its **manifest** (`runtime`: `{kind, lock}`)
and ships it in the package. The manifest is inside the digest and is what the owner signs as `grant_event`, so *which*
file is the lockfile, its kind, and whether dependencies are installed at all are all signed; the index only repeats
them, and a difference is `market.manifest_mismatch`. Installing at step 19, and any later rebuild of the
environment (a retry, a new machine), reads the `runtime` of the granted manifest and nothing from the index. Two
lockfiles in one package cannot be chosen between by anyone but the manifest's author. **The lockfile is a file of the package, so it is inside `package_sha256`** (§8.1 digest
covers every file). Changing one hash in it changes the digest and needs a new grant. The `runtime` key is a new optional
key of `orch.addon/3` that G4 adds to the `addon-manifest` schema (this task fixes its shape and the agreement rule; the
review line "installs these packages" is derived from the granted manifest and lockfile, not from the index).

| `runtime.kind` | File | Rules (all `market.lock_invalid`) |
|---|---|---|
| `python-requirements` | pip `requirements.txt` in hash mode (`uv export --format requirements-txt` produces it) | every logical line is a requirement followed by one or more `--hash=sha256:<64 hex>` and nothing else. The requirement part MUST parse as a real PEP 508 requirement (not a regular expression over text, so an option cannot hide in the marker): one name, exactly one specifier `==` with a full version (no `*`), an optional marker, no URL. Option lines (`--index-url`, `--extra-index-url`, `--find-links`, `-e`, `-r`) and VCS or URL requirements are therefore refused |
| `node-package-lock` | `package-lock.json`, `lockfileVersion` the integer 3 (not `3.0`) | strict parse. Every package key is `node_modules/<name>` repeated for nesting (names by the npm grammar, so no `..`, no empty or dot segment, nothing outside `node_modules/`), has a `version`, is not a `link`, and its `resolved` is **exactly** `https://registry.npmjs.org/<name>/-/<basename>-<version>.tgz` for the key's name and the entry's version (registry host pinned in core per ecosystem). `integrity` is exactly one `sha512-` plus the base64 of a 64-byte digest, nothing more. The root entry (`""`, the add-on itself) MUST NOT name a `resolved`, `integrity`, `link` or `inBundle`, and every dependency it declares MUST be a locked entry. The names and versions the review shows are derived from these checked keys only |

**One parser.** `orch.addons.lockfile.check` is the only code that reads a lockfile; it gives the verdict of the
reference checker on every `lock` vector, and its rows (name, version, hashes, markers, flags) are the only source of
what the review shows, what an update diff compares, what is downloaded and what is installed. Lock text never reaches
an installer. Limits: the file at most 1 MiB of UTF-8 without control characters, at most 1000 rows. A `python-requirements`
manifest's `entry.cmd[0]` is `{python}` (never a python found on PATH, for any add-on; needs add-on API 1.1) and a `node-package-lock` manifest's is `node` (the command the owner reads is the
interpreter the dependencies were chosen for). The signed `addon.granted` carries `runtime {kind, lock, packages}`
(D6: more code arrives after the signature, so the signature names it); the review lists every locked package, and
whether it is compiled, not on this computer, or development only.

**Scripts are not judged by the lockfile.** Flags such as `hasInstallScript` are the publisher's claim, so a lock is neither accepted nor refused for them. The rule is that no dependency script ever runs (§8.2).

More kinds (other runtimes) are an additive change to the schema's enum with their own rules and vectors.

### 8.2 Installing, and keeping the environment inside the guarantee

The package is verified on every run (the runner copies in-memory bytes into a fresh read-only directory). A
dependency environment is persistent code too, so it gets the same guarantee: **an environment runs only if it is
exactly the tree the host built from the granted lockfile**, checked before every run.

**Staging (D3).** At `market.stage`, after step 16, the host plans the lock for this computer (Python markers for the
interpreter that runs add-ons; Node `dev`, `optional`, `os` and `cpu`), then **downloads and verifies every artifact**
(§5.1: one fetcher, `orch.market.download`) into a private cache. A package this computer cannot install, a
source-only dependency, a hash the server does not deliver, a wheel that would put an auto-running file in the
environment, a tarball with links or bundled code: each refuses the stage before the person signs, while the old
version still runs. Nothing is unpacked yet.

**The fetcher.** Only `https` on port 443, the host compared whole against the list for the lock kind (Python:
`pypi.org`, `files.pythonhosted.org`; Node: `registry.npmjs.org`). Every address a name resolves to is judged; the
connection goes to the first checked address with the name as TLS server name and `Host`, with no second lookup; a
redirect is `market.address_refused`. Each artifact streams to a fresh `O_EXCL` file, is hashed while it streams
and is compared with the lock BEFORE it gets its name; a cache hit is hashed again on every use. Caps: 64 MiB per
artifact, 256 MiB per add-on, 8 MiB for a Python project index, a deadline for the whole stage or build. For Python
only a wheel of exactly the locked name and version, whose sha256 the lock lists and whose tags this interpreter
supports, is chosen (none: `no_binary`; only source distributions: `sdist_only`).

**Building** (step 19, after the signed grant, by the host and never by the add-on; `market.env_build`, human-only):

1. *The grant and its bytes.* Under the store lock the latest grant (on, with `runtime`) is read, then the lock is
   released. The package is read into memory from `addons/<name>` and its digest must equal the grant's: that
   in-memory package is the only source of lock bytes, and no installer ever gets a path into the package or into
   staging. One flock per workspace and add-on.
2. *Fresh and ours.* The environment is built in a new `mkdtemp` directory under `<state>/addon-env/` (host-user
   private, same file system) and `rename`d to `<state>/addon-env/<workspace id>/<name>/<package digest hex[:16]>/`
   under the store lock and the flock, after the grant is read again: a changed grant discards the build
   (`superseded`). **A target that already exists is removed, never adopted** (D4: refusing it would let anyone who can
   create the directory block an add-on for ever): it is deleted through a directory descriptor and replaced. Not
   shared between add-ons, workspaces or versions.
3. *Nothing resolves, nothing from a dependency runs, nothing connects.* Python (D1): orch writes `req.txt` from the
   checked rows (`name==version --hash=sha256:<chosen>`, no author text), copies the verified wheels into the build
   directory and runs `pip install --isolated --no-index --find-links <wheels> --require-hashes --no-deps
   --only-binary=:all: --no-compile --no-cache-dir --no-input --target <env>` with the host's own interpreter and an
   environment made from nothing (fixed `PATH`, empty `HOME` and `TMPDIR`, no `PIP_*`, no proxy, no `PYTHON*`), a
   time and output limit. Node (D2): orch unpacks each verified tarball itself (`orch.market.untar`): regular files and
   directories only, the first path component dropped, no link of any kind, no `node_modules` segment inside a
   tarball (bundled code is not in the lock), no case twins, limits on entries, file size, decompressed bytes and
   header size, modes normalized; no npm, no `.bin` links, no script. A dependency whose only form is a source
   distribution is refused, not built (`sdist_only`).
4. *No interpreter ever starts inside the environment.* The host does not run `<env>/bin/python` or any tool from
   the environment to build, check or stamp it. At run time Python dependencies are put on the path with `PYTHONPATH`
   and the interpreter runs with `-S` (no `site`, no `.pth`); Node finds them through a `node_modules` link made by
   orch inside the runner's private copy of the package.
5. *Auto-running files and escapes are refused.* After installing, the whole tree is walked without following a link:
   regular files and directories only, one link each, no path that leaves the environment (absolute, `..`, empty or `.`
   segment, backslash, control character), at the environment root no `*.pth`, `sitecustomize*` or `usercustomize*`
   (any case), at most 20 000 entries, 256 MiB, 64 levels (`env_paths` vectors).
6. *Stamp.* The host writes `<digest dir>.stamp.json` next to the environment (0600, atomic): `{v, ws, name,
   digest, kind, tree, dirs, files, bytes, abi, built_at}`. `tree` is `sha256:` over `<sha256>  <mode>  <path>` lines
   of every file in byte order of the path (the shape of the §8.1 package digest, plus the mode, the `env_stamp`
   vectors), `dirs` the same over the directories (an added empty directory shows), `abi` the interpreter's cache tag and
   best wheel tag, or the Node platform and architecture.
7. *Tidy.* The name's other digest directories and the cache files this build used are removed. At start the host
   removes every `.build-*`, an unstamped target, environments of uninstalled names and of old digests, and cache
   files older than a day (`addonenv.recover`).

**Checking** (before every start, **inside `runner.call`**, like the package, C1): every component is opened without
following a link, the stamp is the one for this workspace, add-on, package digest and kind, its `abi` is this
computer's, and the walk of the tree hashes to the stamp's `tree`, `dirs`, file count and size; otherwise the add-on does
not start (`market.env_changed`, reason `missing`, `changed`, `digest` or `abi`, shown as "needs setup" or
"environment changed" and offered `orch addon setup`). Nothing is rebuilt and nothing connects at run time; Tier A
(§15.1): a same-user process that forges both the tree and the stamp is the stated limit. Equivalent and allowed: copy the
environment into the same fresh read-only directory as the package and hash the copy. A file planted before the build
(refused by step 1), added, changed, removed or re-moded afterwards is caught here.

A failure at any step leaves no environment and no running add-on; the grant stays signed, and the owner can retry
(`orch addon setup NAME`) or remove (which deletes the environment, §7). Having dependencies does not change the add-on's capabilities: `network` stays a capability, and installing
dependencies is the host's act, not available to the add-on.

## 9. Hub policy hook (interface only)

With a hub joined, the workspace host MUST ask the hub's policy before any marketplace use. The hub side is D14 (#927),
the enforcement in core is G7 (#928); this section fixes only what the policy looks like so the vectors and the order of
checks are stable.

The policy object is **`orch.hub-policy/1`**, strict: exactly these three keys, no others.

```
{ "allow":   [<canonical marketplace URL>, …],
  "block":   [<canonical marketplace URL>, …],
  "ceiling": [<capability>, …] | null }          // null = no ceiling
```

`allow` and `block` are lists of canonical URLs (§4.1) and membership is equality of a URL with a list element: never a
substring test, never a prefix. A missing key, a wrong type, an unknown key, a non-canonical URL or an unknown
capability makes the **whole policy malformed**, and a malformed policy is an empty allow list (`market.not_allowed`).
The same policy also gates `market add` and `market refresh`: no network access to a blocked or unlisted marketplace.
"A hub is joined" is read from the workspace's signed log, not from a local file: a workspace whose log says it joined a
hub but whose hub record is missing or unreadable is *not* standalone; it refuses (fail closed).

| Rule | |
|---|---|
| Without a hub the policy is `none` and the owner decides alone (D79). Standalone must not call a hub | |
| With a hub, a marketplace that is not in `allow` is refused, **including the official one** | `market.not_allowed` |
| `block` beats `allow` | `market.blocked` |
| A policy that cannot be read, is expired or is malformed is **an empty allow list** (fail closed) | `market.not_allowed` |
| `ceiling` is checked on the index entry (step 8) **and again on the staged manifest and on the grant event** at step 17/18. The index is only a claim | `market.capability_ceiling` |
| A hub never installs or grants for a workspace; it can only refuse | |
| Policy does not override the human gate: even an allowed marketplace needs the signed grant | |

Whether the ceiling also applies to built-in add-ons and to grants that already exist is a D14/G7 decision; the spec's provisional answer is yes (§15.2, point 4).

## 10. Community marketplaces

Until per-add-on OS-user isolation (#287) exists, any marketplace other than the official one, and any direct source,
is refused (`market.community_unisolated`) and `orch market add` for such a URL is refused the same way. The reason: a
granted add-on today runs as the person, and a third-party add-on with `network` or `spawn_agent` would run its
publisher's code with the person's files and keys within reach. A review cannot make that safe; isolation can.

Isolation is a property core detects (a configured, working per-add-on user), not a setting the owner or an index can
claim. (Whether a hub-pinned company marketplace may be allowed earlier is open point 12 in §15.3.) When #287 lands, G-follow-up #929 flips this check; the format and the flow do not change. The local folder used
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
| **I1 Digest binding** | What runs is exactly the bytes whose §8.1 digest equals both the index entry and the digest inside the owner-signed `addon.granted`; the runner re-checks it on every run. **The same holds for the dependency environment**: it runs only if its tree hash equals the stamp the host wrote from the granted lockfile, checked before every run (§8.2), so code that is not in the package is covered by an equally checked binding. No commit, tag, signature, index version or marketplace name is a substitute for it |
| **I2 Human gate by custody** | Adding a marketplace, installing, updating, removing and granting need a human; an agent or mandate is refused before any network access, and cannot sign |
| **I3 Read once, in staging** | Fetched bytes live in a private staging directory, are read once into memory with `read_package`, and every later step (review, grant, swap) uses those bytes or re-compares their digest under the store lock |
| **I4 The pin does not move, and signatures name their place** | An entry names a full object id and a digest; nothing floats. A different release is a different index state, a new review and a new grant. Index and add-on versions cannot go backwards (a validly signed old entry is still refused), an add-on cannot change marketplace, and a publisher signature is valid only in the marketplace URL it signed |
| **I5 Dependencies are inside the digest and inert until granted** | Lockfiles are package files; they are hash-pinned, resolved by nothing, and parsed by a real grammar, not by pattern. They are installed only after the signed grant, with dependency scripts disabled by the installer (never by trusting lock metadata), from allow-listed hosts, into a fresh environment of the add-on's own that is never adopted from an existing directory, never started as an interpreter by the host, free of auto-running files, and re-verified against its stamp before every run |
| **I6 Policy only restricts, and fails closed** | Hub policy, the community gate and the ceiling can only refuse; unreadable or missing policy under a hub means nothing is allowed; block beats allow; marketplaces are identified by URL, never by self-declared name; the ceiling is checked on the index, the manifest and the grant |
| **I7 The installer connects only to public, expected places** | Fetch and download targets are https, standard port, a host that is the marketplace's own or on a list the index cannot extend, and a public unicast address checked after name resolution; redirects are refused |

| Threat | What happens | Answered by |
|---|---|---|
| **Malicious or compromised index** (lies about digest, capabilities, commit) | The index can only point at bytes. Digest, capabilities and manifest facts are re-derived from the package that was read (steps 13, 14), and the owner signs what the review derives from the package, not what the index says | I1, I3 |
| **Compromised publisher** (pushes a bad release, or signs one) | It reaches a staged update that needs a new review and signature, showing the capability and dependency diff and "new key" if the key changed. The old version keeps running and nothing is widened without the signature | I1, I4, §6 |
| **Official repo or URL taken over without the official key** (account or token compromise, repo-jack): version 2^53−1 to freeze updates, an old index served to fresh hosts, withheld updates, an edited reserved list, a removed entry passed off as revocation | The index is signed by the pinned key over URL, version, expiry, entries and revocations; nothing is recorded from an unverified index; the reserved set ships in core; revocation is an explicit signed list | I4, §3.1 |
| **Signature replay** (a good publisher's signed entry copied into a hostile or mirror marketplace) | The signature covers the marketplace URL, so it fails there (`bad_signature`) | I4 |
| **Stolen or replaced publisher key** | `market.publisher_key_changed`; replacing needs `trust --replace` by a human with both fingerprints. A key is continuity, not identity, and the review says so | §4.4, I2 |
| **Git history rewrite, force-pushed tags, moved branches** | Fetch is by full object id and the digest is the binding. A rewritten history cannot change what is accepted. A removed commit means `market.fetch_failed`, never a silent fallback to a branch head. The diff flags "not a fast-forward" | I1, I4 |
| **Index and entry rollback** (an old index, or an old validly signed entry, replayed to bring back a vulnerable release) | An index `version` lower than the last seen is refused; an entry `version` lower than the installed one or the highest seen for the `(URL, id)` is refused even when its signature is valid | I4 |
| **SSRF through the index** (`source.git` pointing at `127.0.0.1`, the cloud metadata address, an internal host, another port, or a public host that redirects inward) | Schema shape (no IP literal, no port, no `localhost`), the host rule, address checks after resolution with no second lookup, no redirects; the allow list cannot be extended by the index | I7 |
| **TOCTOU between fetch and grant** (directory changed, symlink swapped, tag moved after the review) | Plumbing-only export into private staging; `read_package` with `O_NOFOLLOW`; the grant is for the digest of the staged bytes and the host re-compares inside the lock before the swap; a change gives `market.staged_changed` | I3, I1 |
| **Hostile git content** (filters, hooks, `.gitattributes`, symlinks, submodules, LFS pointers, `.git` inside the tree) | Private git config, no checkout, blobs written as plain files, link and submodule modes refused, `.`-leading names refused by §8.1 | I3 |
| **Dependency confusion** (a private name resolved from a public index; a lookalike package) | Nothing is resolved at install: the lockfile names exact versions, URLs and hashes, and option lines that add indexes are refused. The same add-on id from another marketplace is refused (`origin_changed`); other marketplaces cannot claim reserved ids | I5, I4 |
| **Planted or edited environment** (an agent creates the predictable env directory first, or edits it later; a `sitecustomize.py` or `.pth` that runs whenever an interpreter starts there) | Fresh `mkdtemp` and rename, existing target refused; `PYTHONPATH` from the host's interpreter so no site directory exists; auto-running files refused; the tree hash is verified before every run | I1, I5 |
| **User config redirecting the installer** (`pip.conf`, `PIP_*`, `~/.npmrc`, `NODE_OPTIONS`) | `--isolated`, empty user/global config, scrubbed environment | I5 |
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
| `market.index_unsigned` | the official index has no valid signature by the pinned key or no `expires_at`, or a signature present on another index does not verify | 2 |
| `market.rollback` | index version lower than the last seen, or entry version lower than the installed or highest seen one | 3, 6 |
| `market.community_unisolated` | a non-official marketplace or direct source without OS-user isolation | 4 |
| `market.blocked` | the hub's block list names this marketplace | 5 |
| `market.not_allowed` | with a hub, the marketplace is not on the allow list (or the policy is unreadable) | 5 |
| `market.unknown_addon` | the id is not in the index | 6 |
| `market.revoked` | the signed index lists this `id@version` as revoked | 6 |
| `market.host_not_allowed` | the entry's git host is neither the marketplace's host nor on the host allow list | 6 |
| `market.reserved_id` | a non-official marketplace claims a built-in id | 6 |
| `market.origin_changed` | the installed add-on came from a different marketplace | 6 |
| `market.version_reused` | the entry has the installed version number but other bytes | 6 |
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
| `market.env_failed` | the dependency environment could not be built, or a dependency cannot be had (stage time and step 19). Fixed reason words: `no_binary`, `sdist_only`, `download`, `hash_mismatch`, `auto_run_file`, `path_escape`, `link`, `too_large`, `installer`, `package`, `superseded`, `lock` (two applicable lines of one name), `platform` (a required package that does not run here) | 16, 19 |
| `market.env_changed` | at run time, the environment no longer matches its stamp or the stamp is for another package digest. Reasons: `missing`, `changed`, `digest`, `abi` | run |
| `market.actor_refused` | an agent or mandate asked for a human-only action | 0 |

Rows without a vector in `vectors_market.json` (`fetch_failed`, `commit_mismatch`, `staged_changed`, `env_failed`) need
git, a network or a running host; G2 to G4 test them with a local git repo and a fake runner.

## 14. Files and checks in this task

| File | What |
|---|---|
| `src/orch/addons/schemas/market.v1.json` | strict JSON Schema 2020-12 for the index |
| `tests/addons/vectors_market.json` | the vectors. `check` is one of `index` (schema and index rules), `install` (steps 0 to 15 with a context), `lock`, `address`, `tree` (§5.1.1), `env_paths`, `env_stamp` (§8.2) |
| `tests/addons/market_ref.py` | the reference checker and the signing helpers (test code) |
| `tests/addons/make_vectors_market.py` | builds the vectors; run again to change them. Signatures are random, so the file is the artefact |
| `tests/addons/test_market_vectors.py` | runs every vector, checks the schema, that every code in §13 has a vector or is listed as untestable here, and that signatures verify only for the signed bytes |
| `tests/addons/test_market_adversarial.py` | the L2 reviewer's cases (findings L2M-1 to L2M-10), kept as an independent second check |

An `install` context is `{actor, core_version, market_repo, isolation, official{repo,key}, hub|null, builtin[],
official_ids[], moved[], git_hosts[], installed{id->{origin,version,package_sha256}}, known_keys{"<url>|<id>"->key},
seen{"<url>|<id>"->version}, last_version}`. A product implementation is conformant when it gives the same `ok` or
`refuse` for every case.

## 15. Residual risks and open points

### 15.1 Stated limits (Tier A)

The install record (TOFU keys, highest versions seen, origins, the host allow list), the hub-join record and any
isolation flag are files on the person's own account, and a same-user agent (tier A) can write them. For the official
shelf that is harmless: the key is pinned in core, the digest binds the bytes, and the grant needs the person's
signature. For any other marketplace (after #287) TOFU and `allow-host` are *advisory* against such an agent. Isolation
(#287) MUST be detected from the operating system, never from a same-user-writable flag; a test keeps that.

Crash safety: between appending `addon.granted` and the directory rename (step 18) the registry sees `changed` and
fails closed; recovery at start finishes or rolls the rename back by comparing digests with the last grant
(addon-platform §4), and records the origin and the TOFU key idempotently.

### 15.2 Answers adopted from the L2 review (provisional, owner may overrule)

These are the spec's answers; each is marked **provisional** until the owner confirms it.

1. **Provenance in `addon.granted`: no** for v1. The digest is the binding, and a signed origin would invite trusting
   it. Revisit with G7 if the hub must audit origin. *Provisional.*
2. **Official key:** the same root, its own certificate with `purpose: "marketplace"` and its own CI environment, never
   the catalog or core key. Core pins a list of two (current and next) so a rotation is not an emergency release.
   Revocation is through the signed index (§3.1). *Provisional.*
3. **Catalog versus marketplace:** retire the signed add-on catalog for non-built-in add-ons once G6 lands, and only
   because the official index now has the catalog's guarantees (version, expiry, revocation: §3.1). Keep the bundled
   snapshot for the built-ins. The staged grant (`addon.stage`, grant from staged) is designed in addon-platform §4;
   G3 must find it built or build it. *Provisional.*
4. **Ceiling scope:** the hub ceiling binds new grants of every add-on, built-ins included; an existing grant over the
   ceiling is paused with "needs approval". A ceiling that existing grants escape is not a ceiling. *Provisional.*
5. **Direct sources:** refused before #287. After it, allowed only with an explicit commit and digest. *Provisional.*
6. **Dependency kinds:** Python and Node only in v1. Compiled wheels are allowed only from the pinned PyPI with hashes;
   `.pth`, `sitecustomize` and `usercustomize` are refused (§8.2). *Provisional.*
7. **Index signature:** required for the official index now (§3.1), optional for others. *Provisional.*
8. **Yank and advisories, secrets:** `revoked` is reserved and signed in the official index now (§3.1); tracking and
   pausing installed revoked add-ons is #932; add-on secrets stay #930. *Provisional.*
9. **Freshness:** signed `expires_at` (about 30 days). A stale index is a warning, not a block, for installing the
   current pinned entries; it blocks `--allow-downgrade`. *Provisional.*
10. **TOFU scope:** per (canonical URL, id), the stricter choice. With canonical URLs (§4.1) it does not fragment.
    *Provisional.*
11. **Parsers and host lists:** add `packaging` as a core dependency (a hand-written marker grammar is the riskier
    option). Default git hosts `github.com`, `gitlab.com`; npm `registry.npmjs.org`; PyPI `pypi.org` and
    `files.pythonhosted.org`. *Provisional.*

### 15.3 Open for the owner

12. **Company marketplaces before #287.** The concept page shows "official plus your own company marketplace first";
    §10 refuses every non-official marketplace until #287. Two options:

    - **Option A (as written, safest):** refuse every non-official marketplace and direct source until per-add-on
      OS-user isolation exists. A company waits for #287. Nothing third-party runs as the person, ever, before then.
    - **Option B (reviewer's recommendation):** allow a company marketplace before #287 **only when a hub is joined and
      its policy lists the marketplace in `allow` with a pinned publisher key** (pinned by the hub, not remembered by
      TOFU). Without a hub the refusal stays. The code is third-party code running as the person, but the company's
      admin chose it and pinned who signs it, and every install still needs the person's own signature on the exact
      digest. The residual risk is a compromised company publisher key or a hostile admin, and the tier-A agent
      limits of §15.1 do not apply to a key the hub pins.

    The spec, schema and vectors implement Option A. Option B adds one field to `orch.hub-policy/1`
    (`pinned_keys: {url -> key}`) and one branch at step 4; it needs no change to the index format.

## 16. Install, update and remove as built (V3 G3, #921)

Design note `state/design-g3.md`; conditions C1 to C6. What the code does, where it lives:

- **Fetch and export:** `gitsafe.market_export_package` runs `ls-tree -r -z -l` on the commit (or `commit:subdir`), judges the whole
  listing (modes, `check_path`, case twins, file/directory clash, counts, sizes, manifest on top) before it reads a blob, then reads
  every blob with ONE `cat-file --batch` whose output is capped at the listed total. The package fetch has its own pack cap
  (`MARKET_PACKAGE_PACK`), never the index's. `fetch.fetch_package` resolves and checks the entry host like the index fetch.
- **Stage:** `market.stage {id, url?, update?}` (human, owner) runs steps 0 to 16 and writes ONE stage file
  `<state>/hosts/<ws>/market/staged/<stage>.json` (the package in memory form as base64, digest, origin, commit, publisher key, TOFU
  status; 30 minutes; one live stage per add-on name). Nothing is written to `addons/` and no record changes.
- **Grant:** `addon.grant {name, staged}`. The package comes from the stage file (read once, digest and manifest agreement
  checked again), the signed body is only `grant_event`, the marketplace block travels as `material["market"]` and is shown "per
  host, not signed". Under the store lock, before the journal write, `marketinstall.recheck` judges the stage and the install record
  as they are now (C2). After the append, `human.finish` calls `marketinstall.after_grant`: the directory is swapped by digest
  (`.<name>.new-*`, `.<name>.old-*`; a crash is finished by `marketinstall.recover` at start), then `installs.json` gets the
  origin, `seen` is raised and the publisher key is remembered (C3). `installs.json` changes nowhere else.
- **Plain grant (C1):** `addon.grant` without `staged` signs only the bytes of a built-in add-on or the bytes the install record of
  that name holds (a re-enable). A folder copied into `addons/` is `market.not_installed`.
- **Remove:** `market.uninstall {name}` refuses while the add-on is enabled (`market.still_enabled`), deletes the package, drops
  the origin; `seen` and the publisher key stay. `orch addon remove` signs `addon.disable` first.
- **Sandbox marketplace (C5):** `<root>/state/host/dev.json` may carry `market: {url, keys}`. Only a dev host reads it, and only a
  workspace that passes S1 to S6 now gets class `dev` for that URL (the official rules with those keys, reserved ids as for any
  non-official marketplace). Every address rule applies unchanged.
- **Signer (C4):** `material.market` has exactly the keys `url, class, commit, source_note, publisher{fingerprint, tofu},
  index_version, stale, summary, replaces` and only on a request that signs one `addon.granted`; it is rendered under "per host, not
  signed".
- **Host-derived preview:** the update diff and `widened` come from the host (the staged bytes against the signed grant; when the
  installed folder is not the granted bytes everything counts as widened). They are for previews only: no client may shorten the
  signed view because of them, and the signer always renders every signed capability, field, section and artifact kind.

## 17. Dependencies as built (V3 G4, #922)

Design note `state/design-g4.md`; conditions C1 to C7. Deviations from the first text of §6 and §8 are in the text
above: D1 (orch downloads, installers run offline), D2 (no `npm ci`: orch unpacks), D3 (download and verify at stage),
D4 (an existing target is removed, never adopted), D5 (§6 item 6), D6 (`runtime` in the signed grant), D7 (Python
add-ons run with the host interpreter and `-S`), D8 (`python-requirements` is `uv export --format requirements-txt
--no-dev` output).

- **Code:** `orch/addons/lockfile.py` (the one parser and the plan), `orch/addons/envtree.py` (tree rules, walk, stamp,
  `verify`), `orch/market/download.py` and `untar.py` (the fetcher and the unpacker), `orch/market/deps.py` (review block
  and update diff), `orch/host/addonenv.py` (stage fetch, `market.env_build`, remove, recovery), `orch/addons/runner.py`
  (the check inside every call; Python `-S -B`, Node from fixed system directories).
- **Review and diff:** the review lists every package (compiled, not on this computer, development only) and the
  download size; `material["deps"]` carries it, checked by the signer against the SIGNED `runtime` and labelled "listed
  by this computer from the lockfile, covered by the digest". An update diff compares what this computer INSTALLS: a
  package that keeps its version and hash but starts to be installed here is changed; `widened` is set by runtime added
  or changed, and by any package added or changed (removals do not widen).
- **One mechanism for every cap:** `envtree.Budget` (bytes and entries) is the only way a parser consumes input. One
  budget is shared by everything one operation reads (every wheel and tarball of one environment build or stage, every
  file of one walk, every download of one build or stage: a new budget per operation), so N archives cannot add up to N times what one may use; a parser takes the
  budget as a parameter, so a new one cannot forget it.
- **The invariant (stated once, in `envtree.Budget`):** what orch judged is exactly what gets written, and every written
  byte comes out of a Budget. Where orch and the consumer both parse a format they must agree: a wheel is listed by orch's
  own walk of its central directory (the directory must end exactly at the end record, no zip64 record or locator) and
  then by `zipfile`, which is what pip uses, and the two lists must be identical; a tarball with any sparse member
  (old GNU type or PAX `GNU.sparse.*`) is refused, so no hole is ever expanded into bytes nobody counted. pip runs with a
  file size limit, `PIP_CONFIG_FILE=/dev/null` (no global or site config either) and a size watch on the environment it
  writes; the tree walk after it is the last gate. Directory walks and unpacking hold only the chain of parent
  descriptors, never one per directory, so a package with hundreds of sibling directories works under a 256-descriptor
  limit. A wheel is only taken from `files.pythonhosted.org`.
- **Caps, every one checked before the allocation it guards:** lockfile 1 MiB and 1000 rows (counted before rows are
  parsed); download 64 MiB per artifact (the `Content-Length` before the body, the count per chunk), 256 MiB per
  add-on, 8 MiB index; wheel central directory walked by orch entry by entry (20 000 entries and 256 MiB declared in all, drawn before each
  entry is kept; an end record that lies about its count is refused at the first surplus entry); tarball 20 000 members counted as they stream, 64 MiB per file, decompressed bytes counted by
  a reading wrapper (`MAX_BYTES` plus a small allowance per member), PAX and long-name headers 8 KiB, no sparse member;
  environment tree 20 000 entries, 64 levels, 256 MiB, 240 characters per path; pip 300 s and 8 MiB of log; stamp 4 KiB.
- **Run time:** Python is G6's interpreter line without `-E` (`-B -s -S`; `-E` would ignore the `PYTHONPATH=<package>:<environment>` that carries the dependencies, and the environment is built from nothing anyway); Node `node_modules` link to the environment,
  `node` looked up in `/opt/homebrew/bin`, `/usr/local/bin`, `/usr/bin`.
- **Not done / residual:** native builds are refused (O7); an add-on without `network` can still connect until #287
  (O5); Node resolves packages above the run root only in Tier A terms (O4); the full tree hash per run costs about half
  a second at the cap (O2).
