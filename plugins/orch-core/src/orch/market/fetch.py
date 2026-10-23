"""Fetch the index of a marketplace (spec 5.1): resolve the host, judge every address, connect only to the first
checked one, fetch one commit with ``orch.gitsafe``'s closed operation, read ``marketplace.json`` from it in memory.
No lock is held here (E3)."""

from __future__ import annotations

import shutil
import tempfile

from orch import gitsafe

from . import MarketError, address, constants
from .url import host_of

INDEX_FILE = "marketplace.json"
RESOLVE_SECONDS = 10.0


def fetch_index(url: str, commit: str | None) -> tuple[str, bytes]:
    """``(commit id, bytes of marketplace.json)``. ``commit`` is a full object id, or ``None`` for the remote HEAD
    (its full id is returned and recorded by the caller). Refusals: ``market.address_refused`` (a refused address, or
    a redirect), ``market.fetch_failed``, ``market.commit_mismatch``."""
    host = host_of(url)
    addresses = address.resolve(host, RESOLVE_SECONDS)  # every address judged; one bad one refuses all of them
    staging = tempfile.mkdtemp(prefix="orch-market-")  # 0700, private; holds orch's own repository only
    try:
        got = gitsafe.market_fetch(url, commit, host, addresses[0], staging)
        data = gitsafe.market_read_blob(staging, got, INDEX_FILE, constants.MAX_INDEX_BYTES)
        return got, data
    except gitsafe.MarketGitError as exc:
        raise MarketError(
            {"redirect": "market.address_refused", "mismatch": "market.commit_mismatch"}.get(
                exc.kind, "market.fetch_failed"
            )
        ) from None
    except gitsafe.Unsafe:
        raise MarketError("market.fetch_failed") from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)
