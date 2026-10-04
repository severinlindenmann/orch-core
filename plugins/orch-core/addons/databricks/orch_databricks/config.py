"""Settings of the databricks addon (spec A1 §7.3, v2 §15). The addon never picks a profile on its own:
env -> profile @ host is a human settings save, one `map` line per env."""
from __future__ import annotations

import re
from dataclasses import dataclass

ENV_NAME = re.compile(r"[a-z][a-z0-9_-]{0,19}")
PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
FIXTURE = re.compile(r"[a-z][a-z0-9-]{0,39}")
HOST = re.compile(r"https://[a-z0-9.-]+(?::\d+)?")
RESERVED_PROFILES = frozenset({"default", "__settings__"})
MAX_ENVS = 10
SCOPES = ("mine", "prefix", "all")


@dataclass(frozen=True)
class EnvSpec:
    name: str
    profile: str | None = None
    host: str | None = None
    simulated: str | None = None
    problem: str | None = None

    @property
    def live(self) -> bool:
        return self.problem is None and self.simulated is None


def normalize_host(value) -> str:
    return str(value or "").strip().lower().rstrip("/")


def _one(name: str, value: str) -> EnvSpec:
    if not ENV_NAME.fullmatch(name):
        return EnvSpec(name or "?", problem="environment names are short lowercase words such as dev, int or prod")
    if value.startswith("simulated:"):
        fixture = value.removeprefix("simulated:").strip()
        if not FIXTURE.fullmatch(fixture):
            return EnvSpec(name, problem=f"{name}: simulated:<name> needs a fixture name such as int")
        return EnvSpec(name, simulated=fixture)
    profile, sep, host = (part.strip() for part in value.partition("@"))
    if profile.lower() in RESERVED_PROFILES:  # before the name check: __settings__ is not a valid profile name
        return EnvSpec(name, profile,
                       problem=f"{name}: the {profile} profile is never used; name a profile from ~/.databrickscfg")
    if not PROFILE.fullmatch(profile):
        return EnvSpec(name, problem=f"{name}: write {name} = <profile> @ https://<workspace host>")
    host = normalize_host(host)
    if not sep or not HOST.fullmatch(host):
        return EnvSpec(name, profile,
                       problem=f"{name}: pin the workspace host: {name} = {profile} @ https://<workspace host>")
    return EnvSpec(name, profile, host)


def parse_envs(settings) -> list[EnvSpec]:
    raw = settings.get("envs") if isinstance(settings, dict) else None
    if not isinstance(raw, dict):
        return []
    return [_one(str(name).strip().lower(), str(value).strip()) for name, value in list(raw.items())[:MAX_ENVS]]


def env_by_name(settings, name: str) -> EnvSpec | None:
    return next((e for e in parse_envs(settings) if e.name == name), None)


def scope_mode(settings) -> str:
    value = settings.get("scope") if isinstance(settings, dict) else None
    return value if value in SCOPES else "mine"


def prefixes(settings) -> tuple[str, ...]:
    raw = str((settings or {}).get("prefixes") or "")
    return tuple(p.strip() for p in raw.split(",") if p.strip())[:10]


def owned(item: dict, mode: str, me: str | None, names: tuple[str, ...]) -> bool:
    """Ownership filter for shared workspaces (spec A1 §7.3): mine = created or run by the signed-in user,
    prefix = the name starts with a configured prefix, all = everything."""
    if mode == "all":
        return True
    if mode == "prefix":
        return any(str(item.get("name") or "").startswith(p) for p in names)
    owners = {str(o).lower() for o in (item.get("owners") or []) if o}
    return bool(me) and me.lower() in owners
