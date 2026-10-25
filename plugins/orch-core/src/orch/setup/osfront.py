"""The root side of ``orch remote enable|disable``: the HTTPS front of the remote dashboard (#1198 R1-7, spec 10.2).

Runs inside ``orch-os-setup`` (``--front-enable`` / ``--front-disable``), from the root-owned trusted copy, standard
library only (it is listed in ``oslauncher.TRUSTED_FILES``). Root never runs the person's release; everything the
person's ``orch remote`` command decides arrives as a few validated arguments, and this module rebuilds every file
from them.

What ``enable`` makes (and nothing else):

* the system user and group ``orch-front`` (no login, locked, no home to create: systemd's ``StateDirectory`` owns
  ``/var/lib/orch-front``). It is never the person, root, or an ``orch-agent-*`` / ``orch-addon-*`` user: the front
  exists to keep exactly those processes out.
* ``/run/orch/front/<person uid>``, made at every boot by systemd-tmpfiles: owned by the person, group ``orch-front``,
  mode 0750. The dashboard binds ``front.sock`` there. Why not under ``<state>/run`` (0700) or ``/run/orch/<uid>``
  (setgid, the agent's group): the front's user could not reach either without opening it for the agent too. The
  dashboard's peer-UID check on every connection stays the gate; the directory only keeps everyone else from looking.
* ``/etc/orch/front/Caddyfile`` (root, 0644): a reverse proxy to that socket, ACME for the name, TLS 1.2+, HSTS, a
  10 s header timeout, at most 32 connections to the dashboard, and ``X-Orch-Client`` set (never forwarded) from
  Caddy's own idea of the client address: the TCP peer, or, behind the trusted proxies given with ``--trusted-proxy``,
  the address those proxies report.
* ``/etc/systemd/system/orch-front.service``: Caddy as ``orch-front``, one capability (port 80/443), sandboxed.

Rules shared with ``osapply``: fixed absolute binaries, an empty environment, repair never widen (a foreign file is
refused, a file of ours is replaced), a dry run prints the exact actions and changes nothing.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from .osapply import BIN, ApplyResult, Executor, _Apply
from .osfacts import OsFacts
from .osusers import AGENT_PREFIX, SLOT_PREFIX, valid_person

__all__ = [
    "CADDYFILE",
    "CADDY_BIN",
    "FRONT_GROUP",
    "FRONT_RUN_ROOT",
    "FRONT_USER",
    "TMPFILES_FRONT",
    "UNIT_NAME",
    "UNIT_PATH",
    "FrontPlan",
    "apply_disable",
    "apply_enable",
    "run",
    "valid_name",
    "valid_proxy",
]

FRONT_USER = FRONT_GROUP = "orch-front"
FRONT_HOME = "/var/lib/orch-front"
FRONT_RUN_ROOT = "/run/orch/front"
CONF_DIR = "/etc/orch/front"
CADDYFILE = f"{CONF_DIR}/Caddyfile"
UNIT_NAME = "orch-front.service"
UNIT_PATH = f"/etc/systemd/system/{UNIT_NAME}"
TMPFILES_FRONT = "/etc/tmpfiles.d/orch-front.conf"
CADDY_BIN = "/usr/bin/caddy"
MARKER = "# managed by orch (orch remote enable); a change here is overwritten, `orch remote disable` removes it"
_PERSON_LINE = re.compile(r"^# orch-front person-uid=(\d+) name=(\S+)$", re.MULTILINE)
_SYSTEM_UID_MAX = 999
_PERSON_MIN_UID = 1000
_NOLOGIN = ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false")
_PRIVILEGED = ("sudo", "wheel", "admin", "adm", "docker", "lxd", "libvirt", "disk", "shadow", "root")
_NAME = re.compile(r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?")
MAX_PROXIES = 16


def valid_name(name: str) -> str | None:
    """Why ``name`` cannot be the dashboard's public name (``None``: it can). Same rule as the dashboard's remote
    policy: a lowercase DNS name with a dot, no port, no IP address, no trailing dot, not a local name."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        return "the name must be a lowercase DNS name such as orch.example.com (no port, no address, no trailing dot)"
    if name.split(".")[-1].isdigit() or name.endswith((".localhost", ".local", ".internal", ".test", ".invalid")):
        return "the name must be a public DNS name"
    return None


def valid_proxy(text: str) -> str | None:
    """The normalized network of a trusted proxy (``--trusted-proxy``), or ``None`` when it is not acceptable. A
    network that holds everyone (a /0) would let any client choose its own address, so it is refused."""
    try:
        net = ipaddress.ip_network(text.strip(), strict=False)
    except ValueError:
        return None
    return None if net.prefixlen == 0 else str(net)


@dataclass(frozen=True)
class FrontPlan:
    name: str
    person: str
    person_uid: int
    proxies: tuple[str, ...] = ()

    @property
    def socket_dir(self) -> str:
        return f"{FRONT_RUN_ROOT}/{self.person_uid}"

    @property
    def socket(self) -> str:
        return f"{self.socket_dir}/front.sock"

    def problem(self) -> str | None:
        bad = valid_name(self.name)
        if bad:
            return bad
        if not valid_person(self.person) or self.person_uid < _PERSON_MIN_UID:
            return f"'{self.person}' is not a normal user that runs orch"
        if len(self.proxies) > MAX_PROXIES or any(valid_proxy(p) != p for p in self.proxies):
            return "each trusted proxy must be one network such as 203.0.113.0/24 (never 0.0.0.0/0), at most 16"
        return None

    def caddyfile(self) -> str:
        servers = ["\t\ttimeouts {", "\t\t\tread_header 10s", "\t\t}"]
        if self.proxies:
            servers.append(f"\t\ttrusted_proxies static {' '.join(self.proxies)}")
        return "\n".join(
            [
                MARKER,
                f"# orch-front person-uid={self.person_uid} name={self.name}",
                "{",
                "\tadmin off",
                "\tservers {",
                *servers,
                "\t}",
                "}",
                "",
                f"{self.name} {{",
                "\ttls {",
                "\t\tprotocols tls1.2 tls1.3",
                "\t}",
                '\theader Strict-Transport-Security "max-age=31536000"',
                f"\treverse_proxy unix/{self.socket} {{",
                # `header_up` with a value SETS the header: whatever the browser sent is replaced, never forwarded.
                # {client_ip} is the TCP peer, or the address the trusted proxies report (needs Caddy 2.7+).
                "\t\theader_up X-Orch-Client {client_ip}",
                "\t\ttransport http {",
                "\t\t\tdial_timeout 5s",
                "\t\t\tmax_conns_per_host 32",
                "\t\t}",
                "\t}",
                "}",
                "",
            ]
        )

    def unit(self) -> str:
        return "\n".join(
            [
                MARKER,
                "[Unit]",
                "Description=orch HTTPS front for the remote dashboard (Caddy)",
                "After=network-online.target",
                "Wants=network-online.target",
                "",
                "[Service]",
                "Type=notify",
                f"User={FRONT_USER}",
                f"Group={FRONT_GROUP}",
                "StateDirectory=orch-front",
                "StateDirectoryMode=0700",
                f"Environment=HOME={FRONT_HOME} XDG_DATA_HOME={FRONT_HOME}/data XDG_CONFIG_HOME={FRONT_HOME}/config",
                f"ExecStartPre={CADDY_BIN} validate --config {CADDYFILE} --adapter caddyfile",
                f"ExecStart={CADDY_BIN} run --config {CADDYFILE} --adapter caddyfile",
                "Restart=on-failure",
                "RestartSec=5s",
                "TimeoutStopSec=10s",
                "LimitNOFILE=8192",
                "AmbientCapabilities=CAP_NET_BIND_SERVICE",
                "CapabilityBoundingSet=CAP_NET_BIND_SERVICE",
                "NoNewPrivileges=yes",
                "ProtectSystem=strict",
                "ProtectHome=yes",
                "PrivateTmp=yes",
                "PrivateDevices=yes",
                "ProtectKernelTunables=yes",
                "ProtectKernelModules=yes",
                "ProtectControlGroups=yes",
                "RestrictSUIDSGID=yes",
                "RestrictNamespaces=yes",
                "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
                "LockPersonality=yes",
                "SystemCallArchitectures=native",
                "UMask=0077",
                "",
                "[Install]",
                "WantedBy=multi-user.target",
                "",
            ]
        )

    def tmpfiles(self) -> str:
        return (
            "\n".join(
                [
                    MARKER,
                    f"d {FRONT_RUN_ROOT} 0711 root root -",
                    f"d {self.socket_dir} 0750 {self.person} {FRONT_GROUP} -",
                ]
            )
            + "\n"
        )

    def user_args(self) -> list[str]:
        return [
            "--system",
            "--user-group",
            "--no-create-home",
            "--home-dir",
            FRONT_HOME,
            "--shell",
            "/usr/sbin/nologin",
            FRONT_USER,
        ]


# ---------------------------------------------------------------------------------------------------------------
def _forbidden(facts: OsFacts, uid: int, person_uid: int) -> str | None:
    """Why ``uid`` can never be the front (``None``: nothing against it)."""
    if uid == 0:
        return "the front must not be root"
    if uid == person_uid:
        return "the front must not be the person who runs orch"
    other = facts.user_by_uid(uid)
    if other is not None and other.name.startswith((AGENT_PREFIX, SLOT_PREFIX)):
        return f"the front must not be the agent or add-on user '{other.name}'"
    return None


def front_user_problem(facts: OsFacts, person_uid: int) -> str | None:
    """Whether an existing ``orch-front`` can be adopted (``None``: yes, or it does not exist). Nothing is changed on
    a user that differs; the person fixes or removes it by hand."""
    u = facts.user(FRONT_USER)
    if u is None:
        return None
    bad = _forbidden(facts, u.uid, person_uid)
    if bad:
        return f"The existing user '{FRONT_USER}' cannot be used: {bad}."
    g = facts.group(FRONT_GROUP)
    if u.uid > _SYSTEM_UID_MAX:
        why = "it is a login-range user, not a system user"
    elif u.shell not in _NOLOGIN:
        why = "it can log in"
    elif g is None or g.gid != u.gid:
        why = f"its main group is not '{FRONT_GROUP}'"
    elif u.groups - {FRONT_GROUP} or u.gids - {u.gid}:
        why = "it is in other groups"
    elif set(g.members) - {FRONT_USER}:
        why = f"others are members of the group '{FRONT_GROUP}'"
    else:
        return None
    return f"The existing user '{FRONT_USER}' cannot be used: {why}. Fix or remove it and run this again."


def _caddy_problem(facts: OsFacts) -> str | None:
    st = facts.stat(CADDY_BIN)
    if st is None:
        return (
            f"Caddy 2.7 or newer is not installed at {CADDY_BIN}. Install the caddy package (see "
            "caddyserver.com/docs/install) and run this again. Do not start its own caddy.service."
        )
    if st.is_symlink or st.is_dir or st.uid != 0 or st.mode & 0o022:
        return f"{CADDY_BIN} must be a plain file owned by root that others cannot write."
    node = "/usr/bin"
    while True:
        up = facts.stat(node)
        if up is None or up.is_symlink or up.uid != 0 or up.mode & 0o022:
            return f"{node} is not owned by root or can be written by others; root will not run Caddy from there."
        parent = node.rsplit("/", 1)[0] or "/"
        if node == "/":
            return None
        node = parent


def _ours(text: str | None) -> bool:
    return text is not None and text.startswith(MARKER)


def apply_enable(facts: OsFacts, ex: Executor, plan: FrontPlan, *, is_root: bool) -> ApplyResult:
    dry = bool(getattr(ex, "dry", False))
    res = ApplyResult(dry_run=dry)
    if facts.platform != "linux":
        return res.fail("The HTTPS front is set up on Linux only for now; nothing was changed.")
    if not is_root:
        return res.fail("orch-os-setup must run as root (use sudo). It creates a system user and a service.")
    bad = plan.problem()
    if bad:
        return res.fail(f"{bad}. Nothing was changed.")
    person = facts.user(plan.person)
    if person is None or person.uid != plan.person_uid or person.uid == 0:
        return res.fail(f"There is no normal user '{plan.person}' with id {plan.person_uid}. Nothing was changed.")
    bad = front_user_problem(facts, plan.person_uid) or _caddy_problem(facts)
    if bad:
        return res.fail(bad)
    st = _Apply(facts, ex, res)
    if ex.query([BIN["systemctl"], "is-active", "--quiet", "caddy.service"]).rc == 0:
        return res.fail(
            "The system's own caddy.service is running and would take ports 80 and 443. Stop and disable it "
            "(systemctl disable --now caddy) and run this again; orch runs its own Caddy as 'orch-front'."
        )
    old = ex.read_file(CADDYFILE)
    if old is not None:
        if not _ours(old):
            return res.fail(f"{CADDYFILE} exists and was not made by orch; setup will not overwrite it.")
        mine = _PERSON_LINE.search(old)
        if mine is None or int(mine.group(1)) != plan.person_uid:
            return res.fail(
                "Remote access is already set up for another user of this server. Run orch remote disable as that "
                "user first."
            )
    before = (old, ex.read_file(UNIT_PATH))
    # --- the user ---------------------------------------------------------------------------------------------
    st.root_dir("/etc/orch")
    st.root_dir(CONF_DIR)
    if not res.ok:
        return res
    if facts.user(FRONT_USER) is None:
        st.run(
            [BIN["useradd"], *plan.user_args()],
            f"create the system user '{FRONT_USER}' that runs the HTTPS front (no login, no home to log in to)",
        )
        if not res.ok:
            return res
        st.run([BIN["usermod"], "--lock", FRONT_USER])
        if not res.ok:
            return res
        if not dry:
            created = facts.user(FRONT_USER)
            bad = (
                f"'{FRONT_USER}' does not exist after useradd."
                if created is None
                else _forbidden(facts, created.uid, plan.person_uid) or front_user_problem(facts, plan.person_uid)
            )
            if bad:
                return res.fail(bad)
    # --- files ------------------------------------------------------------------------------------------------
    for path, text, mode in (
        (TMPFILES_FRONT, plan.tmpfiles(), 0o644),
        (CADDYFILE, plan.caddyfile(), 0o644),
        (UNIT_PATH, plan.unit(), 0o644),
    ):
        existing = ex.read_file(path)
        if existing is not None and not _ours(existing) and existing != text:
            return res.fail(f"{path} exists and was not made by orch; setup will not overwrite it.")
        st.put(path, text, mode, replace=True)
        if not res.ok:
            return res
    st.run(
        [BIN["tmpfiles"], "--create", TMPFILES_FRONT],
        f"create {plan.socket_dir} (the person's, group {FRONT_GROUP}, 0750)",
    )
    if not res.ok:
        return res
    if not dry:
        made = facts.stat(plan.socket_dir)
        grp_ = facts.group(FRONT_GROUP)
        if (
            made is None
            or not made.is_dir
            or made.is_symlink
            or made.uid != plan.person_uid
            or grp_ is None
            or made.gid != grp_.gid
            or made.mode & 0o7777 != 0o750
        ):
            return res.fail(
                f"{plan.socket_dir} is not the person's directory in group {FRONT_GROUP} with mode 0750 after "
                "systemd-tmpfiles; the front is not started."
            )
    st.run([BIN["systemctl"], "daemon-reload"])
    if not res.ok:
        return res
    running = ex.query([BIN["systemctl"], "is-active", "--quiet", UNIT_NAME]).rc == 0
    changed = not running or before != (plan.caddyfile(), plan.unit())  # a running front picks up new files by restart
    st.run([BIN["systemctl"], "enable", UNIT_NAME])
    if not res.ok:
        return res
    st.run([BIN["systemctl"], "restart" if changed else "start", UNIT_NAME], f"start {UNIT_NAME}")
    if not res.ok:
        return res
    if not dry and ex.query([BIN["systemctl"], "is-active", "--quiet", UNIT_NAME]).rc != 0:
        return res.fail(f"{UNIT_NAME} did not stay up. Look at: journalctl -u orch-front -n 50")
    res.notes.append(
        "ports 80 and 443 must be open to the internet for the certificate; the dashboard must be running to answer"
    )
    return res


def apply_disable(facts: OsFacts, ex: Executor, *, is_root: bool) -> ApplyResult:
    """Stop and remove the front. The user ``orch-front`` and its certificates stay (re-enabling must not ask the
    certificate authority again); a file that orch did not make is never removed."""
    dry = bool(getattr(ex, "dry", False))
    res = ApplyResult(dry_run=dry)
    if facts.platform != "linux":
        return res.fail("The HTTPS front is set up on Linux only for now; nothing was changed.")
    if not is_root:
        return res.fail("orch-os-setup must run as root (use sudo). Nothing was changed.")
    st = _Apply(facts, ex, res)
    unit_text = ex.read_file(UNIT_PATH)
    if unit_text is not None and not _ours(unit_text):
        return res.fail(f"{UNIT_PATH} was not made by orch; setup will not touch it.")
    if unit_text is not None:
        res.actions.append(f"{BIN['systemctl']} disable --now {UNIT_NAME}")
        out = ex.run([BIN["systemctl"], "disable", "--now", UNIT_NAME])
        if out.rc != 0 and ex.query([BIN["systemctl"], "is-active", "--quiet", UNIT_NAME]).rc == 0:
            return res.fail(f"{UNIT_NAME} could not be stopped; stop it by hand: systemctl stop {UNIT_NAME}")
    removed = False
    for path in (UNIT_PATH, CADDYFILE, TMPFILES_FRONT):
        text = ex.read_file(path)
        if text is None:
            continue
        if not _ours(text):
            return res.fail(f"{path} was not made by orch; setup will not remove it.")
        res.actions.append(f"remove {path}")
        ex.remove(path)
        removed = removed or path == UNIT_PATH
    if removed:
        st.run([BIN["systemctl"], "daemon-reload"])
    res.notes.append(f"the user '{FRONT_USER}' and its certificates are kept (so enabling again is quick)")
    return res


def run(args, facts: OsFacts, ex: Executor) -> ApplyResult:
    """Called by ``orch-os-setup`` for ``--front-enable`` / ``--front-disable`` (arguments already parsed)."""
    if args.front_disable:
        return apply_disable(facts, ex, is_root=True)
    user = facts.user(args.person)
    if user is None:
        return ApplyResult(dry_run=bool(getattr(ex, "dry", False))).fail(
            f"There is no user named '{args.person}'. Nothing was changed."
        )
    plan = FrontPlan(args.name or "", args.person, user.uid, tuple(valid_proxy(p) or p for p in args.trusted_proxy))
    return apply_enable(facts, ex, plan, is_root=True)
