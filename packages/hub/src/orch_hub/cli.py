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
    i.add_argument(
        "--real", action="store_true", help="a real hub (dev: false): real workspaces join it, dev ones are refused"
    )
    i.add_argument("--tls-spki-sha256", help="b64u SHA-256 of the hub TLS SubjectPublicKeyInfo")
    ks.add_parser("rotate-op", help="new HK_op under the next op_seq; reissues card and all certificates")
    ks.add_parser(
        "rotate-tls", help="new persistent TLS key and a re-issued card pinning it (also adds one to an old store)"
    )
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
    sv.add_argument("--listen", help="loopback host:port, e.g. 127.0.0.1:0 (or --link-listen, exactly one)")
    sv.add_argument(
        "--link-listen",
        help="network link bind: one literal private IP assigned to this machine, IP:port (refused on a machine with "
        "workspace host or agent users, see --dev-shared-machine)",
    )
    sv.add_argument(
        "--link-ws-listen",
        help="R6: a second listener for the link over WebSocket, IP:port: loopback (a TLS front on this machine) or one "
        "private address; plain HTTP, serves only /orch/link/1 (see docs/deployment.md, Link over 443)",
    )
    sv.add_argument(
        "--link-ws-public",
        help="R6: a public address of the WebSocket front to list in hub_where, wss://name[:port] (or ws://); a hint only",
    )
    sv.add_argument(
        "--link-ws-behind-front",
        action="store_true",
        help="R6: allow a non-private --link-ws-listen address; only when a TLS-terminating front is the only way to it",
    )
    sv.add_argument(
        "--dev-shared-machine",
        action="store_true",
        help="dev only: allow --link-listen on a machine that has host/agent users (needs a dev card; network link, not separation)",
    )
    sv.add_argument(
        "--bind-all",
        action="store_true",
        help="listen on every interface (0.0.0.0) instead of the one --link-listen/--web-listen address, which stays "
        "the advertised one (a computer cannot dial its own overlay IP)",
    )
    sv.add_argument(
        "--publish-url",
        required=True,
        help="orch-publish base URL (https, or http on loopback, private or 100.64/10 addresses)",
    )
    sv.add_argument("--api-host", help="host the workspaces sign (default: the host:port of --publish-url)")
    sv.add_argument("--invite", action="append", default=[], help="invite code accepted once (repeatable)")
    sv.add_argument(
        "--web-listen",
        help="host:port of the web API: loopback (the edge fronts it) or one private network address of this computer (dev, plain http); default: no web API",
    )
    sv.add_argument(
        "--relay-url",
        help="the relay for push (https, or http on loopback, private or 100.64/10 addresses); default: the card's",
    )
    sv.add_argument("--trusted-peer", action="append", default=[], help="edge address that may state the client (XFF)")
    sv.add_argument(
        "--behind-edge", action="store_true", help="the web API is served behind the edge (needs --trusted-peer)"
    )
    sv.add_argument("--web-origin", action="append", default=[], help="exact origin of the console (repeatable)")
    sv.add_argument("--api-origin", help="the public API origin the phone pairs with: one of the --web-origin values")
    sv.add_argument(
        "--hooks-listen",
        help="host:port of the public webhook listener (/h/<token> only; the edge fronts it). Needs --public-hooks-url; default: off",
    )
    sv.add_argument(
        "--public-hooks-url",
        help="the origin senders use for webhooks, e.g. https://hub.example.com (https; http only for a dev hub). Needs --hooks-listen",
    )
    ins = sub.add_parser(
        "inspect", help="dev only: read-only JSON view of the registry (pins, mode, binding, op counts)"
    )
    ins.add_argument("--state", help="hub state dir (default: $ORCH_HUB_STATE)")
    ins.add_argument("--workspace", help="only this workspace id")
    ins.add_argument("--json", action="store_true", required=True, help="print JSON (the only format)")
    rl = sub.add_parser(
        "releases", help="dev: the links and apps of the bound full-mode workspaces, read at orch-publish (admin read)"
    )
    rl.add_argument("--state", help="hub state dir (default: $ORCH_HUB_STATE)")
    rl.add_argument(
        "--publish-url",
        required=True,
        help="orch-publish base URL (https, or http on loopback, private or 100.64/10 addresses)",
    )
    rl.add_argument("--workspace", help="only this workspace id (32 lower-case hex)")
    rl.add_argument("--json", action="store_true", required=True, help="print JSON (the only format)")
    dr = sub.add_parser(
        "doctor", help="dev: check the state dir, card, registry, TLS pin, publish route and the co-location rules (4a)"
    )
    dr.add_argument("--state", help="hub state dir (default: $ORCH_HUB_STATE)")
    dr.add_argument("--listen", help="the running hub's loopback host:port: its TLS key must match the card pin")
    dr.add_argument("--publish-url", help="orch-publish base URL: its /v2/hub/card route must answer")
    dr.add_argument("--link-listen", help="the running hub's network link IP:port (split dev layout): pin + bind rules")
    dr.add_argument("--dev-shared-machine", action="store_true", help="the hub runs with this dev exception (WARN)")
    dr.add_argument(
        "--skip-link-dial",
        action="store_true",
        help="on the hub machine: compare the stored key with the card, do not dial",
    )
    dr.add_argument(
        "--hub-user", default="orch-hub", help="the hub's OS user (default orch-hub; split hub: orch-hub-split)"
    )
    dr.add_argument("--fw-dump", help="dump of the loaded hub table (default /run/orch/hub-fw.loaded.json)")
    dr.add_argument("--link-iface", default="wt0", help="the only interface the network link port accepts on")
    dr.add_argument(
        "--layout", choices=("auto", "one-vps", "split"), default="auto", help="assert the layout (default: infer)"
    )
    dr.add_argument("--host-user", action="append", default=[], help="a workspace host OS user not named orch-host-*")
    dr.add_argument("--agent-user", action="append", default=[], help="an agent OS user not named agent-*")
    dr.add_argument("--fw-file", help="the link firewall file (default: /etc/orch/hub-fw.nft)")
    rg = sub.add_parser("relay", help="dev: the hub's account at its relay (card from the key store)")
    rg.add_argument("--state", help="hub state dir (default: $ORCH_HUB_STATE)")
    rg.add_argument(
        "--relay-url", help="override the card's relay_url (https, or http on loopback, private or 100.64/10 addresses)"
    )
    rgs = rg.add_subparsers(dest="cmd", required=True)
    rgs.add_parser("register", help="register or rotate the account by hub card (POST /v2/hubs, unsigned)")
    from .backup.cli import add_parser as add_backup_parser
    from .backup.cli import add_restore_parser

    add_backup_parser(sub)
    add_restore_parser(sub)
    from .access.cli import add_parsers

    add_parsers(sub)
    hp = sub.add_parser(
        "helper",
        help="run the service helper (start/stop/restart this machine's parts for the console; unix socket only)",
    )
    hp.add_argument("--state", help="hub state dir (default: $ORCH_HUB_STATE)")
    hp.add_argument("--print-sudoers", action="store_true", help="print the sudoers lines to install, then exit")
    hp.add_argument("--user", help="with --print-sudoers: the user the helper runs as")
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


def _helper(args: argparse.Namespace) -> int:
    from .services.helper_backends import load_config, sudoers_text
    from .services.helper_proto import HelperError
    from .services.helper_server import run_helper

    state = Path(args.state) if args.state else default_state_dir()
    if not args.print_sudoers:
        return run_helper(state)
    try:
        if not args.user:
            raise HelperError("bad_config", "--user is needed")
        print(sudoers_text(args.user, list(load_config(state).parts.values())), end="")
    except HelperError as e:
        print(f"orch-hub: refused: {e}", file=sys.stderr)
        return 1
    return 0


#: #1463: while a removal runs or stalls only these command groups work (an ALLOW list: a command added later is refused by
#: default). ``serve`` must run (it resumes the removal); the others only read.
_CLOSING_ALLOWED = frozenset({"serve", "helper", "doctor", "inspect", "releases"})


def _closing_refused(args: argparse.Namespace) -> bool:
    if args.group in _CLOSING_ALLOWED:
        return False
    if args.group == "keys" and args.cmd == "card" and args.relay_url is None and args.tls_spki_sha256 is None:
        return False  # printing the card writes nothing
    try:
        state = Path(args.state) if getattr(args, "state", None) else default_state_dir()
    except KeysError:
        return False
    return (state / "hub-removal.json").exists()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if _closing_refused(args):
        print("orch-hub: refused: hub_closing (this hub is being removed)", file=sys.stderr)
        return 1
    if args.group == "helper":
        return _helper(args)
    if args.group == "serve":
        from .serve import serve

        try:
            return serve(
                Path(args.state) if args.state else default_state_dir(),
                args.listen,
                args.publish_url,
                args.invite,
                link_listen=args.link_listen,
                dev_shared_machine=args.dev_shared_machine,
                bind_all=args.bind_all,
                link_ws_listen=args.link_ws_listen,
                link_ws_public=args.link_ws_public,
                link_ws_behind_front=args.link_ws_behind_front,
                api_host=args.api_host,
                web_listen=args.web_listen,
                relay_url=args.relay_url,
                trusted_peers=frozenset(args.trusted_peer),
                behind_edge=args.behind_edge,
                web_origins=frozenset(args.web_origin),
                api_origin=args.api_origin,
                hooks_listen=args.hooks_listen,
                public_hooks_url=args.public_hooks_url,
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
    if args.group == "backup-key":
        from .backup.cli import run as run_backup_key

        return run_backup_key(args)
    if args.group == "restore":
        from .backup.cli import run_restore

        return run_restore(args)
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
        kw = {"fw_file": Path(args.fw_file)} if args.fw_file else {}
        if args.fw_dump:
            kw["fw_dump"] = Path(args.fw_dump)
        return (
            1
            if run(
                state,
                listen=args.listen,
                link_listen=args.link_listen,
                dev_shared_machine=args.dev_shared_machine,
                hub_user=args.hub_user,
                link_iface=args.link_iface,
                skip_link_dial=args.skip_link_dial,
                publish_url=args.publish_url,
                out=sys.stdout,
                layout_flag=args.layout,
                host_users=args.host_user,
                agent_users=args.agent_user,
                **kw,
            )
            else 0
        )
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
            keys = HubKeys.init(state, relay_url=args.relay_url, tls_spki_sha256=args.tls_spki_sha256, real=args.real)
            out = keys.card()
        else:
            keys = HubKeys.open(state)
            if args.cmd == "rotate-op":
                out = keys.rotate_op()
            elif args.cmd == "rotate-tls":
                out = keys.rotate_tls()
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
        rows = client.published(args.workspace)
    except (ValueError, KeysError) as e:
        print(f"orch-hub: refused: {e}", file=sys.stderr)
        return 1
    except AdminReadError as e:
        print(f"orch-hub: refused: {e.code}", file=sys.stderr)
        return 1
    finally:
        if reg is not None:
            reg.close()
    print(dumps({"published": rows}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
