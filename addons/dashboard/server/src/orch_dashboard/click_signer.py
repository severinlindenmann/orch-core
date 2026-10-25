"""Click-to-sign (H5c, orch-v2-host section 0.8.6 item 5): the one signer child of this server, and nothing else.

**The key never comes here.** The signer is core's own child process (`python -I -m orch.click`): it holds the device
key, talks to the host itself and builds the View. This module only starts it, restarts it when it exits, speaks its
frames (through `orch.click.client.ClickClient`) and turns its refusals into the server's errors. It never builds a
preview of its own, never logs a View or a credential, and is not reachable from any addon path (only the typed
routes of `click.py` call it).

Trust (binding): the dashboard, its HTTP process and its browser, is trusted with signing authority; the signer keeps
the key out of this process against passive disclosure. The card and every sign panel say so.
"""

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .errors import HostError, fail

CONTRACTS = (3,)  # 3: click-only confirmation (no typed word); the signer hands a runner grant to the runner
#: `ORCH_DASHBOARD_CLICK=off` never starts a signer (a kill switch; the tests use it unless they test the signer).
SWITCH = "ORCH_DASHBOARD_CLICK"
RESTART_AFTER = 1.0  # seconds: a signer that dies at once is not respawned in a tight loop
CONTRACT_RETRY = 30.0
TIMEOUT = {
    "status": 8.0,
    "prepare": 30.0,
    "view": 30.0,
    "sign": 60.0,
    "cancel": 30.0,
    "stash": 30.0,
    "custody_on": 60.0,
    "custody_off": 60.0,
    "runner": 120.0,  # on installs the runner release and service, as `orch runner enable` does
    "github_token": 30.0,  # a sealed GitHub token is opened and written: memory and one small file
    "rotate": 120.0,  # the recovery code is stretched (scrypt) and the host signs the rotation
    "stepup": 8.0,  # a passkey step-up nonce: memory only
}
#: the signer can switch click-to-sign on and off (`custody_on`/`custody_off`, section 0.8.6 item 3a)
CUSTODY_FEATURE = "custody"
IMAGES_FEATURE = "images"  # `stash` and `prepare {sequence}` naming pasted screenshots (core R3 part B)
REVEAL_FEATURE = "reveal"  # `prepare {reveal_key}` and `sign`'s sealed `reveal` of a grant secret (core C7)
BACKUP_KEY_FEATURE = "backup_key"  # `sign {sealed}`: the hub steps by click with the sealed backup key (core C10)
SEALED_PASSPHRASE_FEATURE = (
    "sealed_passphrase"  # `custody_off {begin}`, then `{sealed}`: the new passphrase sealed (core C12)
)
ROTATE_FEATURE = "device_rotate"  # `rotate {begin}`, then `{sealed}`: a new device key for this computer (core C13)
GITHUB_FEATURE = (
    "github_token"  # `github_token {begin}`, then `{sealed}`: the GitHub token sealed to the signer (core C16)
)
RUNNER_FEATURE = "runner_switch"  # `runner {review}`, then `runner {on, view_digest}`: dashboard execution (core C14)
STEPUP_FEATURE = "passkey_stepup"  # `stepup` nonces and the `remote`/`stepup` arguments of the listed ops (core R1-6)
SEQUENCE_FEATURE = "sequence"  # `prepare {sequence}`: one review, N signatures (core batch 11)


def child_env(state: Path) -> dict[str, str]:
    """The signer's environment: ours, with the state directory made explicit (it must be the registry we read) and
    no PYTHONPATH (`-I` ignores it, and a path to another core is never part of a running server)."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["ORCH_STATE_DIR"] = str(state)
    return env


def unavailable(why: str = "The signing helper is not running. Sign in the terminal instead.") -> HostError:
    return fail("click.unavailable", why, exit=9)


class Signer:
    """One signer child per dashboard server, started on first use, restarted if it exits, stopped on shutdown.

    `start` is a seam for tests (default `orch.click.spawn.start`); everything it returns must have `call(op, args,
    timeout=)`, `close()` and an optional `proc` with `poll()`."""

    def __init__(self, state: Path, start: Callable | None = None, clock: Callable[[], float] = time.monotonic):
        self.state = Path(state)
        self._start, self.clock = start, clock
        self.lock = threading.RLock()  # the lifecycle (start, restart, stop): short
        self.chan = threading.Lock()  # the channel: one request at a time, held through a slow sign
        self._last: dict[str, dict] = {}
        self.suspect = False  # a live child that did not answer in time
        self.unsettled: Callable[[], bool] = lambda: False  # set by the routes: is a signature's outcome unknown?
        self.client = None
        self.core: str | None = None
        self.contract: int | None = None  # the running signer's contract (3), once started
        self.features: frozenset[str] = frozenset()  # the running signer's `status.features` (strings only)
        self.last_spawn = -1e9
        self.refused: tuple[float, HostError] | None = None  # a signer of another contract: until this time
        self.restarts = 0
        self.stopped = False

    # -- lifecycle -------------------------------------------------------------------------------------------------
    @property
    def off(self) -> bool:
        return self.stopped or os.environ.get(SWITCH, "").lower() == "off"

    def _spawn(self):
        if self._start is not None:
            return self._start(child_env(self.state))
        from orch.click import spawn

        return spawn.start(child_env(self.state))

    def _alive(self) -> bool:
        proc = getattr(self.client, "proc", None)
        return self.client is not None and (proc is None or proc.poll() is None)

    def _drop(self) -> None:
        client, self.client = self.client, None
        self.suspect = False
        if client is not None:
            self.restarts += 1  # its open approvals are gone, however it was dropped (exit, lost channel, replaced)
            try:
                client.close()
            except Exception:  # noqa: BLE001 - a dead child must not stop a restart
                pass

    def _ensure(self):
        """The running signer, started (or restarted) if needed, and checked: its status must say `contract` 3."""
        if self.off:
            raise unavailable("Click-to-sign is switched off for this server.")
        if self.refused and self.clock() < self.refused[0]:
            raise self.refused[1]
        if self._alive():
            return self.client
        if self.client is not None:
            self._drop()  # it exited
        if self.clock() - self.last_spawn < RESTART_AFTER:
            raise unavailable()
        self.last_spawn = self.clock()
        try:
            client = self._spawn()
        except Exception:  # noqa: BLE001 - not a traceback: the person is told to use the terminal
            raise unavailable() from None
        try:
            status = client.call("status", {}, timeout=TIMEOUT["status"])
            if not isinstance(status, dict):
                raise unavailable()
        except Exception:  # noqa: BLE001
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            raise unavailable() from None
        contract = status.get("contract")
        if type(contract) is not int or contract not in CONTRACTS:  # strict: True is not 1, "3" is not 3
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            error = fail("click.contract", "Update orch-core: its signing helper speaks another contract.", exit=9)
            self.refused = (self.clock() + CONTRACT_RETRY, error)
            raise error
        self.refused = None
        self.contract = contract
        got = status.get("features")
        self.features = frozenset(f for f in got if type(f) is str) if isinstance(got, list) else frozenset()
        self.core = str(status.get("core"))[:40]
        self.client = client
        return client

    def switchable(self) -> bool:
        """Can the running signer (started now if needed) switch click-to-sign on and off? Never raises."""
        with self.lock:
            try:
                self._ensure()
            except HostError:
                return False
            return CUSTODY_FEATURE in self.features

    def sequences(self) -> bool:
        """Can the running signer (started now if needed) sign a sequence on one review (batch 11)? An older signer
        would ignore `sequence` and sign the first step only, so it is never asked. Never raises."""
        with self.lock:
            try:
                self._ensure()
            except HostError:
                return False
            return SEQUENCE_FEATURE in self.features

    def reveals(self) -> bool:
        """Can the running signer seal a grant secret to the page's key (`reveal`)? An older one answers
        `terminal_only` for that grant, so it is never asked. Never raises."""
        with self.lock:
            try:
                self._ensure()
            except HostError:
                return False
            return REVEAL_FEATURE in self.features

    def backup_keys(self) -> bool:
        """Can the running signer take the sealed hub backup key (`sign {sealed}`)? An older one answers `terminal_only`
        for a hub step, so it is never asked. Never raises."""
        with self.lock:
            try:
                self._ensure()
            except HostError:
                return False
            return BACKUP_KEY_FEATURE in self.features

    def passphrase_seals(self) -> bool:
        """Can the running signer take the new passphrase of "One click off" sealed (`custody_off {begin}`)? An older
        one would take it in the clear, so it is never asked. Never raises."""
        with self.lock:
            try:
                self._ensure()
            except HostError:
                return False
            return SEALED_PASSPHRASE_FEATURE in self.features

    def rotations(self) -> bool:
        """Can the running signer replace this computer's device key (`rotate`)? Never raises."""
        with self.lock:
            try:
                self._ensure()
            except HostError:
                return False
            return ROTATE_FEATURE in self.features

    def github_tokens(self) -> bool:
        """Can the running signer take the GitHub token sealed (`github_token`)? Never raises."""
        with self.lock:
            try:
                self._ensure()
            except HostError:
                return False
            return GITHUB_FEATURE in self.features

    def runner_switches(self) -> bool:
        """Can the running signer switch dashboard execution on and off (`runner`)? Never raises."""
        with self.lock:
            try:
                self._ensure()
            except HostError:
                return False
            return RUNNER_FEATURE in self.features

    def passkey_stepups(self) -> bool:
        """Does the running signer check a remote session's passkey step-up itself (`remote`, `stepup`)? An older one
        would ignore both and run the op, so a remote session's step-up op is never sent to it. Never raises."""
        with self.lock:
            try:
                self._ensure()
            except HostError:
                return False
            return STEPUP_FEATURE in self.features

    def images(self) -> bool:
        """Can the running signer hold a pasted screenshot (`stash`)? An older one would ignore `images` of the intent
        and fail at prepare, so it is never asked. Never raises."""
        with self.lock:
            try:
                self._ensure()
            except HostError:
                return False
            return IMAGES_FEATURE in self.features

    def close(self) -> None:
        with self.lock:
            self.stopped = True
            self._drop()

    # -- calls -----------------------------------------------------------------------------------------------------
    def call(self, op: str, args: dict, lost: "HostError | None" = None) -> dict:
        """One request on the channel (one at a time). See `_call`."""
        with self.chan:
            return self._call(op, args, lost)

    def _call(self, op: str, args: dict, lost: "HostError | None" = None) -> dict:
        """A refusal of the signer becomes a `HostError` with its code, reason and wait.

        `lock` only guards the lifecycle, so a slow `sign` never blocks `status`. A lost channel drops the child
        **only if it actually exited**: a timeout leaves a live signer alone (it may be finishing a submit, and killing
        it would lose its retained events and idem), and only marks it suspect. A suspect signer is replaced at the next
        call when no signature's outcome is unknown. `lost` is what the caller says for a lost answer (`sign`: the
        outcome is unknown, never "gone")."""
        from orch.click.protocol import ClickError

        with self.lock:
            if self.suspect and self.client is not None and not self.unsettled():
                self._drop()  # hung but alive, and nothing could be mid-submit: replace it
                self.last_spawn = -1e9  # on purpose: the respawn guard is for a signer that dies at once
            try:
                client = self._ensure()
            except HostError:
                raise
            except Exception:  # noqa: BLE001 - local failure before the sign request reaches the channel
                raise unavailable() from None
        try:
            result = client.call(op, args, timeout=TIMEOUT[op])
        except ClickError as exc:
            if lost is not None and exc.code == "internal":
                # The signer can report an exception after signing, or fail to encode its result.
                raise lost from None
            raise refusal(exc) from None
        except Exception as exc:  # noqa: BLE001 - channel loss, a frame error, a timeout
            dropped = False
            with self.lock:
                proc = getattr(client, "proc", None)
                exited = proc is None or proc.poll() is not None or not isinstance(exc, TimeoutError)
                if exited and self.client is client:
                    self._drop()
                    dropped = True
                elif self.client is client:
                    self.suspect = True
            if lost is not None:
                raise lost from None
            raise unavailable(
                "The signing helper stopped. Open sign requests are gone; sign in the terminal."
                if dropped
                else "The signing helper did not answer in time. Try again."
            ) from None
        return result

    def status(self) -> dict[str, dict]:
        """Per workspace: custody, enabled, available, problem. Empty when no signer can run (never raises). It never
        waits for the channel: while a request is in flight the last answer is returned."""
        if self.off:
            return {}
        if not self.chan.acquire(blocking=False):
            return self._last
        try:
            got = self._call("status", {})
        except HostError:
            return {}
        finally:
            self.chan.release()
        self._last = {
            w["workspace_id"]: w for w in got.get("workspaces", []) if isinstance(w, dict) and "workspace_id" in w
        }
        return self._last


def refusal(exc) -> HostError:
    """The signer's `ClickError` as the server's error: `code`, `reason` and `retry_after_ms` are kept (the UI
    branches on them); the message is the signer's display text."""
    error = {"code": exc.code, "message": exc.message, "retryable": bool(exc.retryable)}
    if exc.reason is not None:
        error["reason"] = exc.reason
    if exc.retry_after_ms is not None:
        error["retry_after_ms"] = int(exc.retry_after_ms)
    return HostError(error, exit=9 if exc.code in {"lock.busy", "host.busy"} else 3)


#: The outcome of a `sign` whose answer was lost is unknown, not "the helper is gone": the person must not sign again.
def lost_sign() -> HostError:
    return HostError(
        {
            "code": "human.click_refused",
            "reason": "uncertain",
            "message": "The signing helper did not answer. The signature may or may not have reached the host.",
            "retryable": False,
        },
        exit=3,
    )
