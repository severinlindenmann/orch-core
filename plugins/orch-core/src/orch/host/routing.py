"""Model routing by an add-on (design G6 section 4). Core keeps the gate; the add-on only proposes.

An agent start with no explicit model, a mode and a harness of the closed registry asks the ONE active add-on that
holds ``choose_model`` which model to use. The input is exactly ``{harness, mode, settings}``: no ticket key, text or
field and no person, so an agent (who writes tickets) cannot steer the answer; the settings come from the replayed,
owner-signed log (never from config.json) and the code is the granted bytes. The answer is exactly
``{"model": str | null}`` and the string must be one of :data:`orch.modelchoices.MODEL_CHOICES` for the harness, else
the start is blocked: nothing is guessed and nothing the add-on says reaches an argv.

Every failure (no answer in time, a crash, a bad answer, a package that is not the granted one, two add-ons) is the
single ``launch.invalid`` of the caller; no ``addon.*`` code leaves this module. Answers are memoised on
``(package digest, settings digest, harness, mode)`` so the preview and the start agree and the add-on runs once.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from orch import canon
from orch.modelchoices import MODEL_CHOICES

if TYPE_CHECKING:
    from .workspace import WorkspaceService

__all__ = ["CAPABILITY", "METHOD", "TIMEOUT", "UNANSWERED", "Routed", "choose"]

LOG = logging.getLogger(__name__)
CAPABILITY = "choose_model"
METHOD = "launch.model"
TIMEOUT = 3.0
UNANSWERED = "Model routing did not answer: pick a model or turn it off"
_TOO_MANY = "More than one add-on chooses models: turn all but one off, or pick a model"
_MEMO_MAX = 64
FAIL_SECONDS = 30.0


@dataclass(frozen=True)
class Routed:
    """``model`` the add-on chose (``None``: the harness default), ``by`` the add-on's SIGNED name (the only word the
    review shows; a package's own title is not signed), or a ``problem`` that blocks the start."""

    model: str | None = None
    by: str | None = None
    problem: str | None = None


_memo: dict[tuple[str, str, str, str], str | None] = {}
_failed: dict[tuple[str, str, str, str], float] = {}  # key -> monotonic time until which a failure stands
_lock = threading.Lock()


def _fail(key: tuple[str, str, str, str]) -> None:
    with _lock:
        if len(_failed) >= _MEMO_MAX:
            _failed.pop(next(iter(_failed)))
        _failed[key] = time.monotonic() + FAIL_SECONDS


def _remember(key: tuple[str, str, str, str], model: str | None) -> None:
    with _lock:
        if len(_memo) >= _MEMO_MAX:
            _memo.pop(next(iter(_memo)))
        _memo[key] = model


def choose(service: WorkspaceService, harness: str, mode: str) -> Routed:
    """What the active ``choose_model`` add-on picks for ``harness`` in ``mode``. Call it outside the store lock.
    Whatever goes wrong, at any step, is the one ``launch.invalid`` problem of the caller."""
    try:
        return _choose(service, harness, mode)
    except Exception:  # noqa: BLE001  (fail closed: the start is blocked, nothing is guessed)
        LOG.exception("model routing failed before or around the add-on call")
        return Routed(problem=UNANSWERED)


def _choose(service: WorkspaceService, harness: str, mode: str) -> Routed:
    from orch.addons import install, runner
    from orch.addons.manifest import ManifestError
    from orch.addons.package import PackageError
    from orch.store.render import thaw

    if harness not in MODEL_CHOICES:
        return Routed()
    ws = service.store.state.workspace
    try:
        everyone = install.as_addons(ws.addons).values()
        active = [a for a in everyone if a.enabled and not a.purged and CAPABILITY in a.capabilities]
    except (AttributeError, KeyError, TypeError):
        return Routed(problem=UNANSWERED)
    if not active:
        return Routed()
    if len(active) > 1:
        return Routed(problem=_TOO_MANY)
    granted = active[0]
    try:
        package, manifest = install.read_installed(service.store.layout.store, granted.name)
    except (PackageError, ManifestError, OSError):
        return Routed(problem=UNANSWERED)
    if package.digest != granted.package_sha256:
        return Routed(problem=UNANSWERED)  # the installed code is not the granted one: never run
    from orch.addons.settings_value import KEY

    held = ws.settings.get(KEY)
    settings = thaw(held.get(granted.name)) if held is not None and granted.name in held else None
    try:
        key = (granted.package_sha256, hashlib.sha256(canon.dumps(settings)).hexdigest(), harness, mode)
    except (ValueError, TypeError, RecursionError):
        return Routed(problem=UNANSWERED)
    now = time.monotonic()
    with _lock:
        hit = key in _memo
        cached = _memo.get(key)
        failed = _failed.get(key, 0.0) > now
    if hit:
        return Routed(cached, granted.name)
    if failed:  # a failed answer is not asked again for a short while: a hung add-on holds no worker per preview
        return Routed(problem=UNANSWERED)
    try:
        answer: Any = runner.call(
            package,
            granted,
            METHOD,
            {"harness": harness, "mode": mode, "settings": settings},
            runner.Limits(timeout=TIMEOUT),
        )
    except runner.RunnerError as exc:
        LOG.warning("model routing add-on %s: %s", granted.name, exc.code)
        _fail(key)
        return Routed(problem=UNANSWERED)
    except Exception:  # noqa: BLE001  (fail closed: whatever went wrong, the start is blocked)
        LOG.exception("model routing add-on %s failed", granted.name)
        _fail(key)
        return Routed(problem=UNANSWERED)
    model = _checked(answer, harness)
    if model is False:
        _fail(key)
        return Routed(problem="Model routing gave a model orch does not offer here: pick a model or turn it off")
    _remember(key, model)  # type: ignore[arg-type]
    return Routed(model, granted.name)  # type: ignore[arg-type]


def _checked(answer: Any, harness: str) -> str | None | bool:
    """The model of a valid answer, ``None`` for "no preference", ``False`` for anything else."""
    if type(answer) is not dict or set(answer) != {"model"}:
        return False
    model = answer["model"]
    if model is None:
        return None
    if type(model) is str and model in MODEL_CHOICES[harness]:
        return model
    return False
