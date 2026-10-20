#!/usr/bin/env python3
"""Build the OpenHarmony mksh runtime for Zed's application sandbox."""

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request


REVISION = "88b0a4a12f2c219c1421ce5a3b90b97b785a08ad"
ARCHIVE_SHA256 = "3b9e70fa43e8f8369ef8223f3e475279c071c442bfe22fda286f703c6904135d"
SOURCE_URL = f"https://codeload.github.com/openharmony/third_party_mksh/tar.gz/{REVISION}"
VERSION = "1.2.0"


def apply_unicode_patch(source: Path):
    patch = Path(__file__).resolve().parent / "patches/mksh-unicode.patch"
    command = ["patch", "--batch", "-p1", "-i", str(patch)]
    forward = subprocess.run(command + ["--dry-run", "--forward"], cwd=source,
                             text=True, capture_output=True)
    if forward.returncode == 0:
        subprocess.run(command + ["--forward"], cwd=source, check=True)
        return
    reverse = subprocess.run(command + ["--dry-run", "--reverse"], cwd=source,
                             text=True, capture_output=True)
    if reverse.returncode != 0:
        raise ValueError("mksh Unicode source changed; review the patch\n" +
                         forward.stdout + forward.stderr + reverse.stdout + reverse.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, default=Path("target/native-tools/shell-build"))
    args = parser.parse_args()
    root = args.workdir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    archive = root / f"mksh-{REVISION}.tar.gz"
    if not archive.exists():
        partial = archive.with_suffix(".partial")
        with urllib.request.urlopen(SOURCE_URL, timeout=60) as source, partial.open("wb") as output:
            shutil.copyfileobj(source, output)
        partial.replace(archive)
    with archive.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != ARCHIVE_SHA256:
            raise ValueError(f"Unexpected mksh archive digest: {archive}")
    source = root / f"third_party_mksh-{REVISION}"
    if not source.exists():
        with tarfile.open(archive) as contents:
            contents.extractall(root, filter="data")

    execution = source / "exec.c"
    original = "\tif (stat(fn, &sb) < 0)\n\t\t/* file does not exist */\n\t\treturn (ENOENT);"
    replacement = """\tif (stat(fn, &sb) < 0) {
\t\t/* OHOS can allow execution while denying metadata access. The
\t\t * kernel still checks the file type and execute permission at exec. */
\t\tif (errno == EACCES && mode == X_OK) {
\t\t\tif (access(fn, X_OK) == 0)
\t\t\t\treturn (0);
\t\t\treturn (errno ? errno : EACCES);
\t\t}
\t\treturn (ENOENT);
\t}"""
    content = execution.read_text()
    if replacement not in content:
        if content.count(original) != 1:
            raise ValueError("mksh command lookup changed; review the sandbox patch")
        execution.write_text(content.replace(original, replacement, 1))

    apply_unicode_patch(source)

    build = root / "build"
    build.mkdir(exist_ok=True)
    native = args.sdk.resolve() / "native"
    environment = os.environ | {
        "TARGET_OS": "Linux",
        "CC": f"{native / 'llvm/bin/clang'} --target=aarch64-linux-ohos",
        "CFLAGS": "-O2 -fPIE",
        "LDFLAGS": "-pie",
        "CPPFLAGS": "-DMKSH_ASSUME_UTF8 -DMKSH_OH_ADAPT -DMKSH_DONT_EMIT_IDSTRING",
        "LC_ALL": "C",
    }
    subprocess.run(["/bin/sh", str(source / "Build.sh"), "-r", "-j"],
                   cwd=build, env=environment, check=True)
    destination = root / "zed-shell"
    (destination / "bin").mkdir(parents=True, exist_ok=True)
    (destination / "licenses").mkdir(exist_ok=True)
    subprocess.run([str(native / "llvm/bin/llvm-strip"), "--strip-unneeded", "-o",
                    str(destination / "bin/sh"), str(build / "mksh")], check=True)
    (destination / "bin/sh").chmod(0o755)
    shutil.copyfile(source / "COPYRIGHT", destination / "licenses/mksh.txt")
    shutil.copyfile(Path(__file__), destination / "licenses/build-shell.py")
    shutil.copyfile(Path(__file__).resolve().parent / "patches/mksh-unicode.patch",
                    destination / "licenses/mksh-unicode.patch")
    (destination / "licenses/sources.txt").write_text(SOURCE_URL + "\n")
    (destination / "version.txt").write_text(VERSION + "\n")
    print(f"OpenHarmony mksh {REVISION} installed into {destination}")


if __name__ == "__main__":
    main()
