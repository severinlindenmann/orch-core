"""``orch-hub`` command line. Only public objects are ever printed; private keys never leave the state dir."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

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
    return p


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
            )
        except (ValueError, KeysError) as e:
            print(f"orch-hub: refused: {e}", file=sys.stderr)
            return 1
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


if __name__ == "__main__":
    raise SystemExit(main())
