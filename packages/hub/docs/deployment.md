# Deploying the hub (dev, V3)

Spec: orch-core `docs/architecture/orch-v2-hub.md` section 4a (D82). Both layouts run the same binary; what differs is the bind, the
machine and the checks. `orch-hub doctor` reports the layout and runs the checks below.

## One VPS (S11): `serve --listen 127.0.0.1:PORT`

The hub shares the machine with workspace hosts (`orch-host-<ws>`), their agents (`agent-<ws>`) and the edge services. Rules:

- the hub is its own OS user (`orch-hub`); its state is a 0700 directory it owns; agents and hosts are other users and not in its group;
- each host needs the server custody profile (a host user plus agents under their own UID); a host whose agents run as the person
  (P2 style) must not share a machine with a hub (doctor FAILs a host user without a matching agent user);
- only root, the hub user and the host users may dial the loopback link port: an nft rule on `oifname "lo"` with `meta skuid !=`, kept in
  one canonical file, loaded by a unit that also dumps the LOADED table as JSON. Doctor compares file and dump and fails on any
  difference (a rule that was loaded by hand, a different uid set, a wrong port or interface).

The front page of a co-located hub must say "shares a machine with workspaces: weaker isolation".

## Split (S12): `serve --link-listen IP:PORT`

`--listen` stays loopback-only. `--link-listen` is the only way to a network bind: one literal private unicast IP that is assigned
to this machine (no wildcard, hostname, multicast, link-local or public address) and a port. It is refused

- on a machine that has `orch-host-*` or `agent-*` users (the production split puts the hub on a machine without them),
- for a hub running as root,
- unless the TLS key in the key store is the one the card names (`tls_pin_mismatch`; `keys rotate-tls` creates or replaces it).

The pin comes only from the `HK_op`-signed card, never from the handshake. The host refuses to dial without a real pin.

### The dev split on the shared edge host

S12 runs a second hub on the dev edge host (`--split-hub` in the kit), which also holds `orch-host-ws2` and `agent-ws2`. That needs
`--dev-shared-machine`, accepted only with a `dev: true` card and a hub user that is neither a host nor an agent user nor in their
groups. The ready line and doctor print:

> dev split on a shared machine: this proves the network link, not machine separation

So S12 proves a real cross-machine link (macOS host to Linux hub over the overlay) with the pinned SPKI, the exporter-bound `WSK`
proof, strict per-link sequence numbers and the unauthenticated-connection cap. It does not prove "hub alone on its machine"; that is
a doctor unit-test fact (account fixtures) until a third, empty Linux peer exists.

### The firewall is surface reduction, not authorization

The kit accepts the link port on `wt0` only and drops everything else (locally originated traffic arrives on `lo`, so the edge's own
agents are dropped too). That shrinks who can talk to the hub; it authorizes nobody. Authorization is the pinned TLS, the `WSK`
challenge bound to the TLS exporter, and signed link messages. Nothing may depend on the firewall for security, and no reverse-proxy
service may target the link port.

## Restart and pin

The TLS key persists in the key store (`tls.key`, 0600): a restart keeps the pin and the card sequence. A rotation is explicit
(`orch-hub keys rotate-tls`) and re-issues the card; hosts that still hold the old card then fail the pin check before a byte is sent
and must take the new card through the signed-card path.

## The public webhook inbox (follow-ups, orch-core#1285)

Off by default. A follow-up can wait for "a job rang" (GitHub, a CI step, a Databricks job); the hub is the always-on
part with a public address, so it receives the ring and the workspace pulls it over its link. Turn it on with two flags
that go together:

```
orch-hub serve ... --hooks-listen 127.0.0.1:8790 --public-hooks-url https://hub.example.com
```

* `--hooks-listen` is a separate socket with its own small worker pool (8 workers, 16 waiting). It serves only
  `POST /h/<token>`; the admin web API (`--web-listen`) is not involved and does not change. Bind it to loopback and let the
  edge forward `/h/*` to it.
* `--public-hooks-url` is the origin senders use (https; http only on a dev hub). Workspaces show `<origin>/h/<token>`.
* Limits: body at most 256 KiB (checked before any read; pick the GitHub events you need, not `push`), 10 s in total for
  head and body, per-sender and global rate limits, 64 live hooks per workspace, hooks live at most 31 days.
* State: `hooks.db` in the state dir (0600): the hash of each token, its signature secret, and at most one ring row per
  hook (no body, no header). It is not part of the hub backup; a restored hub has no hooks and follow-ups fall back to
  their deadline.
* Edge (Caddy >= 2.8): keep `/h/*` out of the access log, the token is the address:

```
hub.example.com {
    @hooks path /h/*
    log_skip @hooks
    reverse_proxy @hooks 127.0.0.1:8790
    # ... the existing reverse_proxy for the console and API
}
```
