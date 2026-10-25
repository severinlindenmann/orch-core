# The add-on API contract `orch.addon-api/1`

Status: v1.1, 24 Oct 2026 (v1.0 of 22 Oct plus section 8; additions only). Part of the add-on platform ([design](../design/addon-platform.md), section 2). This file
is the contract between orch core and an add-on package. Core declares the API it implements; an add-on declares the
API range it needs. The registry compares them and never runs an add-on that does not fit.

## 1. Version of the API

- Core declares `ADDON_API` (`orch.addons.manifest.ADDON_API`), `"<major>.<minor>"`. Today: `1.1`.
- Inside one major version, changes only **add** things: new optional manifest keys, new capabilities, new events,
  new UI components. Removing or narrowing anything is a new major version. After a major bump, core keeps reading
  the previous major for at least 6 months.
- The API version is not the core version. Core releases may leave `ADDON_API` unchanged.

## 2. The manifest, schema `orch.addon/3`

`orch-addon.json` is strict JSON (unknown keys are refused). Schema `orch.addon/3` is `orch.addon/2` plus:

| Key | Required | Meaning |
|---|---|---|
| `api` | yes | The add-on API range the package needs, e.g. `">=1.0,<2"`. |
| `min_core` | no | The lowest core version the package runs on, e.g. `"2.0.0.dev60"`. |

All other keys (`name`, `version`, `title`, `entry`, `fields`, `sections`, `artifact_kinds`, `capabilities`, `needs`,
`cli`, `skills`, `agents_md`) keep the meaning they have in [the ticket format, section 8](orch-v2-ticket-format.md).

**Range grammar.** One to four comparators joined by `,`, each `<op><major>[.<minor>]` with op one of `>=`, `<=`,
`==`, `>`, `<`. A missing minor is `0`. All comparators must hold (logical AND). Anything else is refused when the
manifest loads.

**Core version grammar (`min_core`).** `<major>[.<minor>[.<patch>]]` with an optional `.dev<N>` suffix. A `.devN` build
sorts before the release it leads up to (`2.0.0.dev999` < `2.0.0`).

**Legacy.** A package with schema `orch.addon/2` has no `api` and no `min_core`. Core reads it as
`api: ">=1.0,<2"` and no `min_core`. A `/2` manifest must not carry `api` or `min_core`. An older core refuses `/3`
(its schema is strict): the wanted fail-closed behaviour. The bundled first-party manifests stay `/2` until they are
released through the catalog.

## 3. Compatibility and the state `incompatible`

An add-on is **compatible** when `ADDON_API` is inside its `api` range and the core version is at least `min_core`.
`orch.addons.manifest.incompatibility(manifest, core_version)` returns the reason, or `None`.

Registry states: `active`, `disabled`, `purged`, `changed`, `missing`, `incompatible`, `unknown`. Order of the
checks: never granted, purged, disabled, no package, package differs from the grant (`changed`), then compatibility.
So `incompatible` means: this is exactly the granted package, and it does not fit this core.

An `incompatible` add-on:

- does not register (no fields, sections, needs rules or artifact kinds) and accepts no writes, like any state other
  than `active`;
- is never started: the runner refuses it with `addon.incompatible` even if called directly;
- keeps its data, shown as inactive;
- is shown as incompatible with a reason (`Registry.incompatible_reason`), never as disabled and never silently.

Fail closed: an `api` range or core version that cannot be parsed counts as incompatible.

## 4. What the grant covers (unchanged)

`addon.granted` binds the package digest, name, version, capabilities and the derived declarations. `api` and
`min_core` live in the manifest, which is inside the digest, so a granted package cannot change them without a new
grant. They are checked at registration and at every run, not signed separately.

## 5. Capabilities

The closed list: `serve_http`, `spawn_agent`, `pty`, `network`, `git_push`, and since 1.1 `choose_model` and
`credential`. A new capability is a minor API bump and needs a review of what it allows.

| Capability | Review line ("can ...") | Rule |
|---|---|---|
| `choose_model` | choose the model for agent starts (you still see it before each start) | core calls the add-on only for an agent start with no explicit model (section 8.3) |
| `credential` | get the key you store for this add-on with 'orch addon login' and send it over the internet | requires `network` (the manifest is refused without it); section 8.4 |

## 6. Runner environment, events and ops (as of API 1.0)

- Runner environment: `ORCH_ADDON` and `ORCH_ADDON_CAPABILITIES` plus the fixed minimal set in
  `orch.addons.runner.child_env`. Nothing else is passed.
- Events: `addon.granted`, `addon.disabled`, `addon.purged`.
- Ops: `addon.grant`, `addon.preview`, `addon.list`, `addon.disable`, `addon.purge`.
- A table of which host ops and event kinds each capability allows, and a test that dispatch enforces it, is planned
  for API 1.1 (design section 2.3). Until it lands, this list is the whole surface and anything not named here is not
  part of the contract.

## 7. Changelog convention

A changelog is plain text, at most 4 KB, and lives in the signed catalog entry, not in the package. It is data: it
is never part of what a person signs.

## 8. API 1.1 (additions only)

An add-on that uses any of this (`choose_model`, `credential`, `settings`, `{python}`) must declare an `api` range that
excludes 1.0 (for example `>=1.1,<2`); the manifest is refused otherwise. This section is the contract; the runner, credential store,
settings op and launch hook that implement it are separate tasks (design G6, #924).

**8.1 The `{python}` entry placeholder.** `entry.cmd[0]` may be exactly `{python}` (it is refused anywhere else, and as
part of a longer argument). Core expands it to `[sys.executable, "-B", "-E", "-s", "-S"]`: core's own interpreter, with no
site-packages and no `PYTHON*` environment, so the add-on is stdlib-only and cannot import orch. Not `-I` (it drops the
script directory) and never `python3` from `PATH`. `{pkg}` keeps its meaning. A package with a lockfile and `runtime` is
the dependency route (G4) and maps the same token to its own interpreter.

**8.2 Settings.** An optional manifest key `settings: {title, schema}` (`title` 1 to 60 characters) declares what the
person can configure. `schema` is a closed JSON-Schema subset, checked when the manifest loads
(`orch.addons.settings_schema`); anything else is refused:

- the root is `type: "object"`; `type` is one of `object`, `string`, `integer`, `boolean`, `array`;
- object: `properties` (at most 64, names `[a-z0-9][a-z0-9_-]{0,63}`, no dot), `additionalProperties: false` (required),
  `required` (names of properties), `maxProperties` (0 to 64);
- string: `enum` (1 to 64 distinct printable strings of at most 64 characters), `maxLength` (1 to 200);
- integer: `minimum`, `maximum` (whole numbers, `minimum <= maximum`); no floats anywhere;
- array: `items` (a schema) and `maxItems` (0 to 64), both required;
- any node: `title` (1 to 60 printable characters); schema nodes nest at most 4 deep.

`pattern`, `$ref`, `format`, `default`, `description`, `oneOf` and every other keyword are refused. An add-on must work
with no settings at all (`null`): there are no required settings in 1.1.

**8.3 `choose_model`.** Core may ask a granted add-on with `choose_model` for the model of an agent start. The input is
exactly `{harness, mode, settings}`; the answer is exactly `{"model": <string or null>}`, and the string must be one core
already allows for that harness. Anything else blocks the start.

**8.4 `credential`.** An add-on with `credential` (and therefore `network`) receives the one key the person stored for it
with `orch addon login <name>` (for the `github` add-on also by typing it in the dashboard's add-on settings, sealed to the signer; #1198 C16) in the call's `params.credential`, only while that capability is granted. orch does not
hand it to agents, and it never appears in a signed text, a log or an add-on answer. Tier A limit (spec 15.1): an agent
running as the same UID can read or write the stored file directly until the per-add-on UID (#287).

**8.5 Reserved ids.** `github`, `landing` and `model-routing` are reserved for the official marketplace
(`orch.market.constants.OFFICIAL_IDS`); another marketplace that lists one is refused with `market.reserved_id`.
