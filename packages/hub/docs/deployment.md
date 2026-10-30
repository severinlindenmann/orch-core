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

## Link over 443 (R6): TLS passthrough and a WebSocket front

Some networks only let web traffic out (port 443, sometimes through an HTTP proxy). The link then needs one of two public
shapes. Both keep the security where it is today: the link's own TLS 1.3 ends **at the hub**, checked against the pin in the
hub card, with the exporter-bound proof on top. Whatever sits in front only moves bytes; it holds no key that matters and a
wrong or hostile one costs one failed handshake. Addresses are hints (`hub_where`, the connect link): a host stores one only
after a pinned handshake at it passed. No secrets belong in this file or in a front's config for the hub.

| | what the front does | address the host gets | hub flag |
|---|---|---|---|
| Passthrough | forwards the raw TCP stream by the name in the TLS hello; never terminates TLS | `hub.example.org:443` | none (the normal `--link-listen`) |
| WebSocket front | ends its own outer TLS and the WebSocket, forwards the bytes inside | `wss://hub.example.org` | `--link-ws-listen` |

### Passthrough by name on a public server

A small TCP proxy on the public server reads only the server name of the TLS hello (the host sends it: the name of the
address) and forwards the connection to the hub over the private network. It has **no certificate and no key**, so there is
nothing to steal on the public server. This needs a DNS name for the hub; without one use a plain TCP port forward (no name
routing).

```nginx
# nginx, stream module; example names only
stream {
    map $ssl_preread_server_name $hub_link {
        hub.example.org   10.0.0.5:8105;     # the hub's --link-listen address on the private network
        default           127.0.0.1:1;       # anything else: refuse (nothing listens there)
    }
    server {
        listen 443;
        ssl_preread on;
        proxy_pass $hub_link;
        proxy_connect_timeout 5s;
        proxy_timeout 1h;
    }
}
```

HAProxy does the same with `mode tcp`, `tcp-request inspect-delay 5s`, `tcp-request content accept if { req.ssl_hello_type 1 }`
and `use_backend hub if { req.ssl_sni -i hub.example.org }`. If the server also serves other sites on 443, the proxy routes
those names to them the same way.

### WebSocket front

For a front that can only speak HTTP (a shared reverse proxy, an edge that terminates TLS and routes by host name), the hub
has a second listener:

```
orch-hub serve ... --link-listen 10.0.0.5:8105 --link-ws-listen 127.0.0.1:8107 --link-ws-public wss://hub.example.org
```

- `--link-ws-listen IP:PORT` serves **only** `GET /orch/link/1` with a WebSocket upgrade (subprotocol `orch-link.1`, no
  extensions, no `Origin`). Everything else is a 404 and a closed connection: no console, no API, no page. It is plain HTTP
  with no TLS of its own, so bind it to **loopback** (a front on this machine) or one **private** address. A wildcard needs
  `--bind-all`; a public address is refused unless `--link-ws-behind-front` says a TLS-terminating front is the only way to
  it. The same machine rules as `--link-listen` apply to a non-loopback bind.
- It shares the link port's connection limits (and the remote clients' pools), taken before the request is read.
- Whatever the front sends (`X-Forwarded-For`, `Forwarded`, cookies) is **ignored**: a WebSocket link never counts as
  "from this computer" and has no peer address on record, so a front on the hub's own machine cannot make a remote
  workspace look local.
- `--link-ws-public wss://name[:port]` adds that address (kind "public") to the `hub_where` answer, so bound workspaces learn
  it. The connect link may carry the same form as its endpoint. A hint only.

Front configuration (example; the front holds only the outer certificate):

```caddyfile
hub.example.org {
    @link path /orch/link/1
    reverse_proxy @link 127.0.0.1:8107 {
        flush_interval -1
        transport http {
            read_timeout 120s       # idle WebSockets are cut by fronts at ~60 s: the link pings every 25 s
        }
    }
    respond 404                     # nothing else on this name
}
```

For nginx: `location = /orch/link/1 { proxy_pass http://127.0.0.1:8107; proxy_http_version 1.1; proxy_set_header Upgrade
$http_upgrade; proxy_set_header Connection "upgrade"; proxy_buffering off; proxy_read_timeout 120s; }` and no other
location. Do not route `/orch/link/1` for other names, and do not point any front at the link port itself (`--link-listen`).

A front that inspects or re-encrypts the inner TLS breaks the pin on purpose: the link stays down rather than fall back
(protocol section 10.10.2). With a company proxy in the middle, ask the IT team to let the hub's name through without
inspection. If a private network (VPN) reaches the hub, its addresses are ordinary `host:port` addresses and need nothing here.
