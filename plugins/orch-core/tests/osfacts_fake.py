"""A fake ``OsFacts`` for the OS-user plan and isolation tests: a dict world, no real users, no real files."""

from __future__ import annotations

import stat

from orch.setup.osfacts import GroupInfo, PathInfo, UnitState, UserInfo


class FakeOsFacts:
    def __init__(self, platform: str = "linux") -> None:
        self.platform = platform
        self.users: dict[str, UserInfo] = {}
        self.groups: dict[str, GroupInfo] = {}
        self.paths: dict[str, PathInfo] = {}
        self.readable: dict[tuple[int, str], bool] = {}
        self.pins: dict[int, dict] = {}
        self.units: dict[str, UnitState] = {}
        self.connectable: dict[tuple[int, str], bool] = {}
        self.sigs: dict[str, tuple[int, int, int]] = {}
        self.sudo_answer: bool | None = False  # sudo -l -U: no rule
        self.sshd_answer: bool | None = True  # sshd -T -C: denied

    def add_user(self, name, uid, *, gid=None, home="/home/x", shell="/bin/bash", groups=(), extra_gids=()) -> None:
        gid = uid if gid is None else gid
        self.groups.setdefault(name, GroupInfo(name, gid, frozenset()))
        gids = {gid, *extra_gids}
        for g in groups:  # extra groups by name: reuse the group's gid or invent a fresh one
            if g not in self.groups:
                self.groups[g] = GroupInfo(g, 5000 + len(self.groups), frozenset())
            gids.add(self.groups[g].gid)
        self.users[name] = UserInfo(name, uid, gid, home, shell, frozenset({name, *groups}), frozenset(gids))

    def add_path(self, path, *, uid=0, gid=0, mode=0o755, kind="file", acl=False) -> None:
        t = {"dir": stat.S_IFDIR, "sock": stat.S_IFSOCK, "link": stat.S_IFLNK, "file": stat.S_IFREG}[kind]
        self.paths[path] = PathInfo(uid, gid, t | mode, kind == "dir", kind == "sock", kind == "link", acl)

    def user(self, name):
        return self.users.get(name)

    def user_by_uid(self, uid):
        return next((u for u in self.users.values() if u.uid == uid), None)

    def group(self, name):
        return self.groups.get(name)

    def stat(self, path):
        return self.paths.get(path)

    def listdir(self, path):
        pre = path.rstrip("/") + "/"
        return sorted(p[len(pre) :] for p in self.paths if p.startswith(pre) and "/" not in p[len(pre) :])

    def can_read(self, uid, path):
        return self.readable.get((uid, path), True)  # unknown reads as readable: fail closed

    def pin(self, uid):
        return self.pins.get(uid)

    def unit_state(self, unit):
        return self.units.get(unit, UnitState("unknown"))

    def can_connect(self, uid, path):
        return self.connectable.get((uid, path), False)

    def sudo_allows(self, user):
        return self.sudo_answer

    def sshd_denies(self, user, host, addr):
        return self.sshd_answer

    def file_sig(self, path):
        return self.sigs.get(path)
