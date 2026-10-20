#!/usr/bin/env python3
"""Cross-compile the pinned rust-analyzer language server for OpenHarmony."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request


VERSION = "2026-10-12"
COMMIT = "2ad81e72b7e978cb800dffe1bcce5c9f98032acf"
ARCHIVE_URL = f"https://codeload.github.com/rust-lang/rust-analyzer/legacy.tar.gz/{COMMIT}"
ARCHIVE_SHA256 = "fc03ff86d6b4b08c79c01ce9ea37232a7e47e0c1680ac2e1ca4a72af13f610bf"
SOURCE_DIRECTORY = "rust-lang-rust-analyzer-2ad81e7"
TARGET = "aarch64-unknown-linux-ohos"
TOOLCHAIN = "1.98.1"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--workdir", type=Path,
                        default=Path("target/native-tools/rust-analyzer-build"))
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    if args.jobs < 1:
        raise ValueError("The build requires a positive job count")
    root = args.workdir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    native = args.sdk.resolve() / "native"
    compiler = native / "llvm/bin/clang"
    if not compiler.is_file():
        raise ValueError(f"Missing OHOS SDK compiler: {compiler}")

    archive = root / f"{COMMIT}.tar.gz"
    if not archive.exists():
        partial = archive.with_suffix(".partial")
        with urllib.request.urlopen(ARCHIVE_URL, timeout=60) as source, partial.open("wb") as output:
            shutil.copyfileobj(source, output)
        partial.replace(archive)
    with archive.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != ARCHIVE_SHA256:
            raise ValueError(f"Unexpected rust-analyzer source digest: {archive}")
    source_root = root / SOURCE_DIRECTORY
    with tarfile.open(archive) as source:
        if not source_root.exists():
            source.extractall(root, filter="data")
        for member in source:
            if not member.isfile():
                continue
            extracted = root / member.name
            if not extracted.resolve().is_relative_to(source_root):
                raise ValueError(f"Source file escapes its directory: {member.name}")
            expected = source.extractfile(member)
            if expected is None:
                raise ValueError(f"Cannot read rust-analyzer source file: {member.name}")
            with expected, extracted.open("rb") as actual:
                expected_digest = hashlib.file_digest(expected, "sha256").digest()
                actual_digest = hashlib.file_digest(actual, "sha256").digest()
                if expected_digest != actual_digest:
                    raise ValueError(f"Modified rust-analyzer build input: {extracted}")

    # A source archive nested in this checkout would otherwise report Zed's Git revision.
    environment = os.environ | {
        "GIT_CEILING_DIRECTORIES": str(root),
        "CFG_RELEASE": VERSION,
        "RA_COMMIT_HASH": COMMIT,
        "RA_COMMIT_SHORT_HASH": COMMIT[:9],
        "CARGO_BUILD_JOBS": str(args.jobs),
        "CARGO_TARGET_DIR": str(root / "target"),
        "CC_aarch64_unknown_linux_ohos": str(compiler),
        "CFLAGS_aarch64_unknown_linux_ohos": "--target=aarch64-linux-ohos",
        "AR_aarch64_unknown_linux_ohos": str(native / "llvm/bin/llvm-ar"),
        "CARGO_TARGET_AARCH64_UNKNOWN_LINUX_OHOS_LINKER": str(compiler),
        "CARGO_TARGET_AARCH64_UNKNOWN_LINUX_OHOS_RUSTFLAGS": "-C link-arg=--target=aarch64-linux-ohos",
    }
    subprocess.run([
        "cargo", f"+{TOOLCHAIN}", "build", "--release", "--locked", "-p", "rust-analyzer",
        "--bin", "rust-analyzer", "--target", TARGET,
    ], cwd=source_root, env=environment, check=True)

    destination = root / "zed-rust-analyzer"
    (destination / "bin").mkdir(parents=True, exist_ok=True)
    executable = destination / "bin/rust-analyzer"
    subprocess.run([
        str(native / "llvm/bin/llvm-strip"), "--strip-unneeded", "-o", str(executable),
        str(root / "target" / TARGET / "release/rust-analyzer"),
    ], check=True)
    executable.chmod(0o755)
    elf = subprocess.check_output([
        str(native / "llvm/bin/llvm-readelf"), "--program-headers", "--dynamic", str(executable),
    ], text=True)
    if "interpreter: /lib/ld-musl-aarch64.so.1" not in elf:
        raise ValueError("rust-analyzer does not use the OHOS dynamic linker")
    licenses = destination / "licenses"
    licenses.mkdir(exist_ok=True)
    for filename in ("LICENSE-APACHE", "LICENSE-MIT", "Cargo.lock"):
        shutil.copyfile(source_root / filename, licenses / filename)
    (licenses / "sources.txt").write_text(
        f"rust-analyzer {VERSION}\nCommit: {COMMIT}\n"
        f"Source: {ARCHIVE_URL}\nSHA256: {ARCHIVE_SHA256}\n"
        f"Target: {TARGET}\nRust toolchain: {TOOLCHAIN}\n"
        "License: MIT OR Apache-2.0\n"
        "Built from unmodified upstream sources with the OHOS SDK linker.\n"
    )
    (destination / "version.txt").write_text(VERSION + "\n")
    (root / "runtime.json").write_text(json.dumps({
        "version": VERSION, "commit": COMMIT, "archiveSha256": ARCHIVE_SHA256,
        "executable": str(executable), "target": TARGET,
        "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "elf": elf,
    }, indent=2) + "\n")
    print(destination)


if __name__ == "__main__":
    main()
