"""One bounded, isolated records tool per workspace. No shell, signer descriptors or inherited grants."""

import json
import os
import selectors
import signal
import subprocess
import sys
import threading
import time

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .adapters.addons import prose
from .errors import HostError, fail
from .gateway import WORKSPACE_ID, state_dir

BUDGET = 60
OUTPUT_LIMIT = 65_536  # each pipe; stderr is drained but never returned or logged
ENV_KEYS = ("HOME", "PATH", "SSH_AUTH_SOCK", "LANG", "ORCH_STATE_DIR", "XDG_CONFIG_HOME")


class Result(BaseModel):
    # H11e also permits success detail and failure hint; neither is forwarded to the browser.
    model_config = ConfigDict(extra="forbid", strict=True)
    ok: bool
    code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")
    message: str | None = Field(default=None, max_length=400)
    commit: str | None = Field(default=None, pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
    pushed: bool | None = None
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    detail: dict | None = None
    hint: str | None = Field(default=None, max_length=400)


def catalog(code):
    from orch.host.errors import HOST_CODES
    from orch.ops.errors import ERRORS
    from orch.records import RecordsError

    if code in RecordsError.EXITS:
        return RecordsError.EXITS[code], code in {"records.snapshot_changed", "records.busy", "records.budget"}
    if code in HOST_CODES:
        return HOST_CODES[code]
    if code in ERRORS:
        spec = ERRORS[code]
        return spec.exit, spec.retryable
    if code == "host.unavailable":
        return 9, True
    raise fail("records.child_invalid", "The records child returned an unknown error code.", exit=1)


def child_error(code):
    exit_code, retryable = catalog(code)
    return HostError({"code": code, "message": code, "retryable": retryable}, exit_code)


def parse(output, returncode):
    try:
        # Exactly one line, with an optional final newline. Reject duplicate keys and non-JSON constants.
        def pairs(items):
            out = {}
            for k, v in items:
                if k in out:
                    raise ValueError("duplicate key")
                out[k] = v
            return out

        def constant(value):
            raise ValueError("non-JSON constant")

        lines = output.decode("utf-8").splitlines()
        if len(lines) != 1:
            raise ValueError("expected one line")
        raw = json.loads(lines[0], object_pairs_hook=pairs, parse_constant=constant)
        result = Result.model_validate(raw)
        allowed = (
            {"ok", "commit", "pushed", "url", "detail"} if result.ok else {"ok", "code", "message", "hint", "commit"}
        )
        if set(raw) - allowed or any(v is None for v in raw.values()):
            raise ValueError("invalid envelope")
        if result.ok:
            if returncode != 0:
                raise ValueError("success with nonzero exit")
            out = result.model_dump(include={"ok", "commit", "pushed", "url"}, exclude_none=True)
            if result.url is not None:
                out.update(prose(out, "url"))
            return out
        if result.code is None or result.message is None:
            raise ValueError("missing error")
        exit_code, retryable = catalog(result.code)
        if returncode != exit_code:
            raise ValueError("catalog exit mismatch")
        error = {"code": result.code, "retryable": retryable, **prose(raw, "message")}
        if result.commit is not None:
            error["commit"] = result.commit  # a failed push may already have committed locally
        raise HostError(error, exit_code)
    except (ValueError, UnicodeError, ValidationError, RecursionError):
        raise fail("records.child_invalid", "The records child returned invalid output.", exit=1) from None


class RecordsChild:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = set()

    def run(self, ws, *, push=False, auto_off=False, state=None):
        if not isinstance(ws, str) or not WORKSPACE_ID.fullmatch(ws):
            raise fail("invalid.input", exit=5)
        with self.lock:
            if ws in self.active:
                raise child_error("records.busy")
            self.active.add(ws)
        try:
            env = {key: os.environ[key] for key in ENV_KEYS if key in os.environ}
            # Pin the same resolved state as the gateway, including an explicitly supplied gateway state.
            env["ORCH_STATE_DIR"] = str(state_dir({**env, **({"ORCH_STATE_DIR": str(state)} if state else {})}))
            argv = [sys.executable, "-I", "-m", "orch.records"]
            argv += ["auto", "off"] if auto_off else ["commit", *(["--push"] if push else [])]
            argv += ["--workspace", ws, "--json"]
            return self._execute(argv, env)
        finally:
            with self.lock:
                self.active.remove(ws)

    def _execute(self, argv, env):
        deadline = time.monotonic() + BUDGET
        try:
            proc = subprocess.Popen(
                argv,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                close_fds=True,
            )
        except OSError:
            raise fail("records.child_unavailable", "The records child could not start.", exit=1) from None
        output = bytearray()
        counts = {proc.stdout: 0, proc.stderr: 0}
        try:
            with selectors.DefaultSelector() as selector:
                for pipe in counts:
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ)
                while selector.get_map():
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise child_error("records.budget")
                    for key, _ in selector.select(left):
                        data = os.read(key.fileobj.fileno(), 8192)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        counts[key.fileobj] += len(data)
                        if counts[key.fileobj] > OUTPUT_LIMIT:
                            raise fail(
                                "records.child_output_limit", "The records child exceeded its output limit.", exit=1
                            )
                        if key.fileobj is proc.stdout:
                            output.extend(data)
                try:
                    code = proc.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    raise child_error("records.budget") from None
            return parse(bytes(output), code)
        finally:
            # Do not poll/reap before killing on failure: keep the child's PID reserved until the group is killed.
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            proc.stdout.close()
            proc.stderr.close()
