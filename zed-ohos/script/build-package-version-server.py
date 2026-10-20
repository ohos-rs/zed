#!/usr/bin/env python3
"""Cross-compile Zed's package version language server for OpenHarmony."""

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tomllib
import urllib.request


VERSION = "0.0.10"
COMMIT = "4d9cad808364a048e0115b5d0eba309bf69e238b"
ARCHIVE_URL = f"https://codeload.github.com/zed-industries/package-version-server/tar.gz/{COMMIT}"
ARCHIVE_SHA256 = "7912aa88288ef7d4d03ca1d4aa5b9a4a8e01dc61dbc9b0722e6cea4086004e80"
TARGET = "aarch64-unknown-linux-ohos"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--workdir", type=Path,
                        default=Path("target/native-tools/package-version-server-build"))
    parser.add_argument("--git-workdir", type=Path, default=Path("target/native-tools/git-build"),
                        help="build-git.py output containing the static OpenSSL dependency")
    args = parser.parse_args()
    root = args.workdir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    dependencies = args.git_workdir.resolve() / "dependencies"
    openssl_license = args.git_workdir.resolve() / "zed-git/licenses/openssl.txt"
    for required in (dependencies / "lib/libssl.a", dependencies / "lib/libcrypto.a",
                     dependencies / "include/openssl/ssl.h", openssl_license):
        if not required.is_file():
            raise ValueError(f"Run build-git.py first; missing {required}")

    archive = root / f"{COMMIT}.tar.gz"
    if not archive.exists():
        partial = archive.with_suffix(".partial")
        with urllib.request.urlopen(ARCHIVE_URL, timeout=60) as source, partial.open("wb") as output:
            shutil.copyfileobj(source, output)
        partial.replace(archive)
    with archive.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != ARCHIVE_SHA256:
            raise ValueError(f"Unexpected package-version-server source digest: {archive}")
    source_root = root / f"package-version-server-{COMMIT}"
    if not source_root.exists():
        with tarfile.open(archive) as source:
            source.extractall(root, filter="data")
    manifest_path = source_root / "Cargo.toml"
    manifest = manifest_path.read_text()
    if tomllib.loads(manifest)["package"]["version"] != VERSION:
        raise ValueError(f"Expected package-version-server {VERSION}")
    # The downloaded crate lives inside Zed's workspace but builds independently.
    if "workspace" not in tomllib.loads(manifest):
        manifest_path.write_text(manifest + "\n[workspace]\n")

    entry_path = source_root / "src/main.rs"
    entry = entry_path.read_text()
    original = "#[tokio::main]\nasync fn main() {"
    replacement = """fn main() {
    // OHOS's CA bundle is outside openssl-probe's known paths. Configure it before
    // Tokio starts worker threads, preserving explicit trust-store overrides.
    #[cfg(target_env = "ohos")]
    if std::env::var_os("SSL_CERT_FILE").is_none()
        && std::env::var_os("SSL_CERT_DIR").is_none()
    {
        std::env::set_var("SSL_CERT_FILE", "/etc/ssl/certs/cacert.pem");
    }
    run_server();
}

#[tokio::main]
async fn run_server() {"""
    if entry.count(original) == 1:
        entry_path.write_text(entry.replace(original, replacement))
    elif original in entry or replacement not in entry:
        raise ValueError("Upstream server entry changed; review its OHOS certificate setup")

    native = args.sdk.resolve() / "native"
    compiler = native / "llvm/bin/clang"
    environment = os.environ | {
        "CC_aarch64_unknown_linux_ohos": str(compiler),
        "CFLAGS_aarch64_unknown_linux_ohos": "--target=aarch64-linux-ohos",
        "AR_aarch64_unknown_linux_ohos": str(native / "llvm/bin/llvm-ar"),
        "CARGO_TARGET_AARCH64_UNKNOWN_LINUX_OHOS_LINKER": str(compiler),
        "CARGO_TARGET_AARCH64_UNKNOWN_LINUX_OHOS_RUSTFLAGS": "-C link-arg=--target=aarch64-linux-ohos",
        "CARGO_TARGET_DIR": str(root / "target"),
        "OPENSSL_DIR": str(dependencies),
        "OPENSSL_STATIC": "1",
    }
    subprocess.run(["cargo", "build", "--release", "--locked", "--target", TARGET],
                   cwd=source_root, env=environment, check=True)
    destination = root / "zed-package-version-server"
    (destination / "bin").mkdir(parents=True, exist_ok=True)
    executable = destination / "bin/package-version-server"
    subprocess.run([str(native / "llvm/bin/llvm-strip"), "--strip-unneeded", "-o", str(executable),
                    str(root / "target" / TARGET / "release/package-version-server")], check=True)
    executable.chmod(0o755)
    licenses = destination / "licenses"
    licenses.mkdir(exist_ok=True)
    shutil.copyfile(source_root / "README.md", licenses / "package-version-server.txt")
    shutil.copyfile(source_root / "Cargo.lock", licenses / "Cargo.lock")
    shutil.copyfile(openssl_license, licenses / "openssl.txt")
    (licenses / "sources.txt").write_text(
        f"package-version-server {VERSION}\nCommit: {COMMIT}\n"
        f"Source: {ARCHIVE_URL}\nSHA256: {ARCHIVE_SHA256}\n"
        "License: MIT (declared in the upstream Cargo.toml and README.md)\n"
        "Build changes: isolate the Cargo workspace and select the OHOS system CA bundle\n"
        "before starting Tokio when SSL_CERT_FILE and SSL_CERT_DIR are unset.\n"
        "OpenSSL: static library from build-git.py; license included.\n"
    )
    (destination / "version.txt").write_text(VERSION + "\n")
    print(destination)


if __name__ == "__main__":
    main()
