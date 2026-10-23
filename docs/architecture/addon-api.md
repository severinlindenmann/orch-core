# The add-on API contract `orch.addon-api/1`

Status: v1.0, 22 Oct 2026. Part of the add-on platform ([design](../design/addon-platform.md), section 2). This file
is the contract between orch core and an add-on package. Core declares the API it implements; an add-on declares the
API range it needs. The registry compares them and never runs an add-on that does not fit.

## 1. Version of the API

- Core declares `ADDON_API` (`orch.addons.manifest.ADDON_API`), `"<major>.<minor>"`. Today: `1.0`.
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

The closed list: `serve_http`, `spawn_agent`, `pty`, `network`, `git_push`. A new capability is a minor API bump
and needs a review of what it allows.

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
