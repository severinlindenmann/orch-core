"""T2 for B1b: the PRODUCT CLI as the real owner, under a pseudo-terminal, against the REAL orch-relay of ``SOURCE``
(loopback only). No sandbox manifest, no dev-pair, no ``tests/relay/owner.py``: ``orch init`` -> host -> ``orch keys
primary init --workspace --relay --invite-file`` (account) -> ``orch host relay link`` -> ``orch host relay primary
set`` -> ``orch host relay pair`` (inline primary, the default), the reference phone (``tests/relay/phone.py``) on the
relay's HTTP API. Cases: a wrong recovery code three times (nothing signed, offer cancelled), a wrong ``cert_code``
three times (rejection), a kill at the recovery prompt then ``--resume`` to ``approved`` (+ the phone's column-R
read), and a second install of the same person (409 ``account_exists`` -> ``PUT /v2/devices``). The run is timed
(review L6): the numbers are printed with ``-s`` and bounded.

The root lives below the checkout (not ``/tmp``: the host refuses a run directory below a sticky directory)."""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from orch import crypto
from orch.identity import recovery as rec
from tests.relay.phone import DevPhone, HttpNet, PhoneError

from .harness import LIMITS, RelayProcess, relay_dir, start_relay

pytestmark = pytest.mark.relay_t2

HERE = Path(__file__).resolve()
BASE = HERE.parents[5]  # the checkout root: a real directory below the user's home, not /tmp
# the host's control socket is `<root>/run/<32 hex>/host.sock` and must fit 100 UTF-8 bytes
# (listener.SOCKET_PATH_LIMIT); the root is `.t2b` + 8 random characters, so a checkout nested deeper than this (a
# worktree under ~/orch-dev/.worktrees) cannot hold it: the install then goes below the account's home instead
_SOCK_TAIL = len("/run//host.sock") + 32 + len(".t2b") + 8 + 1
SOCKET_PATH_LIMIT = 100


def install_base() -> Path | None:
    import pwd

    for base in (BASE, Path(pwd.getpwuid(os.getuid()).pw_dir)):  # not $HOME: the tests run with HOME below /tmp
        if base.is_dir() and not str(base).startswith(("/tmp", "/private/tmp")):
            if len(str(base).encode()) + _SOCK_TAIL <= SOCKET_PATH_LIMIT:
                return base
    return None


MIB4 = 4 << 20
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PRIMARY_PASS = "primary-pass-Tq7!mPx2-vL9rTb"
SAS_PROMPT = "pairing code: type the 6 characters"
CERT_PROMPT = "compare code: type the 6 characters"
TIMINGS: dict[str, float] = {}


class Killed(BaseException):  # noqa: N818 - a BaseException so the pty driver kills the child and re-raises
    pass


@dataclass
class Responder:
    pattern: re.Pattern[str]
    fn: Callable[[re.Match[str]], str | None]
    #: the answer goes out only after the prompt has been the last output for this long: the review prompt flushes the
    #: terminal's input right after it writes the prompt (`tcflush`, on purpose), so an answer sent at once is dropped
    quiet: float = 0.0


def run_pty(
    argv: Sequence[str],
    *,
    env: dict[str, str],
    cwd: Path,
    responders,
    timeout: float,
    pass_fds: Sequence[int] = (),
    watch: Sequence[int] = (),
    on_watch=None,
    observe=None,
) -> tuple[int, str]:
    """``argv`` with a pseudo-terminal as its controlling terminal; answers prompts through ``responders`` (the end of
    the unanswered output matches). Returns ``(exit, ANSI-stripped transcript)``. A BaseException from a responder
    kills the child and propagates."""
    master, slave = os.openpty()
    wrapper = (
        "import fcntl, os, sys, termios\nfcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
        "os.execve(sys.argv[1], sys.argv[1:], dict(os.environ))\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", wrapper, *argv],
        cwd=cwd,
        env=env,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        pass_fds=tuple(pass_fds),
    )
    os.close(slave)
    buf = bytearray()
    scan = 0
    last_output = time.monotonic()
    end = time.monotonic() + timeout
    status = None
    try:
        while True:
            ready, _, _ = select.select([master, *watch], [], [], 0.2)
            for fd in ready:
                if fd == master:
                    try:
                        data = os.read(master, 4096)
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        data = b""
                    buf += data
                    if data:
                        last_output = time.monotonic()
                    if observe is not None and data:
                        observe(ANSI.sub("", data.decode("utf-8", "replace")).replace("\r", ""))
                elif on_watch is not None:
                    on_watch(fd, os.read(fd, 4096))
            tail = ANSI.sub("", bytes(buf[scan:]).decode("utf-8", "replace")).replace("\r", "")
            for r in responders() if callable(responders) else responders:
                m = re.search(r.pattern.pattern + r"\Z", tail, r.pattern.flags)
                if m is not None:
                    if time.monotonic() - last_output < r.quiet:
                        break  # the prompt is on screen, but the program may not be reading yet: look again
                    reply = r.fn(m)
                    if reply is not None:
                        os.write(master, reply.encode() + b"\n")
                        scan = len(buf)
                    break
            rc = proc.poll()
            if rc is not None:
                status = rc
                with contextlib.suppress(OSError):
                    while select.select([master], [], [], 0)[0]:
                        more = os.read(master, 4096)
                        if not more:
                            break
                        buf += more
                break
            if time.monotonic() >= end:
                raise AssertionError(
                    f"{argv[-3:]}: timed out\n{ANSI.sub('', bytes(buf).decode('utf-8', 'replace'))[-1500:]}"
                )
    except BaseException:
        if status is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            proc.wait()
        raise
    finally:
        with contextlib.suppress(OSError):
            os.close(master)
    return status if status is not None else -1, ANSI.sub("", bytes(buf).decode("utf-8", "replace")).replace("\r", "")


@dataclass
class Install:
    """One real-install-shaped root: its own HOME, state and run directories; the allow-list environment."""

    root: Path
    relay: RelayProcess
    passphrase: str = ""
    code: str = ""
    wid: str = ""
    host: subprocess.Popen | None = None
    ws_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        for d in ("home", "tmp", "state", "run", "ws"):
            os.mkdir(self.root / d, 0o700)
        os.chmod(self.root / "run", 0o711)  # the host's run directory is 0711
        self.ws_dir = self.root / "ws"

    def env(self) -> dict[str, str]:
        r = str(self.root)
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": f"{r}/home",
            "ORCH_STATE_DIR": f"{r}/state",
            "ORCH_HOST_RUN_DIR": f"{r}/run",
            "LANG": "C.UTF-8",
            "TMPDIR": f"{r}/tmp",
            "ORCH_NO_CONVENIENCE": "1",
            "TERM": "xterm",
        }

    def orch(self, *a: str) -> list[str]:
        return [sys.executable, "-m", "orch", *a]

    def plain(self, *a: str, timeout: float = 120) -> subprocess.CompletedProcess:
        return subprocess.run(
            self.orch(*a),
            cwd=self.ws_dir,
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )

    def pty(self, *a: str, responders, timeout: float = 240, **kw) -> tuple[int, str]:
        t0 = time.monotonic()
        try:
            return run_pty(self.orch(*a), env=self.env(), cwd=self.ws_dir, responders=responders, timeout=timeout, **kw)
        finally:
            TIMINGS[" ".join(a[:3])] = TIMINGS.get(" ".join(a[:3]), 0.0) + time.monotonic() - t0

    # -- the prompts every command shares
    def review(self) -> Responder:
        return Responder(
            re.compile(r"Type (\S+)(?: and Enter)? to continue(?:; anything else stops| \(d = details\)): "),
            lambda m: m.group(1),
            quiet=0.3,
        )

    def passphrases(self, primary: str | None = None) -> list[Responder]:
        def phrase(m):
            return primary if primary and "your primary key" in m.string else self.passphrase

        return [
            Responder(re.compile(r"Repeat passphrase: "), lambda m: primary or self.passphrase),
            Responder(re.compile(r"Passphrase: "), phrase),
            self.review(),
        ]

    def stop(self) -> None:
        if self.host is not None and self.host.poll() is None:
            self.host.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.host.wait(10)
            if self.host.poll() is None:
                self.host.kill()
                self.host.wait()


def init_workspace(inst: Install) -> None:
    """``orch init`` through the pty (as the owner does): the generated passphrase typed back, three recovery-code words
    confirmed; the passphrase and the code are kept in memory only (this is the owner's own paper)."""
    st: dict = {"pass": None, "code": None, "all": ""}
    phrase = re.compile(r"Your generated passphrase:\s*\n\s*\n\s+(.+?)\s*\n")
    code_re = re.compile(r"shown now, once\.\s*\n\s*\n((?: *\d+ [a-z]+(?: +\d+ [a-z]+)*\n){4})")

    def observe(chunk: str) -> None:
        st["all"] = (st["all"] + chunk)[-8192:]
        if st["pass"] is None and (m := phrase.search(st["all"])):
            st["pass"] = m.group(1).strip()
        if m2 := code_re.search(st["all"]):
            st["code"] = " ".join(re.findall(r"\d+ ([a-z]+)", m2.group(1)))

    def word(m):
        words = (st["code"] or "").split()
        n = int(m.group(1))
        return words[n - 1] if 1 <= n <= len(words) else None

    pw = lambda m: st["pass"]  # noqa: E731
    responders = [
        Responder(re.compile(r"Passphrase: "), pw),
        Responder(re.compile(r"Repeat your passphrase: "), pw),
        Responder(re.compile(r"Repeat passphrase: "), pw),
        Responder(re.compile(r"Press Enter when you have written the code down: "), lambda m: ""),
        Responder(re.compile(r"Word (\d+): "), word),
    ]
    rc, _ = inst.pty("init", "--prefix", "T2B", "--name", "b1b", responders=responders, timeout=300, observe=observe)
    assert rc == 0 and st["pass"] and st["code"], f"orch init failed ({rc})"
    inst.passphrase, inst.code = st["pass"], st["code"]


def start_host(inst: Install) -> None:
    log = open(inst.root / "host.log", "wb")  # noqa: SIM115
    inst.host = subprocess.Popen(
        inst.orch("host", "run", "--foreground"),
        cwd=inst.root,
        env=inst.env(),
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log.close()
    sock = inst.root / "run" / "control" / "host.sock"
    end = time.monotonic() + 60
    while time.monotonic() < end:
        if inst.host.poll() is not None:
            raise AssertionError("the host exited at start:\n" + (inst.root / "host.log").read_text()[-1500:])
        if sock.exists():
            break
        time.sleep(0.25)
    r = inst.plain("host", "register", "--no-service", str(inst.ws_dir))
    assert r.returncode == 0, (r.stdout + r.stderr)[-800:]
    wss = list((inst.root / "state" / "hosts").iterdir())
    assert len(wss) == 1
    inst.wid = wss[0].name


def invite_file(inst: Install, name: str) -> Path:
    path = inst.root / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, inst.relay.admin("invite", "create", "--ttl", "1h").strip().encode() + b"\n")
    os.close(fd)
    return path


def call(inst: Install, op: str, args: dict) -> dict:
    from orch.host.control_client import ControlClient

    with ControlClient(inst.root / "run" / "control" / "host.sock") as c:
        res = c.call(op, args)
    assert res.get("ok"), res
    return res["result"]["data"]


def wait_for(fn, what: str, timeout: float = 90.0, every: float = 0.25):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        last = fn()
        if last:
            return last
        time.sleep(every)
    raise AssertionError(f"timed out waiting for {what}: last {last!r}")


@pytest.fixture
def relay_long(tmp_path_factory):
    """The real relay of the pinned checkout, without the 20 s per-test budget of the in-process T2 tests (this test
    drives real processes with the production KDF; its own run time is measured and bounded)."""
    d = relay_dir()
    rp = start_relay(d, tmp_path_factory.mktemp("relay-data"), LIMITS[0])
    try:
        yield rp
    finally:
        leaked = rp.finish()
    assert not leaked, f"relay teardown leaked (reaped): {leaked}"


@pytest.fixture
def install(relay_long):
    base = install_base()
    if base is None:
        pytest.skip("neither the checkout nor the account's home is a short real directory: the host refuses a run "
                    "directory in /tmp and a control socket path over 100 bytes")  # fmt: skip
    root = Path(tempfile.mkdtemp(prefix=".t2b", dir=base))
    os.chmod(root, 0o700)
    inst = Install(root, relay_long)
    try:
        yield inst
    finally:
        inst.stop()
        shutil.rmtree(root, ignore_errors=True)


class PhoneRun:
    """The reference phone on a thread: joins through the link, tells the codes it shows, ends approved or refused."""

    def __init__(self, inst: Install, link: str) -> None:
        self.phone = DevPhone(link, label="t2 phone")
        self.net = HttpNet(inst.relay.origin, inst.wid)
        self.codes: dict[str, str] = {}
        self.result: object = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self) -> None:
        try:
            self.result = self.phone.pair(
                self.net, codes=lambda o, ph, c: self.codes.__setitem__(ph, c), deadline_s=420, poll_s=0.5
            )
        except BaseException as exc:  # noqa: BLE001
            self.result = exc


def pair_responders(
    inst: Install,
    phone: PhoneRun | None,
    *,
    recovery: Callable[[], str | None],
    cert: str,
    kill_at_recovery: bool = False,
) -> list[Responder]:
    """The terminal side of ``orch host relay pair``: the SAS from the phone, the primary passphrase, the recovery
    code (``recovery()`` -> text, or ``None`` = wait), the phone's second code (``cert``: ``"phone"`` types what the
    phone shows, else that text)."""
    sasd = {"waited": 0}

    def sas(m):
        got = phone.codes.get("sas") if phone is not None else None
        if got is None:
            sasd["waited"] += 1
            return None
        return got

    def cert_code(m):
        if cert != "phone":
            return cert
        return phone.codes.get("cert_code")

    def recovery_code(m):
        if kill_at_recovery:
            raise Killed
        return recovery()

    return [
        Responder(re.compile(SAS_PROMPT + r".*: "), sas),
        Responder(re.compile(CERT_PROMPT + r".*: "), cert_code),
        Responder(re.compile(r"Recovery code: "), recovery_code),
        *inst.passphrases(PRIMARY_PASS),
    ]


def start_pair(
    inst: Install,
    extra: Sequence[str] = (),
    *,
    responders_for: Callable[[PhoneRun | None], list[Responder]],
    timeout: float = 300,
):
    """Run ``orch host relay pair`` under the pty. The offer's link arrives on ``--json-fd``: the phone starts from it.
    Returns ``(exit, transcript, offer_id, phone)``."""
    r_fd, w_fd = os.pipe()
    seen: dict = {"line": b"", "offer": None, "phone": None}

    def on_watch(_fd, data):
        seen["line"] += data
        while b"\n" in seen["line"]:
            raw, seen["line"] = seen["line"].split(b"\n", 1)
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if "link" in obj and seen["phone"] is None:
                seen["offer"] = obj["offer_id"]
                seen["phone"] = PhoneRun(inst, obj["link"])

    try:
        rc, text = inst.pty(
            "host",
            "relay",
            "pair",
            "--workspace",
            inst.wid,
            *extra,
            "--json-fd",
            str(w_fd),
            "--json",
            responders=lambda: responders_for(seen["phone"]),
            timeout=timeout,
            pass_fds=(w_fd,),
            watch=(r_fd,),
            on_watch=on_watch,
        )
    finally:
        os.close(w_fd)
        os.close(r_fd)
    return rc, text, seen["offer"], seen["phone"]


def journal_states(inst: Install) -> list[str]:
    from orch.relay_owner import PrimaryJournal, journal_dir

    with PrimaryJournal(journal_dir(inst.root / "state")).transaction() as v:
        return [e.state for e in v.entries()]


def test_the_real_owner_pairs_the_real_mac_with_the_product_cli(install: Install):
    inst = install
    assert not (inst.root / ".orch-dev-sandbox").exists()
    t_all = time.monotonic()
    # -- orch init, the host, the account (orch keys primary init), link, primary set
    init_workspace(inst)
    start_host(inst)
    inv = invite_file(inst, "invite")
    rc, text = inst.pty(
        "keys",
        "primary",
        "init",
        "--workspace",
        inst.wid,
        "--relay",
        inst.relay.origin,
        "--invite-file",
        str(inv),
        responders=[
            Responder(re.compile(r"Recovery code: "), lambda m: inst.code),
            Responder(re.compile(r"Repeat passphrase: "), lambda m: PRIMARY_PASS),
            Responder(re.compile(r"Passphrase: "), lambda m: PRIMARY_PASS),
        ],
    )
    assert rc == 0, text[-1200:]
    assert inst.code not in text, "the recovery code is not echoed"
    state_blob = b"".join(p.read_bytes() for p in (inst.root / "state").rglob("*") if p.is_file())
    invite_text = inv.read_text().strip().encode()
    assert invite_text not in state_blob and inst.code.encode() not in state_blob
    rc, text = inst.pty(
        "host",
        "relay",
        "link",
        "--workspace",
        inst.wid,
        "--max-publication-bytes",
        str(MIB4),
        responders=inst.passphrases(),
        timeout=240,
    )  # --relay defaults to the registered primary's
    assert rc == 0 and '"link": "linked"' in text, text[-1200:]
    r = inst.plain("host", "relay", "primary", "set", "--workspace", inst.wid)  # the stored certificate and label
    assert r.returncode == 0, r.stdout + r.stderr

    # -- (1) a wrong cert_code three times: a rejection is signed and posted; the phone is refused
    rc, text, offer, phone = start_pair(
        inst,
        responders_for=lambda ph: pair_responders(inst, ph, recovery=lambda: inst.code, cert="AAAAAA"),
        timeout=300,
    )
    assert rc == 3 and "the pairing ended: rejected" in text, text[-1500:]
    assert text.count("that is not the code:") == 3 and "state: rejected" in text
    wait_for(lambda: not phone.thread.is_alive(), "the phone's refusal", 60)
    assert isinstance(phone.result, PhoneError) and phone.result.code == "rejected", phone.result
    assert inst.code not in text

    # -- (2) killed at the recovery prompt (the request is verified and shown), then --resume to approved
    holder: dict = {}

    def killing(ph):
        holder["phone"] = ph
        return pair_responders(inst, ph, recovery=lambda: inst.code, cert="phone", kill_at_recovery=True)

    with pytest.raises(Killed):
        start_pair(inst, responders_for=killing, timeout=300)
    phone = holder["phone"]
    offers = call(inst, "host.relay_pair_list", {"workspace_id": inst.wid})["offers"]
    mine = next(o for o in offers if o["state"] == "cert_pending")
    offer = mine["offer_id"]
    assert mine["outstanding_request"]
    t_resume = time.monotonic()
    rc, text = inst.pty(
        "host",
        "relay",
        "pair",
        "--workspace",
        inst.wid,
        "--resume",
        "--offer",
        offer,
        responders=pair_responders(inst, phone, recovery=lambda: inst.code, cert="phone"),
        timeout=420,
    )
    TIMINGS["pair resume leg"] = time.monotonic() - t_resume
    assert rc == 0 and "state: approved" in text and "approved: t2 phone is paired" in text, text[-2000:]
    assert inst.code not in text and PRIMARY_PASS not in text and inst.passphrase not in text
    wait_for(lambda: not phone.thread.is_alive(), "the phone's approval", 60)
    assert getattr(phone.result, "scopes", None) == ("look", "decide"), phone.result

    # -- the phone joins (R6b published the membership) and reads column R
    def joined():
        try:
            return phone.phone.join(phone.net)
        except PhoneError as exc:
            if exc.code in ("not_a_member", "below_anchor", "no_grant"):
                return None
            raise

    assert wait_for(joined, "the phone's join", 120)
    meta, data = phone.phone.whoami(phone.net)
    assert meta == {"status": "ok"}, (meta, data)

    # -- (3) last: a wrong recovery code three times: nothing is signed, the offer is cancelled
    other = rec.generate_recovery_code()
    tries = {"n": 0}

    def wrong_code():
        tries["n"] += 1
        return other

    rc, text, offer, phone = start_pair(
        inst, responders_for=lambda ph: pair_responders(inst, ph, recovery=wrong_code, cert="phone"), timeout=300
    )
    assert rc == 3 and tries["n"] == 3, (rc, tries, text[-1500:])
    assert "no valid recovery code was entered; nothing was signed" in text and "the pairing is cancelled" in text
    assert "a new offer is possible in about" in text
    assert "d_" in text and "named by the device, not authenticated" in text
    assert inst.code not in text and other not in text
    states = journal_states(inst)  # the two earlier requests were decided and uploaded; this one was only cancelled
    assert sorted(states) == ["cancelled", "uploaded", "uploaded"], states
    assert (
        call(inst, "host.relay_pair_show", {"workspace_id": inst.wid, "offer_id": offer})["offer"]["state"]
        == "cancelled"
    )
    phone.thread.join(1)  # the phone's thread ends on its own deadline; it is a daemon

    # -- no secret anywhere under the state directory or the pair journal
    blob = b"".join(p.read_bytes() for p in (inst.root / "state").rglob("*") if p.is_file())
    assert invite_text not in blob and inst.code.encode() not in blob and PRIMARY_PASS.encode() not in blob
    TIMINGS["total"] = time.monotonic() - t_all
    print("\nT2 b1b timings (s):", json.dumps({k: round(v, 1) for k, v in TIMINGS.items()}, sort_keys=True))
    assert TIMINGS["total"] < 1200


def test_a_second_install_of_the_same_person_joins_the_account_by_409(install: Install, monkeypatch):
    """The account exists (made by the first install); a second install registers its own primary device with the same
    stored-certificate path: ``POST /v2/accounts`` -> 409 ``account_exists`` -> ``PUT /v2/devices`` -> login."""
    from orch.custody import KdfParams, PassphraseBackend, primary, primary_register
    from orch.relay_owner import OwnerClient

    inst = install
    code = rec.generate_recovery_code()
    pk = rec._key_at(code, 2**10)
    pk_pub = crypto.public_bytes(pk)

    class RealPK:
        """The person key of one command, from a test-sized derivation (the account is the same person)."""

        def __init__(self, **kw):
            self.pk_pub = pk_pub
            self.person_id = crypto.person_id(pk_pub)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def signer(self, label):
            from orch.crypto.labels import L

            def sign(payload):
                assert payload.startswith(L[label])
                return crypto.sign(pk, payload)

            return sign

        def derive_label_key(self):
            return b"\x05" * 32

    fast = KdfParams(n=2**10)
    monkeypatch.setattr(
        primary,
        "open_backend",
        lambda d: PassphraseBackend(d, _passphrase_provider=lambda r: PRIMARY_PASS, _kdf=fast, _min_n=2**10),
    )
    results = []
    for n in (1, 2):
        st = inst.root / f"s{n}"
        inv = invite_file(inst, f"invite{n}")
        results.append(
            primary_register.register(
                st,
                origin=inst.relay.origin,
                owner_pk=pk_pub,
                invite_file=inv,
                label=f"primary {n}",
                pk_factory=RealPK,
                client_factory=lambda o, s: OwnerClient(o, s),
            )
        )
    first, second = results
    assert first["person_id"] == second["person_id"] == crypto.person_id(pk_pub).hex()
    assert first["device_id"] != second["device_id"]
    assert primary_register.read_cert(inst.root / "s2")["registered"] is True
