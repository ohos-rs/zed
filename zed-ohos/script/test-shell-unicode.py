#!/usr/bin/env python3
"""Exercise the packaged mksh Unicode parsing and real PTY line editing."""

import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import termios
import time


PROMPT = b"__ZED_UNICODE__ "


def interactive(shell: Path, initial: str, edits: bytes, expected: str):
    child, descriptor = pty.fork()
    if child == 0:
        environment = os.environ | {
            "ENV": "/dev/null", "PS1": PROMPT.decode(), "TERM": "xterm-256color",
            "LC_ALL": "en_US.UTF-8", "ASAN_OPTIONS": "detect_leaks=0",
        }
        os.execve(shell, [str(shell), "-U", "-o", "emacs", "-i"], environment)
    transcript = bytearray()

    def receive(predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate(bytes(transcript)):
                return
            readable, _, _ = select.select([descriptor], [], [], 0.1)
            if readable:
                try:
                    data = os.read(descriptor, 65536)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                if not data:
                    break
                transcript.extend(data)
        raise AssertionError("PTY did not reach the expected state: " + repr(bytes(transcript)))

    try:
        fcntl.ioctl(descriptor, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        receive(lambda output: output.endswith(PROMPT))
        os.write(descriptor, initial.encode() + edits)
        os.write(descriptor, b"\x01print -r -- '\x05'\r")
        result = b"\r\n" + expected.encode() + b"\r\n"
        receive(lambda output: result in output and output.endswith(PROMPT))
        assert b"AddressSanitizer" not in transcript, bytes(transcript)
        return transcript.decode(errors="replace")
    finally:
        os.close(descriptor)
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(child, 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shell", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    shell = arguments.shell.resolve()
    results = []

    def check(name, callback):
        try:
            detail = callback()
            results.append({"name": name, "passed": True, "detail": detail})
        except (AssertionError, OSError, subprocess.SubprocessError) as error:
            results.append({"name": name, "passed": False, "error": str(error)})

    def command(source, expected):
        result = subprocess.run([str(shell), "-U", "-c", source], capture_output=True,
                                timeout=10, env=os.environ | {"ASAN_OPTIONS": "detect_leaks=0"})
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        assert result.stdout == expected.encode(), repr(result.stdout)
        assert b"AddressSanitizer" not in result.stderr, result.stderr.decode(errors="replace")

    check("ANSI supplementary escape", lambda: command("print -r -- $'\\U0001F44B'", "👋\n"))
    check("print supplementary escape", lambda: command("print '\\U0001F44B'", "👋\n"))
    check("print arithmetic character", lambda: command("print -A 128075", "👋\n"))
    check("length and substring", lambda: command("value='a👋z'; print -r -- \"${#value}|${value:1:1}\"", "3|👋\n"))
    check("maximum Unicode scalar", lambda: command("print -r -- $'\\U0010FFFF'", "\U0010ffff\n"))
    check("out-of-range Unicode escape", lambda: command("print -r -- $'\\U00110000'", "�\n"))
    for name, initial, edits, expected in [
        ("ASCII backward delete", "abz", b"\x1b[D\x7f", "az"),
        ("BMP backward delete", "a你z", b"\x1b[D\x7f", "az"),
        ("emoji backward delete", "a👋z", b"\x1b[D\x7f", "az"),
        ("emoji forward delete", "a👋z", b"\x01\x1b[C\x1b[3~", "az"),
        ("emoji cursor movement", "a👋z", b"\x1b[D\x1b[DX", "aX👋z"),
        ("supplementary CJK cursor movement", "a𠀀z", b"\x1b[D\x1b[DX", "aX𠀀z"),
        ("supplementary narrow cursor movement", "a𝄞z", b"\x1b[D\x1b[DX", "aX𝄞z"),
        ("emoji transpose", "a👋z", b"\x14", "az👋"),
        ("emoji forward character search", "a👋z", b"\x01\x1d" + "👋".encode() + b"X", "aX👋z"),
        ("emoji backward character search", "a👋z", b"\x1b\x1d" + "👋".encode() + b"X", "aX👋z"),
        ("long line supplementary delete", "a" * 120 + "👋z", b"\x1b[D\x7f", "a" * 120 + "z"),
        ("out-of-range input preserves following ASCII", "a", b"\xf4\x90\x80\x80z", "az"),
        ("truncated input preserves following ASCII", "a", b"\xf0\x9fz", "az"),
    ]:
        check(name, lambda initial=initial, edits=edits, expected=expected:
              interactive(shell, initial, edits, expected))
    arguments.output.write_text(json.dumps({"shell": str(shell), "results": results},
                                          ensure_ascii=False, indent=2) + "\n")
    failures = [result for result in results if not result["passed"]]
    print(f"{len(results) - len(failures)}/{len(results)} shell Unicode checks passed")
    for failure in failures:
        print(failure["name"] + ": " + failure["error"][:600])
    raise SystemExit(bool(failures))


if __name__ == "__main__":
    main()
