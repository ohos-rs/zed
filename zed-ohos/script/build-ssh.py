#!/usr/bin/env python3
"""Cross-compile the OpenSSH clients with the OpenHarmony SDK (Python 3.11+)."""

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request


VERSION = "10.6p1"
ARCHIVE_URL = f"https://cdn.openbsd.org/pub/OpenBSD/OpenSSH/portable/openssh-{VERSION}.tar.gz"
ARCHIVE_SHA256 = "a9dc9565dffe8640f64d863cd29a32bc4a3dbdec0566a7fc44c5d6ee767d5f39"
CLIENTS = ("ssh", "scp", "sftp", "ssh-keygen", "ssh-add", "ssh-agent", "ssh-keyscan")


def patch_source(path: Path, original: str, replacement: str):
    content = path.read_text()
    if content.count(original) == 1:
        path.write_text(content.replace(original, replacement))
        return
    if original in content or replacement not in content:
        raise ValueError(f"OpenSSH source changed; review the OHOS patch for {path.name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, default=Path("target/native-tools/ssh-build"))
    parser.add_argument("--git-workdir", type=Path, default=Path("target/native-tools/git-build"),
                        help="build-git.py output containing the static OpenSSL dependency")
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    root = args.workdir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    dependencies = args.git_workdir.resolve() / "dependencies"
    openssl_license = args.git_workdir.resolve() / "zed-git/licenses/openssl.txt"
    for required in (dependencies / "lib/libcrypto.a", dependencies / "include/openssl/ssl.h",
                     openssl_license):
        if not required.is_file():
            raise ValueError(f"Run build-git.py first; missing {required}")
    archive = root / f"openssh-{VERSION}.tar.gz"
    if not archive.exists():
        partial = archive.with_suffix(".partial")
        with urllib.request.urlopen(ARCHIVE_URL, timeout=60) as source, partial.open("wb") as output:
            shutil.copyfileobj(source, output)
        partial.replace(archive)
    with archive.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != ARCHIVE_SHA256:
            raise ValueError(f"Unexpected OpenSSH source archive digest: {archive}")
    source_root = root / f"openssh-{VERSION}"
    if not source_root.exists():
        with tarfile.open(archive) as source:
            source.extractall(root, filter="data")

    # OHOS's kernel socket header conflicts with its libc sockaddr_storage definition.
    patch_source(source_root / "openbsd-compat/port-net.c",
                 "#include <linux/if.h>", "#include <net/if.h>")
    patch_source(source_root / "defines.h",
                 '#ifndef _PATH_BSHELL\n# define _PATH_BSHELL "/bin/sh"',
                 '#undef _PATH_BSHELL\n#ifndef _PATH_BSHELL\n# define _PATH_BSHELL "/system/bin/sh"')
    # Closing libc's CLOEXEC log socket leaves its fork callback holding a stale descriptor.
    patch_source(source_root / "ssh.c", "\tclosefrom(STDERR_FILENO + 1);", """\textern int ohos_close_inherited_descriptors(int);
\tif (ohos_close_inherited_descriptors(STDERR_FILENO + 1) != 0)
\t\tfatal("close inherited descriptors: %s", strerror(errno));""")
    # Shared document storage cannot host Unix sockets.
    patch_source(source_root / "ssh-agent.c", """\tif (T_flag)
\t\tsocket_dirspec = xstrdup(_PATH_SSH_AGENT_SOCKET_TMPDIR);
\telse if (socket_dirspec == NULL && agentsocket == NULL)
\t\tsocket_dirspec = xstrdup(_PATH_SSH_AGENT_SOCKET_DIR);""", """\tif (T_flag || (socket_dirspec == NULL && agentsocket == NULL)) {
\t\tconst char *temporary_directory = getenv("TMPDIR");
\t\tif (temporary_directory == NULL || temporary_directory[0] != '/')
\t\t\ttemporary_directory = "/data/storage/el2/base/cache";
\t\txasprintf(&socket_dirspec, "shared:%s", temporary_directory);
\t}""")
    # OHOS disallows hard links to sockets; preserve OpenSSH's atomic no-clobber publication.
    patch_source(source_root / "mux.c",
                 "if (link(options.control_path, orig_control_path) != 0)",
                 "if (renameat2(AT_FDCWD, options.control_path, AT_FDCWD, orig_control_path, RENAME_NOREPLACE) != 0)")
    patch_source(source_root / "mux.c", 'fatal_f("link mux listener %s => %s: %s",',
                 'fatal_f("rename mux listener %s => %s: %s",')
    patch_source(source_root / "mux.c",
                 "\t}\n\tunlink(options.control_path);\n\tfree(options.control_path);",
                 "\t}\n\tfree(options.control_path);")

    native = args.sdk.resolve() / "native"
    destination = root / "zed-openssh"
    environment = os.environ | {
        "LC_ALL": "C", "LANG": "C",
        "CC": f"{native / 'llvm/bin/clang'} --target=aarch64-linux-ohos",
        "AR": str(native / "llvm/bin/llvm-ar"),
        "RANLIB": str(native / "llvm/bin/llvm-ranlib"),
        "CFLAGS": "-O2 -fPIE",
        "CPPFLAGS": f"-I{dependencies / 'include'}",
        "LDFLAGS": f"-L{dependencies / 'lib'} -pie -Wl,--wrap=getpwuid,--wrap=getpwnam",
    }

    def run(arguments):
        subprocess.run(arguments, cwd=source_root, env=environment, check=True)

    build_platform = subprocess.check_output(["sh", "config.guess"], cwd=source_root, text=True).strip()
    run([
        "./configure", "--host=aarch64-linux-musl", f"--build={build_platform}",
        "--prefix=/usr", "--sysconfdir=/etc/ssh", f"--with-ssl-dir={dependencies}",
        "--without-openssl-header-check", "--without-zlib-version-check", "--without-rpath",
        "--without-pam", "--without-selinux", "--without-libedit", "--without-shadow",
        "--without-security-key-builtin", "--disable-security-key", "--disable-pkcs11",
        "--with-sandbox=no", "--disable-strip",
    ])
    compatibility = source_root / "openssh-ohos.o"
    run([
        str(native / "llvm/bin/clang"), "--target=aarch64-linux-ohos", "-O2", "-fPIE",
        "-Wall", "-Wextra", "-Werror", "-c",
        str(Path(__file__).resolve().parents[1] / "src/openssh_ohos.c"), "-o", str(compatibility),
    ])
    # HNP executables are exposed through the application's inherited PATH.
    for client in CLIENTS:
        (source_root / client).unlink(missing_ok=True)
    run(["make", f"-j{args.jobs}", "SSH_PROGRAM=ssh", "ASKPASS_PROGRAM=zed-askpass",
         f"LIBS={compatibility}", *CLIENTS])
    binaries = destination / "bin"
    binaries.mkdir(parents=True, exist_ok=True)
    for client in CLIENTS:
        shutil.copyfile(source_root / client, binaries / client)
        run([str(native / "llvm/bin/llvm-strip"), "--strip-unneeded", str(binaries / client)])
        (binaries / client).chmod(0o755)
    licenses = destination / "licenses"
    licenses.mkdir(exist_ok=True)
    shutil.copyfile(source_root / "LICENCE", licenses / "openssh.txt")
    shutil.copyfile(openssl_license, licenses / "openssl.txt")
    (licenses / "sources.txt").write_text(ARCHIVE_URL + "\n")
    (destination / "version.txt").write_text(VERSION + "\n")
    print(f"OpenSSH {VERSION} clients installed into {destination}")


if __name__ == "__main__":
    main()
