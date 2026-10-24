"""A fake ``OsFacts`` for the OS-user plan and isolation tests: a dict world, no real users, no real files."""

from __future__ import annotations

import stat

from orch.setup.osfacts import GroupInfo, PathInfo, UserInfo


class FakeOsFacts:
    def __init__(self, platform: str = "linux") -> None:
        self.platform = platform
        self.users: dict[str, UserInfo] = {}
        self.groups: dict[str, GroupInfo] = {}
        self.paths: dict[str, PathInfo] = {}
        self.readable: dict[tuple[int, str], bool] = {}
        self.pins: dict[int, dict] = {}

    def add_user(self, name, uid, *, gid=None, home="/home/x", shell="/bin/bash", groups=()) -> None:
        gid = uid if gid is None else gid
        self.users[name] = UserInfo(name, uid, gid, home, shell, frozenset({name, *groups}))
        self.groups.setdefault(name, GroupInfo(name, gid, frozenset()))

    def add_path(self, path, *, uid=0, gid=0, mode=0o755, kind="file") -> None:
        t = {"dir": stat.S_IFDIR, "sock": stat.S_IFSOCK, "link": stat.S_IFLNK, "file": stat.S_IFREG}[kind]
        self.paths[path] = PathInfo(uid, gid, t | mode, kind == "dir", kind == "sock", kind == "link")

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
