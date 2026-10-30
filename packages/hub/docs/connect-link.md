# Workspace connect link (run-9 feedback 6)

Contract for orch-core (`hub join --link`). Code: `src/orch_hub/connect.py` (`build_link`, `parse_link`).

## Link format

```
<origin>/c#h1.<hub_id>.<code>[.<endpoint>]
```

- `<origin>`: `http(s)://host[:port]`, the hub web console origin (no domain required: `http://10.0.0.5:8443` is fine).
  Path is exactly `/c`, no query. The page at `/c` is a static landing text; the fragment never reaches the server.
- `h1`: literal version tag. `hub_id`: 32 lowercase hex (the hub id of protocol section 2.3).
- `code`: single-use invite, `[A-Za-z0-9_-]{1,64}` (no dots). Valid 30 minutes.
- `endpoint` (optional): where the host dials the hub link: `host:port`, `[v6]:port` or the word `local`.
  It may contain dots, so split the fragment with at most 3 splits (`fragment.split(".", 3)`).

Test vector:

```
link:  https://hub.example.test:8443/c#h1.0123456789abcdef0123456789abcdef.Ab3_x-9Zq.10.0.0.5:8105
parse: origin   = https://hub.example.test:8443
       hub_id   = 0123456789abcdef0123456789abcdef
       code     = Ab3_x-9Zq
       endpoint = 10.0.0.5:8105
       card_url = https://hub.example.test:8443/api/v1/hub/card
```

Without endpoint: `http://h/c#h1.<hub_id>.abc` parses with `endpoint = None`. Anything else (other path,
other version, short `hub_id`, bad endpoint) is refused. (Test: `tests/test_connect_link.py`.)

## Card endpoint

`GET <origin>/api/v1/hub/card`, no auth, no cookie. Answer `200 {"card": <signed hub card>, "api_origin": ...}`.
The hub's `api_origin` must be configured (the loopback dev console sets it itself), else `503 unavailable`.

Workspace side: parse the link, GET the card, run `verify_card`, require `card.o.hub_id == link.hub_id` (else refuse,
`other_hub`), pin `hk_root_pub` and `tls_spki_sha256` from the verified card, dial `endpoint`, send `join` with
`invite = code` (the hub's `check`/`consume`: unknown, used or expired code is `bad_invite`).

## Minting (hub side)

`POST /api/v1/admin/connect` (hub-admin session, body optional `{"endpoint": "host:port"}`, default: the hub's own
link address) answers `{link, code, hub_id, endpoint, expires_ms}`. The base origin is the console origin the admin
used. Console: Admin, Workspaces tab, "Connect a workspace".
A workspace on a new server: Workspaces, "New workspace on a server" is a guide only (install, `orch setup --no-browser`, the
`ssh -o ExitOnForwardFailure=yes -L 127.0.0.1:47821:127.0.0.1:47821 user@server` tunnel, then this connect link). The hub never
connects to the server and never sees its keys or the setup link.
