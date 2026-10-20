#!/usr/bin/env python3
"""Build native Rustdoc against the pinned official OpenHarmony compiler."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import tarfile


COMMIT = "48a229ceaefd4985c50990b14116b6d856af0985"
SOURCE_URL = f"https://codeload.github.com/rust-lang/rust/tar.gz/{COMMIT}"
SOURCE_SHA256 = "50ac07d25365f6681bae413743695e35b2c35bf7d45dc9a3749d5f7549b0f31d"
LOCK_SHA256 = "3f627eca6b15297c32f101922b1b856a7abb422e74006ec568a429ff939f6d7e"
HOST = "x86_64-unknown-linux-gnu"
PACKAGE_VERSION = "1.98.1-rustdoc.2"


def load_toolchain_builder():
    path = Path(__file__).with_name("build-rust-toolchain.py")
    specification = importlib.util.spec_from_file_location("rust_toolchain", path)
    if specification is None or specification.loader is None:
        raise ValueError(f"Cannot load Rust toolchain builder: {path}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def extract_sources(archive_path, destination, digest):
    prefixes = ("src/librustdoc", "src/rustdoc-json-types", "src/tools/rustdoc")
    licenses = ("COPYRIGHT", "LICENSE-APACHE", "LICENSE-MIT")
    with tarfile.open(archive_path) as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"Invalid Rustdoc source path: {member.name}")
            relative = PurePosixPath(*path.parts[1:])
            selected = any(relative.is_relative_to(prefix) for prefix in prefixes)
            if not selected and str(relative) not in licenses:
                continue
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError(f"Unsupported Rustdoc source: {member.name}")
            content = archive.extractfile(member)
            if content is None:
                raise ValueError(f"Cannot read Rustdoc source: {member.name}")
            target = destination / str(relative)
            with content:
                expected = content.read()
            if target.exists():
                if digest(target) != hashlib.sha256(expected).hexdigest():
                    raise ValueError(f"Modified Rustdoc source: {target}")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(expected)
    (destination / "Cargo.toml").write_text(
        '[workspace]\nresolver = "2"\n'
        'members = ["src/tools/rustdoc", "src/librustdoc", "src/rustdoc-json-types"]\n'
        '\n[profile.release]\ndebug = false\n'
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--linker", type=Path,
                        help="Linux OHOS clang when the SDK contains another host's tools")
    parser.add_argument("--rust-runtime", type=Path, required=True,
                        help="Unmodified installation from build-rust-toolchain.py")
    parser.add_argument("--workdir", type=Path,
                        default=Path("target/native-tools/rustdoc-build"))
    parser.add_argument("--archives", type=Path)
    parser.add_argument("--jobs", type=int, default=4)
    arguments = parser.parse_args()
    # Official target metadata records the Linux build host's proc-macro hashes.
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("This compiler's rustc-dev metadata requires an x86_64 Linux build host")
    if arguments.jobs < 1:
        raise ValueError("The build requires a positive job count")
    toolchain = load_toolchain_builder()
    root = arguments.workdir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    archives = arguments.archives.resolve() if arguments.archives else root / "archives"
    archives.mkdir(parents=True, exist_ok=True)
    native = arguments.sdk.resolve() / "native"
    compiler = arguments.linker.resolve() if arguments.linker else native / "llvm/bin/clang"
    reader = compiler.parent / "llvm-readelf"
    stripper = compiler.parent / "llvm-strip"
    for path in (compiler, reader, stripper, native / "sysroot"):
        if not path.exists():
            raise ValueError(f"Missing OHOS build input: {path}")
    runtime = arguments.rust_runtime.resolve()
    metadata = json.loads((runtime.parent / "runtime.json").read_text())
    if metadata["version"] != toolchain.VERSION or metadata["target"] != toolchain.TARGET:
        raise ValueError("Rustdoc requires the matching official Rust runtime")
    for filename, expected in metadata["files"].items():
        path = runtime / filename
        if not path.resolve().is_relative_to(runtime) or toolchain.digest(path) != expected:
            raise ValueError(f"Modified Rust runtime input: {path}")
    if toolchain.digest(native / "llvm/lib/aarch64-linux-ohos/libc++_shared.so") != metadata[
        "files"
    ]["lib/libc++_shared.so"]:
        raise ValueError("Rustdoc's SDK C++ runtime differs from the packaged compiler runtime")

    manifest_path = archives / f"channel-rust-{toolchain.VERSION}.toml"
    toolchain.download(toolchain.MANIFEST_URL, manifest_path, toolchain.MANIFEST_SHA256)
    manifest = toolchain.tomllib.loads(manifest_path.read_text())
    compiler_version = manifest["pkg"]["rustc"]["version"]
    if not compiler_version.startswith(f"{toolchain.VERSION} ({COMMIT[:9]} "):
        raise ValueError("Rustdoc source differs from the pinned compiler revision")
    sysroot = root / "sysroot"
    build_toolchain = root / "linux-toolchain"
    components = []
    for name, target, destination in (
        ("rustc-dev", toolchain.TARGET, sysroot),
        ("rustc-dev", HOST, sysroot),
        ("rustc", HOST, build_toolchain),
        ("cargo", HOST, build_toolchain),
        ("rust-std", HOST, build_toolchain),
    ):
        component = manifest["pkg"][name]["target"][target]
        if not component["available"]:
            raise ValueError(f"Missing official Rust build component: {name}/{target}")
        archive = archives / component["xz_url"].rsplit("/", 1)[-1]
        toolchain.download(component["xz_url"], archive, component["xz_hash"])
        component_name = f"rust-std-{target}" if name == "rust-std" else name
        destination.mkdir(parents=True, exist_ok=True)
        toolchain.install_component(archive, destination, component_name)
        components.append({"name": name, "target": target, "sha256": component["xz_hash"]})
    libraries = sysroot / "lib/rustlib" / toolchain.TARGET / "lib"
    for path in (runtime / "lib/rustlib" / toolchain.TARGET / "lib").iterdir():
        destination = libraries / path.name
        if not destination.exists():
            destination.symlink_to(path)
    for path in (runtime / "lib").glob("librustc_driver-*.so"):
        if toolchain.digest(libraries / path.name) != toolchain.digest(path):
            raise ValueError("Rustdoc's compiler metadata differs from the runtime driver")

    source_archive = archives / f"rust-{COMMIT}.tar.gz"
    toolchain.download(SOURCE_URL, source_archive, SOURCE_SHA256)
    source = root / "source"
    source.mkdir(exist_ok=True)
    extract_sources(source_archive, source, toolchain.digest)
    lockfile = Path(__file__).with_name("rustdoc-Cargo.lock")
    if toolchain.digest(lockfile) != LOCK_SHA256:
        raise ValueError("Unexpected Rustdoc dependency lockfile")
    shutil.copyfile(lockfile, source / "Cargo.lock")

    environment = os.environ | {
        "RUSTC_BOOTSTRAP": "1", "RUSTUP_AUTO_INSTALL": "0",
        "DOC_RUST_LANG_ORG_CHANNEL": toolchain.VERSION,
        "CFG_RELEASE": toolchain.VERSION,
        "CFG_RELEASE_NUM": toolchain.VERSION,
        "CFG_RELEASE_CHANNEL": "stable",
        "CFG_VERSION": compiler_version,
        "CFG_VER_HASH": COMMIT,
        "CFG_VER_DATE": "2026-09-01",
        "RUSTC": str(build_toolchain / "bin/rustc"),
        "RUSTDOC": str(build_toolchain / "bin/rustdoc"),
        "CARGO_HOME": str(root / "linux-cargo-home"),
        "CARGO_BUILD_JOBS": str(arguments.jobs),
        "CARGO_TARGET_DIR": str(root / "target-linux"),
        "CARGO_TARGET_AARCH64_UNKNOWN_LINUX_OHOS_LINKER": str(compiler),
        "CC_aarch64_unknown_linux_ohos": str(compiler),
        "CFLAGS_aarch64_unknown_linux_ohos": "--target=aarch64-linux-ohos",
        "AR_aarch64_unknown_linux_ohos": str(compiler.parent / "llvm-ar"),
        "CARGO_ENCODED_RUSTFLAGS": "\x1f".join([
            "--sysroot", str(sysroot), "-C", "prefer-dynamic",
            "-L", f"native={native / 'llvm/lib/aarch64-linux-ohos'}",
            "-C", "link-arg=--target=aarch64-linux-ohos",
            "-C", "link-arg=-Wl,-rpath,$ORIGIN/../lib",
            "-Z", "dual-proc-macros", "-C", f"link-arg=--sysroot={native / 'sysroot'}",
        ]),
    }
    subprocess.run([
        str(build_toolchain / "bin/cargo"), "-Zdual-proc-macros", "build", "--release",
        "--locked", "-p", "rustdoc-tool", "--bin", "rustdoc_tool_binary",
        "--target", toolchain.TARGET,
    ], cwd=source, env=environment, check=True)

    destination = root / "zed-rust-toolchain"
    if destination == runtime:
        raise ValueError("Rustdoc output must differ from the accepted base runtime")
    shutil.copytree(runtime, destination, dirs_exist_ok=True)
    executable = destination / "bin/rustdoc"
    subprocess.run([
        str(stripper), "--strip-unneeded", "-o", str(executable),
        str(root / "target-linux" / toolchain.TARGET / "release/rustdoc_tool_binary"),
    ], check=True)
    executable.chmod(0o755)
    elf = subprocess.check_output([
        str(reader), "--file-header", "--program-headers", "--dynamic", str(executable),
    ], text=True)
    if not re.search(r"Machine:\s+AArch64\b", elf):
        raise ValueError("Rustdoc is not an ARM64 executable")
    if "interpreter: /lib/ld-musl-aarch64.so.1" not in elf:
        raise ValueError("Rustdoc does not use the OHOS dynamic linker")
    for needed in re.findall(r"\(NEEDED\).*?\[([^\]]+)\]", elf):
        if not (destination / "lib" / needed).is_file() and not (
            native / "sysroot/usr/lib/aarch64-linux-ohos" / needed
        ).is_file():
            raise ValueError(f"Unresolved Rustdoc dependency: {needed}")
    licenses = destination / "licenses/rustdoc"
    licenses.mkdir(parents=True, exist_ok=True)
    for filename in ("COPYRIGHT", "LICENSE-APACHE", "LICENSE-MIT", "Cargo.lock"):
        shutil.copyfile(source / filename, licenses / filename)
    (licenses / "sources.txt").write_text(
        f"Rustdoc {toolchain.VERSION}\nCommit: {COMMIT}\n"
        f"Source: {SOURCE_URL}\nSHA256: {SOURCE_SHA256}\n"
        f"Dependency lock SHA256: {LOCK_SHA256}\n"
        "Built from unmodified Rustdoc sources against the matching official rustc-dev.\n"
    )
    metadata["runtime"] = str(destination)
    metadata["packageVersion"] = PACKAGE_VERSION
    metadata["rustdoc"] = {
        "commit": COMMIT, "sourceSha256": SOURCE_SHA256, "lockSha256": LOCK_SHA256,
        "buildComponents": components,
        "linker": subprocess.check_output([str(compiler), "--version"], text=True),
    }
    metadata["elf"]["bin/rustdoc"] = elf
    metadata["files"] = {
        str(path.relative_to(destination)): toolchain.digest(path)
        for path in sorted(destination.rglob("*")) if path.is_file()
    }
    (root / "runtime.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(destination)


if __name__ == "__main__":
    main()
