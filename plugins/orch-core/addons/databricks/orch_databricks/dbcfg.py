"""~/.databrickscfg, read locally before every CLI call (spec v2 §15: host pinning). Never written."""
from __future__ import annotations

import configparser
from pathlib import Path

from .config import RESERVED_PROFILES, normalize_host


def config_path() -> Path:
    return Path.home() / ".databrickscfg"


def profiles(path: Path | None = None) -> dict[str, str]:
    """{profile: normalized host} for every profile section except DEFAULT and __settings__; {} when unreadable."""
    # A default_section nobody uses, so [DEFAULT] is an ordinary section that we can skip by name.
    parser = configparser.ConfigParser(interpolation=None, strict=False, default_section="\x00orch-none")
    try:
        parser.read_string((path or config_path()).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, configparser.Error):
        return {}
    return {name: normalize_host(parser.get(name, "host", fallback=""))
            for name in parser.sections() if name.lower() not in RESERVED_PROFILES}


def host_problem(profile: str, pinned_host: str, path: Path | None = None) -> str | None:
    found = profiles(path)
    if profile not in found:
        return (f"profile {profile} is not in ~/.databrickscfg; add it with "
                f"databricks auth login --host {pinned_host} --profile {profile}")
    if found[profile] != pinned_host:
        return (f"profile {profile} now points to {found[profile] or 'no host'}, not the saved host {pinned_host}; "
                "check the profile or update the environment in Workspace & addons")
    return None
