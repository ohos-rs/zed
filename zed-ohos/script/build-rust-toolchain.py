#!/usr/bin/env python3
"""Assemble official native Rust tools and their OpenHarmony runtime dependencies."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tomllib
import urllib.request


VERSION = "1.98.1"
TARGET = "aarch64-unknown-linux-ohos"
MANIFEST_URL = f"https://static.rust-lang.org/dist/channel-rust-{VERSION}.toml"
MANIFEST_SHA256 = "a7c8774a5fd8441c997d94c029776cbc5eb111e9d72ab5d256fa69866644347e"
COMPONENTS = ("rustc", "cargo", "rustfmt-preview", "rust-std", "rust-src")
OPENSSL_VERSION = "3.5.9"
OPENSSL_URL = (
    f"https://github.com/openssl/openssl/releases/download/openssl-{OPENSSL_VERSION}/"
    f"openssl-{OPENSSL_VERSION}.tar.gz"
)
OPENSSL_SHA256 = "603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a"


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def download(url, path, expected):
    if not path.exists():
        partial = path.with_suffix(".partial")
        print(f"Downloading {path.name}", flush=True)
        with urllib.request.urlopen(url, timeout=60) as source, partial.open("wb") as output:
            shutil.copyfileobj(source, output)
        partial.replace(path)
    if digest(path) != expected:
        raise ValueError(f"Unexpected archive digest: {path}")


def install_component(archive_path, destination, component):
    installed = []
    with tarfile.open(archive_path) as archive:
        roots = {PurePosixPath(member.name).parts[0] for member in archive.getmembers()}
        if len(roots) != 1:
            raise ValueError(f"Unexpected component archive layout: {archive_path}")
        archive_root = roots.pop()
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"Invalid component path: {member.name}")
            if not member.isfile():
                if not member.isdir():
                    raise ValueError(f"Unsupported component archive entry: {member.name}")
                continue
            if path.parts[:2] == (archive_root, component) and len(path.parts) > 2:
                relative = Path(*path.parts[2:])
                if relative == Path("manifest.in"):
                    continue
            elif len(path.parts) == 2 and path.name.startswith(("LICENSE", "COPYRIGHT")):
                relative = Path("licenses", component, path.name)
            else:
                continue
            content = archive.extractfile(member)
            if content is None:
                raise ValueError(f"Cannot read component archive entry: {member.name}")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with content, target.open("wb") as output:
                shutil.copyfileobj(content, output)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
            installed.append(str(relative))
    return installed


def build_openssl(root, native, archives, jobs):
    archive_path = archives / f"openssl-{OPENSSL_VERSION}.tar.gz"
    download(OPENSSL_URL, archive_path, OPENSSL_SHA256)
    source_root = root / f"openssl-{OPENSSL_VERSION}"
    with tarfile.open(archive_path) as archive:
        if not source_root.exists():
            archive.extractall(root, filter="data")
        for member in archive:
            if not member.isfile():
                continue
            target = root / member.name
            if not target.resolve().is_relative_to(source_root):
                raise ValueError(f"OpenSSL source escapes its directory: {member.name}")
            expected = archive.extractfile(member)
            if expected is None:
                raise ValueError(f"Cannot read OpenSSL source: {member.name}")
            with expected:
                if hashlib.file_digest(expected, "sha256").hexdigest() != digest(target):
                    raise ValueError(f"Modified OpenSSL source: {target}")
    environment = os.environ | {
        "LC_ALL": "C", "LC_CTYPE": "C", "LANG": "C",
        "CC": f"{native / 'llvm/bin/clang'} --target=aarch64-linux-ohos",
        "AR": str(native / "llvm/bin/llvm-ar"),
        "RANLIB": str(native / "llvm/bin/llvm-ranlib"),
    }
    subprocess.run([
        "perl", "Configure", "linux-aarch64", "shared", "no-module", "no-tests",
        "no-legacy", "--openssldir=/etc/ssl",
    ], cwd=source_root, env=environment, check=True)
    subprocess.run(["make", f"-j{jobs}", "build_libs"],
                   cwd=source_root, env=environment, check=True)
    return source_root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--workdir", type=Path,
                        default=Path("target/native-tools/rust-toolchain-build"))
    parser.add_argument("--archives", type=Path)
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    if args.jobs < 1:
        raise ValueError("The build requires a positive job count")
    root = args.workdir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    archives = args.archives.resolve() if args.archives else root / "archives"
    archives.mkdir(parents=True, exist_ok=True)
    native = args.sdk.resolve() / "native"
    readelf = native / "llvm/bin/llvm-readelf"
    if not readelf.is_file():
        raise ValueError(f"Missing OHOS SDK ELF inspector: {readelf}")
    manifest_path = archives / f"channel-rust-{VERSION}.toml"
    download(MANIFEST_URL, manifest_path, MANIFEST_SHA256)
    manifest = tomllib.loads(manifest_path.read_text())
    destination = root / "zed-rust-toolchain"
    destination.mkdir(exist_ok=True)
    components = []
    for name in COMPONENTS:
        package = manifest["pkg"][name]
        target = package["target"]["*" if name == "rust-src" else TARGET]
        if not target["available"]:
            raise ValueError(f"Official Rust component is unavailable: {name}")
        archive_path = archives / target["xz_url"].rsplit("/", 1)[1]
        download(target["xz_url"], archive_path, target["xz_hash"])
        component = f"rust-std-{TARGET}" if name == "rust-std" else name
        installed = install_component(archive_path, destination, component)
        components.append({
            "name": name, "version": package["version"], "url": target["xz_url"],
            "sha256": target["xz_hash"], "files": len(installed),
        })
        print(f"Installed {name}: {len(installed)} files", flush=True)

    openssl = build_openssl(root, native, archives, args.jobs)
    libraries = destination / "lib"
    libraries.mkdir(exist_ok=True)
    for name in ("libssl", "libcrypto"):
        source = openssl / f"{name}.so.3"
        for filename in (f"{name}.so", f"{name}.so.3"):
            subprocess.run([
                str(native / "llvm/bin/llvm-strip"), "--strip-unneeded", "-o",
                str(libraries / filename), str(source),
            ], check=True)
    shutil.copyfile(native / "llvm/lib/aarch64-linux-ohos/libc++_shared.so",
                    libraries / "libc++_shared.so")
    licenses = destination / "licenses"
    shutil.copyfile(openssl / "LICENSE.txt", licenses / "openssl.txt")
    shutil.copyfile(manifest_path, licenses / "rust-channel.toml")
    sdk_notice = native / "llvm/NOTICE"
    if not sdk_notice.is_file():
        raise ValueError(f"Missing SDK runtime notices: {sdk_notice}")
    shutil.copyfile(sdk_notice, licenses / "sdk-runtime-NOTICE")
    (destination / "version.txt").write_text(VERSION + "\n")
    (licenses / "sources.txt").write_text(
        f"Official Rust {VERSION} / {TARGET}\nManifest: {MANIFEST_URL}\n"
        f"Manifest SHA256: {MANIFEST_SHA256}\n"
        f"OpenSSL {OPENSSL_VERSION}: {OPENSSL_URL}\nSHA256: {OPENSSL_SHA256}\n"
        "Rust component payloads are unmodified. OpenSSL is built with the OHOS SDK.\n"
        "libc++_shared.so is supplied by the same SDK.\n"
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
            raise ValueError(f"Rust runtime contains a non-ARM64 ELF: {path}")
        if "INTERP" in report and "interpreter: /lib/ld-musl-aarch64.so.1" not in report:
            raise ValueError(f"Rust runtime contains a non-OHOS executable: {path}")
        for needed in re.findall(r"\(NEEDED\).*?\[([^\]]+)\]", report):
            bundled = list(libraries.glob(needed))
            system = native / "sysroot/usr/lib/aarch64-linux-ohos" / needed
            if not bundled and not system.is_file():
                raise ValueError(f"Unresolved native runtime dependency {needed}: {path}")
        reports[str(path.relative_to(destination))] = report
    metadata = {
        "version": VERSION, "target": TARGET, "manifestSha256": MANIFEST_SHA256,
        "components": components, "runtime": str(destination), "elf": reports,
        "files": {str(path.relative_to(destination)): digest(path)
                  for path in sorted(destination.rglob("*")) if path.is_file()},
    }
    (root / "runtime.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(destination, flush=True)


if __name__ == "__main__":
    main()
