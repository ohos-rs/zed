#!/usr/bin/env python3
"""Assemble the official device Clang tools and the installed OpenHarmony SDK sysroot."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request


VERSION = "15.0.4"
TARGET = "aarch64-unknown-linux-ohos"
DISTRIBUTION = "clang_ohos-arm64-02fbe8-20260908"
ARCHIVE_URL = (
    "https://repo.huaweicloud.com/openharmony/compiler/clang/15.0.4-02fbe8/"
    f"ohos_arm64/{DISTRIBUTION}.tar.gz"
)
ARCHIVE_SHA256 = "ae8b104119903945adda92210d901545706500a96d6e4fc8d5ebe58a4b8a0cb1"
COMMANDS = {
    "cc": "llvm/bin/aarch64-unknown-linux-ohos-clang",
    "clang": "llvm/bin/aarch64-unknown-linux-ohos-clang",
    "aarch64-linux-ohos-clang": "llvm/bin/aarch64-unknown-linux-ohos-clang",
    "aarch64-unknown-linux-ohos-clang": "llvm/bin/aarch64-unknown-linux-ohos-clang",
    "c++": "llvm/bin/aarch64-unknown-linux-ohos-clang++",
    "clang++": "llvm/bin/aarch64-unknown-linux-ohos-clang++",
    "aarch64-linux-ohos-clang++": "llvm/bin/aarch64-unknown-linux-ohos-clang++",
    "aarch64-unknown-linux-ohos-clang++": "llvm/bin/aarch64-unknown-linux-ohos-clang++",
    "ld.lld": "llvm/bin/ld.lld",
    "llvm-ar": "llvm/bin/llvm-ar",
    "llvm-ranlib": "llvm/bin/llvm-ar",
    "ar": "llvm/bin/llvm-ar",
    "ranlib": "llvm/bin/llvm-ar",
}


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def copy_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--workdir", type=Path,
                        default=Path("target/native-tools/clang-toolchain-build"))
    args = parser.parse_args()
    root = args.workdir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    native = args.sdk.resolve() / "native"
    readelf = native / "llvm/bin/llvm-readelf"
    if not readelf.is_file():
        raise ValueError(f"Missing SDK ELF inspector: {readelf}")
    archive_path = args.archive.resolve() if args.archive else root / f"{DISTRIBUTION}.tar.gz"
    if not archive_path.is_file():
        if args.archive:
            raise ValueError(f"Missing compiler archive: {archive_path}")
        partial = archive_path.with_suffix(".partial")
        with urllib.request.urlopen(ARCHIVE_URL, timeout=60) as source, partial.open("wb") as output:
            shutil.copyfileobj(source, output)
        partial.replace(archive_path)
    if digest(archive_path) != ARCHIVE_SHA256:
        raise ValueError(f"Unexpected compiler archive SHA256: {archive_path}")

    destination = root / "zed-clang-toolchain"
    if destination.exists():
        metadata_path = root / "runtime.json"
        if not metadata_path.is_file():
            raise ValueError(f"Existing runtime has no build manifest: {destination}")
        previous = json.loads(metadata_path.read_text())["files"]
        actual = {str(path.relative_to(destination)): digest(path)
                  for path in destination.rglob("*") if path.is_file()}
        if previous != actual:
            raise ValueError(f"Existing generated runtime was modified: {destination}")
        shutil.rmtree(destination)
    destination.mkdir()
    with tempfile.TemporaryDirectory(prefix="clang-upstream-", dir=root) as temporary:
        with tarfile.open(archive_path) as archive:
            archive.extractall(temporary, filter="data")
        upstream = Path(temporary) / DISTRIBUTION
        for source, target in (
            ("bin/clang-15", "llvm/bin/clang"),
            ("bin/clang-15", "llvm/bin/clang++"),
            ("bin/lld", "llvm/bin/ld.lld"),
            ("bin/llvm-ar", "llvm/bin/llvm-ar"),
            ("bin/aarch64-unknown-linux-ohos-clang", "llvm/bin/aarch64-unknown-linux-ohos-clang"),
            ("bin/aarch64-unknown-linux-ohos-clang++", "llvm/bin/aarch64-unknown-linux-ohos-clang++"),
            ("lib/libxml2.so.16", "llvm/lib/libxml2.so.16"),
            ("NOTICE", "licenses/clang-NOTICE"),
        ):
            copy_file(upstream / source, destination / target)
        for name in (f"lib/clang/{VERSION}/include", f"lib/clang/{VERSION}/lib/aarch64-linux-ohos",
                     "include/c++", "lib/aarch64-linux-ohos"):
            shutil.copytree(upstream / name, destination / "llvm" / name)

    for source in ("usr/include", "usr/lib/aarch64-linux-ohos"):
        shutil.copytree(native / "sysroot" / source, destination / "sysroot" / source)
    copy_file(native / "llvm/NOTICE", destination / "licenses/sdk-NOTICE")
    (destination / "version.txt").write_text(VERSION + "\n")
    (destination / "licenses/sources.txt").write_text(
        f"OpenHarmony device Clang {VERSION}: {ARCHIVE_URL}\n"
        f"SHA256: {ARCHIVE_SHA256}\n"
        "Compiler, target wrappers and runtime payloads are unmodified.\n"
        "Public native headers and ARM64 sysroot libraries are supplied by the installed SDK.\n"
    )
    reports = {}
    for path in sorted(destination.rglob("*")):
        if not path.is_file():
            continue
        with path.open("rb") as source:
            if source.read(4) != b"\x7fELF":
                continue
        report = subprocess.check_output([
            str(readelf), "--file-header", "--program-headers", "--dynamic", str(path),
        ], text=True)
        if not re.search(r"Machine:\s+AArch64\b", report):
            raise ValueError(f"Compiler runtime contains a non-ARM64 ELF: {path}")
        if "INTERP" in report and "interpreter: /lib/ld-musl-aarch64.so.1" not in report:
            raise ValueError(f"Compiler runtime contains a non-OHOS executable: {path}")
        reports[str(path.relative_to(destination))] = report
    bundled = {Path(name).name for name in reports}
    system = native / "sysroot/usr/lib/aarch64-linux-ohos"
    for name, report in reports.items():
        for needed in re.findall(r"\(NEEDED\).*?\[([^\]]+)\]", report):
            if needed not in bundled and not (system / needed).is_file():
                raise ValueError(f"Unresolved compiler runtime dependency {needed}: {name}")
    metadata = {
        "version": VERSION, "target": TARGET, "archiveUrl": ARCHIVE_URL,
        "archiveSha256": ARCHIVE_SHA256, "runtime": str(destination),
        "commands": COMMANDS, "elf": reports,
        "files": {str(path.relative_to(destination)): digest(path)
                  for path in sorted(destination.rglob("*")) if path.is_file()},
    }
    (root / "runtime.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(destination, flush=True)


if __name__ == "__main__":
    main()
