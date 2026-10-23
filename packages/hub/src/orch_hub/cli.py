"""``orch-hub`` command line. Only public objects are ever printed; private keys never leave the state dir."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import dev_of
from .contract.canon import CanonError, unb64u
from .contract.hub import FACTORS
from .keys import HubKeys, KeysError, default_state_dir
from .keys.store import dumps


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orch-hub")
    sub = p.add_subparsers(dest="group", required=True)
    k = sub.add_parser("keys", help="hub key custody and issuing (dev only in V3)")
    k.add_argument("--state", help="hub state dir (default: $ORCH_HUB_STATE)")
    ks = k.add_subparsers(dest="cmd", required=True)
    i = ks.add_parser("init", help="create HK_root, HK_op, HXK and the first card")
    i.add_argument("--relay-url")
    i.add_argument("--tls-spki-sha256", help="b64u SHA-256 of the hub TLS SubjectPublicKeyInfo")
    ks.add_parser("rotate-op", help="new HK_op under the next op_seq; reissues card and all certificates")
    r = ks.add_parser("revoke-op", help="root-signed revocation of an op certificate (default: the current one)")
    r.add_argument("--cert-id")
    c = ks.add_parser("card", help="print the current card; with options, reissue it (card_seq + 1)")
    c.add_argument("--relay-url")
    c.add_argument("--tls-spki-sha256")
    u = ks.add_parser("issue-uk", help="issue a UK certificate for <user> (a user id, or 'new')")
    u.add_argument("user")
    ch = ks.add_parser("issue-channel", help="certify a channel key for an existing user")
    ch.add_argument("--user", required=True)
    ch.add_argument("--key-pub", required=True, help="b64u 65-byte P-256 public key")
    ch.add_argument("--factor", required=True, choices=FACTORS)
    sv = sub.add_parser("serve", help="run the dev hub: link server + publish forwarding (loopback only)")
    sv.add_argument("--state", help="hub state dir (default: $ORCH_HUB_STATE)")
    sv.add_argument("--listen", required=True, help="loopback host:port, e.g. 127.0.0.1:0")
    sv.add_argument("--publish-url", required=True, help="orch-publish base URL (https, or http on loopback)")
    sv.add_argument("--api-host", help="host the workspaces sign (default: the host:port of --publish-url)")
    sv.add_argument("--invite", action="append", default=[], help="invite code accepted once (repeatable)")
    sv.add_argument("--web-listen", help="loopback host:port of the web API (the edge fronts it); default: no web API")
    sv.add_argument("--relay-url", help="the relay for push (https, or http on loopback); default: the card's")
    sv.add_argument("--trusted-peer", action="append", default=[], help="edge address that may state the client (XFF)")
    sv.add_argument(
        "--behind-edge", action="store_true", help="the web API is served behind the edge (needs --trusted-peer)"
    )
    sv.add_argument("--web-origin", action="append", default=[], help="exact origin of the console (repeatable)")
    sv.add_argument("--api-origin", help="the public API origin the phone pairs with: one of the --web-origin values")
    ins = sub.add_parser(
        "inspect", help="dev only: read-only JSON view of the registry (pins, mode, binding, op counts)"
    )
    ins.add_argument("--state", help="hub state dir (default: $ORCH_HUB_STATE)")
    ins.add_argument("--workspace", help="only this workspace id")
    ins.add_argument("--json", action="store_true", required=True, help="print JSON (the only format)")
    rl = sub.add_parser(
        "releases", help="dev: release metadata of the bound full-mode workspaces, read at orch-publish (admin read)"
    )
    rl.add_argument("--state", help="hub state dir (default: $ORCH_HUB_STATE)")
    rl.add_argument("--publish-url", required=True, help="orch-publish base URL (https, or http on loopback)")
    rl.add_argument("--workspace", help="only this workspace id (32 lower-case hex)")
    rl.add_argument("--json", action="store_true", required=True, help="print JSON (the only format)")
    dr = sub.add_parser("doctor", help="dev: check the state dir, card, registry, TLS pin and publish route")
    dr.add_argument("--state", help="hub state dir (default: $ORCH_HUB_STATE)")
    dr.add_argument("--listen", help="the running hub's loopback host:port: its TLS key must match the card pin")
    dr.add_argument("--publish-url", help="orch-publish base URL: its /v2/hub/card route must answer")
    rg = sub.add_parser("relay", help="dev: the hub's account at its relay (card from the key store)")
    rg.add_argument("--state", help="hub state dir (default: $ORCH_HUB_STATE)")
    rg.add_argument("--relay-url", help="override the card's relay_url (https, or http on loopback)")
    rgs = rg.add_subparsers(dest="cmd", required=True)
    rgs.add_parser("register", help="register or rotate the account by hub card (POST /v2/hubs, unsigned)")
    from .access.cli import add_parsers

    add_parsers(sub)
    from .spaces.cli import add_parsers as add_space_parsers

    add_space_parsers(sub)
    return p


def _relay_register(args: argparse.Namespace) -> int:
    from .relay import RelayClient, RelayError

    try:
        keys = HubKeys.open(Path(args.state) if args.state else default_state_dir())
        print(dumps(RelayClient(keys, args.relay_url, dev=dev_of(keys)).register()))
    except (KeysError, RelayError) as e:
        print(f"orch-hub: refused: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.group == "serve":
        from .serve import serve

        try:
            return serve(
                Path(args.state) if args.state else default_state_dir(),
                args.listen,
                args.publish_url,
                args.invite,
                api_host=args.api_host,
                web_listen=args.web_listen,
                relay_url=args.relay_url,
                trusted_peers=frozenset(args.trusted_peer),
                behind_edge=args.behind_edge,
                web_origins=frozenset(args.web_origin),
                api_origin=args.api_origin,
            )
        except (ValueError, KeysError) as e:
            print(f"orch-hub: refused: {e}", file=sys.stderr)
            return 1
    if args.group in ("users", "groups", "grants", "channels", "access"):
        from .access.cli import run as run_access

        return run_access(args)
    if args.group == "spaces":
        from .spaces.cli import run as run_spaces

        return run_spaces(args)
    if args.group == "relay":
        return _relay_register(args)
    if args.group == "releases":
        return _releases(args)
    if args.group == "doctor":
        from .doctor import run

        try:
            state = Path(args.state) if args.state else default_state_dir()
        except KeysError as e:
            print(f"orch-hub: {e}", file=sys.stderr)
            return 1
        return 1 if run(state, listen=args.listen, publish_url=args.publish_url, out=sys.stdout) else 0
    if args.group == "inspect":
        from .inspect import InspectError, inspect

        try:
            print(dumps(inspect(Path(args.state) if args.state else default_state_dir(), args.workspace)))
        except InspectError as e:
            print(f"orch-hub: {e}", file=sys.stderr)
            return 1
        return 0
    try:
        state = Path(args.state) if args.state else default_state_dir()
        if args.cmd == "init":
            keys = HubKeys.init(state, relay_url=args.relay_url, tls_spki_sha256=args.tls_spki_sha256)
            out = keys.card()
        else:
            keys = HubKeys.open(state)
            if args.cmd == "rotate-op":
                out = keys.rotate_op()
            elif args.cmd == "revoke-op":
                out = keys.revoke_op(args.cert_id)
            elif args.cmd == "card":
                if args.relay_url is None and args.tls_spki_sha256 is None:
                    out = keys.card()
                else:
                    kw = {} if args.relay_url is None else {"relay_url": args.relay_url}
                    out = keys.reissue_card(tls_spki_sha256=args.tls_spki_sha256, **kw)
            elif args.cmd == "issue-uk":
                out = keys.issue_uk(None if args.user == "new" else args.user)
            else:
                try:
                    pub = unb64u(args.key_pub, 65)
                except CanonError:
                    raise KeysError("malformed", "key-pub is not canonical b64u") from None
                out = keys.issue_channel(args.user, pub, args.factor)
    except KeysError as e:
        print(f"orch-hub: refused: {e.code}" + (f" ({e})" if str(e) != e.code else ""), file=sys.stderr)
        return 1
    print(dumps(out))
    return 0


def _releases(args: argparse.Namespace) -> int:
    from .link.registry import Registry
    from .publish import AdminReadClient, AdminReadError

    reg = None
    try:
        state = Path(args.state) if args.state else default_state_dir()
        keys = HubKeys.open(state)
        client = AdminReadClient(args.publish_url, keys.op_signer, keys.hub_id)
        if args.workspace is not None:
            client.target(args.workspace)  # canonical or malformed
            reg = Registry(str(state / "registry.db"))
            if reg.mode(args.workspace) != "full":  # the admin read covers bound full-mode workspaces only (§10.6)
                print("orch-hub: refused: not_bound", file=sys.stderr)
                return 1
        rows = client.releases(args.workspace)
    except (ValueError, KeysError) as e:
        print(f"orch-hub: refused: {e}", file=sys.stderr)
        return 1
    except AdminReadError as e:
        print(f"orch-hub: refused: {e.code}", file=sys.stderr)
        return 1
    finally:
        if reg is not None:
            reg.close()
    print(dumps({"releases": rows}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
