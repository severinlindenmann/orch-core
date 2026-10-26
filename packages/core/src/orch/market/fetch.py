"""Fetch from a marketplace (spec 5.1): resolve the host, judge every address, connect only to the first checked one,
fetch one commit with ``orch.gitsafe``'s closed operation, then read ``marketplace.json`` (:func:`fetch_index`) or an
add-on's package (:func:`fetch_package`, V3 G3) from it in memory. No lock is held here (E3)."""

from __future__ import annotations

import shutil
import tempfile
from typing import Any

from orch import gitsafe
from orch.addons import package

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


def fetch_package(entry: dict[str, Any]) -> package.Package:
    """Steps 11 and 12 of spec 5 for an index ``entry`` (already checked by the index rules): resolve the entry's
    host, judge every address, fetch exactly ``entry["commit"]`` into a fresh private directory (the pack has a cap
    of its own), export the package at ``source.subdir`` in memory and read it once under the 8.1 rules. The git
    directory is deleted before this returns, on every path; the package exists only in the returned object.
    Refusals: ``market.address_refused``, ``market.fetch_failed``, ``market.commit_mismatch``,
    ``market.package_invalid``. The digest is NOT compared here (step 13 is the caller's, on these bytes)."""
    url, commit = entry["source"]["git"], entry["commit"]
    host = host_of(url)
    addresses = address.resolve(host, RESOLVE_SECONDS)
    staging = tempfile.mkdtemp(prefix="orch-market-")
    try:
        gitsafe.market_fetch(url, commit, host, addresses[0], staging, cap=gitsafe.MARKET_PACKAGE_PACK)
        files = gitsafe.market_export_package(staging, commit, entry["source"].get("subdir"))
        return package.from_files(files)
    except gitsafe.MarketGitError as exc:
        raise MarketError(
            {
                "redirect": "market.address_refused",
                "mismatch": "market.commit_mismatch",
                "package": "market.package_invalid",
            }.get(exc.kind, "market.fetch_failed")
        ) from None
    except package.PackageError:
        raise MarketError("market.package_invalid") from None
    except gitsafe.Unsafe:
        raise MarketError("market.fetch_failed") from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)
