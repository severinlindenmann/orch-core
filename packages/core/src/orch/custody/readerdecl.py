"""``orch keys reader declare`` (orch-v2-host §0.8.6 item 9.2, 9.3, H12d-2): the signing path of a core's reader
declaration.

The declaring build runs the known-answer test over its **production** parsers at runtime (:func:`self_test`, the
``log`` surface of ``reader_kat_1``, shipped as package data next to :mod:`orch.identity.reader`) and declares the
highest level that passes, or level 0. The declaration is signed by the device key through its custody backend: the
passphrase prompt shows every field (:mod:`orch.custody.describe`); the convenience backend refuses (no review).

* ``declare_hosted``: the person's own device in a registered workspace (``via:"declare"``, column P): the host's
  ``reader.list`` names the device's ``dk_hash`` and person, ``reader.declare`` stores it.
* ``enrol``: an invitee's declaration (``via:"enrol"``) for the device certificate it hands the owner with
  ``member.add --reader-decl PATH``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orch import canon, crypto

__all__ = ["CORPUS", "build", "declare_hosted", "enrol", "self_test", "sign"]

CORPUS = Path(__file__).resolve().parents[1] / "identity" / "reader_kat_1.json"
KEY_ID = "dk"


def self_test(corpus_path: Path = CORPUS) -> tuple[int, dict[str, dict[str, str]]]:
    """``(level, kat)`` this build may declare: level 1 with its ``log`` digest when core's production parsers
    reproduce ``KAT_DIGESTS``, else level 0 and no digests (9.2: never a level whose KAT fails at runtime)."""
    from orch.identity import reader

    try:
        corpus = json.loads(Path(corpus_path).read_bytes())
        _, digest = reader.run_kat(corpus, "log")
    except Exception:  # noqa: BLE001 - a corpus that does not run is a failed self-test: level 0
        return 0, {}
    if digest != reader.KAT_DIGESTS["1"]["log"]:
        return 0, {}
    return 1, {"1": {"log": digest}}


def build(
    *,
    workspace_id: str,
    dk_sig_pub: bytes,
    dk_hash: str,
    person_hex: str,
    via: str,
    created_ms: int,
    level: int,
    kat: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    """The declaration object ``o`` (9.3)."""
    return {
        "v": 1,
        "kind": "reader_decl",
        "suite": crypto.SUITE_ID,
        "workspace_id": workspace_id,
        "device_id": crypto.device_id(dk_sig_pub).hex(),
        "dk_hash": dk_hash,
        "who": {"person_id": person_hex},
        "reader": level,
        "kat": {k: dict(v) for k, v in kat.items()},
        "bind": {"via": via},
        "created_ms": created_ms,
    }


def sign(backend: Any, o: dict[str, Any]) -> dict[str, Any]:
    """``{o, sig}``, signed by the device key under ``orch/v2/sig/reader-decl|`` through ``backend``."""
    from orch.identity.reader import LABEL

    sig = backend.sign(KEY_ID, LABEL + canon.cj_checked(o), action=f"declare reader level {o['reader']}")
    return {"o": o, "sig": crypto.b64u(sig)}


def declare_hosted(conn: Any, backend: Any, workspace_id: str, now_ms: int, idem: str) -> dict[str, Any]:
    """Declare this machine's device of a registered workspace: ``reader.list`` (its ``dk_hash`` and person),
    sign, ``reader.declare``. Returns the host's answer ``{device, reader, level, digest}``."""
    from orch.identity import device_ref

    pub = backend.public_key(KEY_ID)
    device = device_ref(pub)
    rows = conn.call("reader.list", {})["data"]["readers"]
    row = next((r for r in rows if r.get("device") == device), None)
    if row is None:
        raise LookupError(f"{device} is not a current device of this workspace")
    level, kat = self_test()
    o = build(
        workspace_id=workspace_id,
        dk_sig_pub=pub,
        dk_hash=row["dk_hash"],
        person_hex=row["person"][2:],
        via="declare",
        created_ms=now_ms,
        level=level,
        kat=kat,
    )
    return conn.call("reader.declare", {"reader_decl": sign(backend, o)}, idem=idem)["data"]


def enrol(backend: Any, workspace_id: str, cert: Any, now_ms: int) -> dict[str, Any]:
    """An invitee's declaration (``via:"enrol"``) for ``cert``, the device certificate ``member.add`` will carry;
    the certificate must be of the key ``backend`` holds."""
    o = cert.get("o") if isinstance(cert, dict) else None
    try:
        sig_pub = crypto.unb64u(o["dk_sig_pub"], crypto.PUB_LEN)
        kx_pub = crypto.unb64u(o["dk_kx_pub"], crypto.PUB_LEN)
        person_hex = o["person_id"]
    except (KeyError, TypeError, crypto.CryptoError, ValueError):
        raise ValueError("--cert is not a device certificate") from None
    if backend.public_key(KEY_ID) != sig_pub:
        raise ValueError("the device certificate is not of the device key on this machine")
    level, kat = self_test()
    return sign(
        backend,
        build(
            workspace_id=workspace_id,
            dk_sig_pub=sig_pub,
            dk_hash=crypto.b64u(crypto.dk_hash(sig_pub, kx_pub)),
            person_hex=person_hex,
            via="enrol",
            created_ms=now_ms,
            level=level,
            kat=kat,
        ),
    )
