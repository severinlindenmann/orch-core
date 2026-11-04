"""W35: the computer is shown by its friendly name, never the raw host name."""

from __future__ import annotations

from pathlib import Path

from orch.setup import computer


def test_macos_uses_the_sharing_computer_name():
    calls = []

    def run(argv, timeout):
        calls.append((argv, timeout))
        return "Lume's MacBook\n"

    assert computer.friendly_name("darwin", run=run, read=lambda p: None) == "Lume's MacBook"
    assert calls == [(["/usr/sbin/scutil", "--get", "ComputerName"], 2.0)]


def test_macos_never_returns_a_dot_local_name_and_falls_back_on_error():
    assert computer.friendly_name("darwin", run=lambda a, t: "lumes-Virtual-Machine.local", read=lambda p: None) is None

    def boom(argv, timeout):
        raise OSError("no scutil")

    assert computer.friendly_name("darwin", run=boom, read=lambda p: None) is None
    assert computer.friendly_name("darwin", run=lambda a, t: "  ", read=lambda p: None) is None


def test_linux_uses_pretty_hostname_from_machine_info():
    text = 'CHASSIS=vm\nPRETTY_HOSTNAME="Office \\"Box\\""\nICON_NAME=x\n'
    seen = []

    def read(path):
        seen.append(path)
        return text

    name = computer.friendly_name("linux", run=lambda a, t: "", read=read)
    assert name == 'Office "Box"'
    assert seen == [Path("/etc/machine-info")]


def test_linux_without_pretty_hostname_has_no_name():
    assert computer.friendly_name("linux", run=lambda a, t: "", read=lambda p: "CHASSIS=vm\n") is None
    assert computer.friendly_name("linux", run=lambda a, t: "", read=lambda p: None) is None


def test_name_is_trimmed_and_cleaned():
    assert (
        computer.friendly_name("darwin", run=lambda a, t: "A\x00B\x1b[0m" + "x" * 200, read=lambda p: None)
        == "AB[0m" + "x" * 75
    )


def test_c1_and_bidi_controls_are_stripped():
    assert (
        computer.friendly_name("darwin", run=lambda a, t: "Lu\u0085me\u202eevil\u200b\u2066x", read=lambda p: None)
        == "Lumeevilx"
    )
