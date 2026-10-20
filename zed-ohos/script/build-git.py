#!/usr/bin/env python3
"""Cross-compile the bundled Git runtime with the OpenHarmony SDK (Python 3.11+)."""

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request


GIT_VERSION = "2.56.0"
SOURCES = {
    f"git-{GIT_VERSION}": (
        f"https://www.kernel.org/pub/software/scm/git/git-{GIT_VERSION}.tar.gz",
        "826817fd3671b8565586d6d49f688298de8354ab11db829c02720e5c74b9b39f",
    ),
    "openssl-3.5.9": (
        "https://github.com/openssl/openssl/releases/download/openssl-3.5.9/openssl-3.5.9.tar.gz",
        "603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a",
    ),
    "curl-8.23.0": (
        "https://curl.se/download/curl-8.23.0.tar.gz",
        "e3de77cfa6367083c776a26647fc9f633b538c06c3baa73d931a252ac8f72b49",
    ),
}


def prepare_source(root: Path, name: str) -> Path:
    url, expected = SOURCES[name]
    archive = root / f"{name}.tar.gz"
    if not archive.exists():
        partial = archive.with_suffix(".partial")
        with urllib.request.urlopen(url, timeout=60) as source, partial.open("wb") as output:
            shutil.copyfileobj(source, output)
        partial.replace(archive)
    with archive.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != expected:
            raise ValueError(f"Unexpected source archive digest: {archive}")
    directory = root / name
    if not directory.exists():
        with tarfile.open(archive) as source:
            source.extractall(root, filter="data")
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, default=Path("target/native-tools/git-build"))
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    root = args.workdir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    native = args.sdk.resolve() / "native"
    compiler = native / "llvm/bin/clang"
    dependencies = root / "dependencies"
    destination = root / "zed-git"
    build_environment = os.environ | {"LC_ALL": "C", "LC_CTYPE": "C", "LANG": "C"}
    environment = build_environment | {
        "CC": f"{compiler} --target=aarch64-linux-ohos",
        "AR": str(native / "llvm/bin/llvm-ar"),
        "RANLIB": str(native / "llvm/bin/llvm-ranlib"),
    }

    def run(arguments, directory, env=None):
        print(f"Building in {directory.name}: {arguments[0]}", flush=True)
        subprocess.run(arguments, cwd=directory, env=env or build_environment, check=True)

    openssl = prepare_source(root, "openssl-3.5.9")
    if not (dependencies / "lib/libssl.a").exists():
        run([
            "perl", "Configure", "linux-aarch64", "no-shared", "no-module", "no-tests",
            "no-legacy", "no-engine", f"--prefix={dependencies}", "--libdir=lib",
            "--openssldir=/etc/ssl",
        ], openssl, environment)
        run(["make", f"-j{args.jobs}", "build_libs"], openssl, environment)
        run(["make", "install_dev"], openssl, environment)

    curl = prepare_source(root, "curl-8.23.0")
    curl_build = root / "curl-build"
    run([
        "cmake", "-S", str(curl), "-B", str(curl_build), "-G", "Ninja",
        f"-DCMAKE_TOOLCHAIN_FILE={native / 'build/cmake/ohos.toolchain.cmake'}",
        "-DOHOS_ARCH=arm64-v8a", "-DCMAKE_BUILD_TYPE=Release",
        f"-DCMAKE_INSTALL_PREFIX={dependencies}", "-DCMAKE_INSTALL_LIBDIR=lib",
        "-DBUILD_SHARED_LIBS=OFF", "-DBUILD_STATIC_LIBS=ON", "-DBUILD_CURL_EXE=OFF",
        "-DBUILD_TESTING=OFF", "-DBUILD_LIBCURL_DOCS=OFF", "-DENABLE_CURL_MANUAL=OFF",
        "-DHTTP_ONLY=ON", "-DCURL_USE_OPENSSL=ON", "-DCURL_USE_LIBPSL=OFF",
        "-DUSE_LIBIDN2=OFF", "-DUSE_NGHTTP2=OFF", "-DCURL_ZLIB=OFF",
        "-DCURL_BROTLI=OFF", "-DCURL_ZSTD=OFF", "-DCURL_USE_LIBSSH2=OFF",
        f"-DOPENSSL_INCLUDE_DIR={dependencies / 'include'}",
        f"-DOPENSSL_SSL_LIBRARY={dependencies / 'lib/libssl.a'}",
        f"-DOPENSSL_CRYPTO_LIBRARY={dependencies / 'lib/libcrypto.a'}",
        "-DCURL_CA_BUNDLE=/etc/ssl/certs/cacert.pem", "-DCURL_CA_PATH=/etc/ssl/certs",
    ], root)
    run(["cmake", "--build", str(curl_build), "--parallel", str(args.jobs)], root)
    run(["cmake", "--install", str(curl_build)], root)

    git = prepare_source(root, f"git-{GIT_VERSION}")
    command_source = git / "run-command.c"
    source = command_source.read_text()
    # OHOS has no pthread cancellation; its stub returns ENOSYS even when disabling it.
    for arguments, message in [
        ("PTHREAD_CANCEL_DISABLE, &as->cs", "disabling cancellation"),
        ("as->cs, NULL", "re-enabling cancellation"),
    ]:
        original = f'\tCHECK_BUG(pthread_setcancelstate({arguments}),\n\t\t"{message}");'
        replacement = f"#ifndef __OHOS__\n{original}\n#endif"
        if replacement not in source:
            if source.count(original) != 1:
                raise ValueError("Git cancellation call changed; review the OHOS patch")
            source = source.replace(original, replacement)
    command_source.write_text(source)
    makefile = git / "Makefile"
    # Generated scripts need the device shell; build-time generators run on the host.
    makefile.write_text(makefile.read_text().replace(
        "\t$(QUIET_GEN)$(SHELL_PATH)", "\t$(QUIET_GEN)$(SHELL)"
    ))
    shared_makefile = git / "shared.mak"
    shared_makefile.write_text(shared_makefile.read_text().replace(
        '$(SHELL_PATH) "$(1)/GIT-VERSION-GEN"', '$(SHELL) "$(1)/GIT-VERSION-GEN"'
    ))
    configuration = {
        "CC": environment["CC"], "AR": environment["AR"], "RANLIB": environment["RANLIB"],
        "CFLAGS": f"-O2 -I{dependencies / 'include'}",
        "LDFLAGS": f"-L{dependencies / 'lib'}",
        "CC_LD_DYNPATH": "",
        "CURL_LDFLAGS": "-lcurl -lssl -lcrypto -ldl -pthread",
        "CURLDIR": str(dependencies), "CURL_CONFIG": "false",
        "NO_GETTEXT": "YesPlease", "NO_TCLTK": "YesPlease", "NO_HOMEBREW": "YesPlease",
        "NO_PERL": "YesPlease", "NO_PYTHON": "YesPlease", "NO_EXPAT": "YesPlease",
        "NO_RUST": "YesPlease", "NO_REGEX": "YesPlease", "NEEDS_LIBICONV": "",
        "RUNTIME_PREFIX": "YesPlease", "INSTALL_SYMLINKS": "YesPlease",
        "SKIP_DASHED_BUILT_INS": "YesPlease",
        "DEFAULT_PAGER": "more",
        "SHELL_PATH": "/system/bin/sh", "prefix": str(destination),
    }
    (git / "config.mak").write_text("".join(f"{key} = {value}\n" for key, value in configuration.items()))
    make = ["make", "uname_S=Linux", "uname_M=aarch64", "uname_R=6.6", "SHELL=/bin/sh"]
    run([*make, f"-j{args.jobs}", "all"], git)
    run([*make, "install"], git)
    run([
        "rustc", "--edition=2024", "--target=aarch64-unknown-linux-ohos",
        "-C", "opt-level=2", "-C", f"linker={compiler}",
        "-C", "link-arg=--target=aarch64-linux-ohos",
        str(Path(__file__).resolve().parents[1] / "src/askpass.rs"),
        "-o", str(destination / "bin/zed-askpass"),
    ], root)
    for path in destination.rglob("*"):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as source:
                native_executable = source.read(4) == b"\x7fELF"
            if native_executable:
                run([str(native / "llvm/bin/llvm-strip"), "--strip-unneeded", str(path)], root)
    licenses = destination / "licenses"
    licenses.mkdir(exist_ok=True)
    for name, directory, filename in [
        ("git", git, "COPYING"), ("openssl", openssl, "LICENSE.txt"), ("curl", curl, "COPYING"),
    ]:
        shutil.copyfile(directory / filename, licenses / f"{name}.txt")
    (licenses / "sources.txt").write_text("\n".join(url for url, _ in SOURCES.values()) + "\n")
    print(f"Git {GIT_VERSION} installed into {destination}")


if __name__ == "__main__":
    main()
