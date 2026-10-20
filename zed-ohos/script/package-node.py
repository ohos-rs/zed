#!/usr/bin/env python3
"""Package Node, a shell and optional native tools into a Zed OpenHarmony HAP."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile


VERSION = "24.11.0"
ARCHIVE_NAME = f"node-v{VERSION}-openharmony-arm64.tar.gz"
ARCHIVE_SHA256 = "83e7ea2a9d6a47ae226a0e94457d9922f84aa5a4100ebce39640eeaaade12095"
ARCHIVE_URL = f"https://ohos-node.com/dist/v{VERSION}/{ARCHIVE_NAME}"
PACKAGE_NAME = "zed-node"
GIT_VERSION = "2.56.0"
SSH_VERSION = "10.6p1"
SSH_CLIENTS = ("ssh", "scp", "sftp", "ssh-keygen", "ssh-add", "ssh-agent", "ssh-keyscan")
PACKAGE_VERSION_SERVER_VERSION = "0.0.10"
RUST_ANALYZER_VERSION = "2026-10-12"
RUST_TOOLCHAIN_VERSION = "1.98.1"
CLANG_TOOLCHAIN_VERSION = "15.0.4"
SHELL_VERSION = "1.2.0"


def stage_runtime(archive_path: Path, stage: Path):
    with archive_path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != ARCHIVE_SHA256:
        raise ValueError(f"Unexpected Node archive SHA256: {digest}")

    distribution = f"node-v{VERSION}-openharmony-arm64"
    with tarfile.open(archive_path) as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or path.parts[0] != distribution:
                raise ValueError(f"Invalid archive path: {member.name}")
            relative = path.relative_to(distribution)
            if relative not in (PurePosixPath("bin/node"), PurePosixPath("LICENSE")) and not (
                relative.is_relative_to("lib/node_modules/npm")
            ):
                continue
            destination = stage / relative
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ValueError(f"Unexpected non-regular runtime file: {member.name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"Cannot read runtime file: {member.name}")
            with source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)

    for required in ("bin/node", "lib/node_modules/npm/bin/npm-cli.js", "LICENSE"):
        if not (stage / required).is_file():
            raise ValueError(f"Missing runtime file: {required}")
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "licenses" / f"node-v{VERSION}.txt",
        stage / "NODE_LICENSE",
    )
    config = {
        "type": "hnp-config",
        "name": PACKAGE_NAME,
        "version": VERSION,
        "install": {"links": [
            {"source": "/bin/node", "target": "zed-node"},
            {"source": "/bin/node", "target": "node"},
        ]},
    }
    (stage / "hnp.json").write_text(json.dumps(config, indent=2) + "\n")
    for path in (stage, *stage.rglob("*")):
        path.chmod(0o755 if path.is_dir() or path == stage / "bin/node" else 0o644)


def stage_shell_runtime(sdk: Path, stage: Path):
    project = Path(__file__).resolve().parents[1]
    workdir = project / "target/native-tools/shell-build"
    subprocess.run([
        sys.executable, str(project / "script/build-shell.py"),
        "--sdk", str(sdk), "--workdir", str(workdir),
    ], check=True)
    runtime = workdir / "zed-shell"
    if (runtime / "version.txt").read_text().strip() != SHELL_VERSION:
        raise ValueError(f"Expected shell runtime {SHELL_VERSION}")
    shutil.copytree(runtime, stage, dirs_exist_ok=True)
    executable = stage / "bin/sh"
    with executable.open("rb") as binary:
        header = binary.read(20)
    if header[:6] != b"\x7fELF\x02\x01" or header[18:20] != b"\xb7\x00":
        raise ValueError("Shell runtime must be an ARM64 ELF executable")
    configuration = {
        "type": "hnp-config", "name": "zed-shell", "version": SHELL_VERSION,
        "install": {"links": [{"source": "/bin/sh", "target": "sh"}]},
    }
    (stage / "hnp.json").write_text(json.dumps(configuration, indent=2) + "\n")
    for path in (stage, *stage.rglob("*")):
        path.chmod(0o755 if path.is_dir() or path == executable else 0o644)


def stage_git_runtime(runtime: Path, stage: Path):
    runtime = runtime.resolve()
    for directory in ("bin", "libexec/git-core", "share/git-core/templates", "licenses"):
        source_root = runtime / directory
        if not source_root.is_dir():
            raise ValueError(f"Missing Git runtime directory: {source_root}")
        for source in source_root.rglob("*"):
            if source.is_dir():
                continue
            if not source.resolve().is_relative_to(runtime):
                raise ValueError(f"Git runtime link escapes the installation: {source}")
            destination = stage / source.relative_to(runtime)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination, follow_symlinks=True)
    for executable in ("bin/git", "bin/zed-askpass", "libexec/git-core/git-remote-https"):
        with (stage / executable).open("rb") as source:
            header = source.read(20)
        if header[:5] != b"\x7fELF\x02" or header[18:20] != b"\xb7\x00":
            raise ValueError(f"Git runtime must contain ARM64 ELF executables: {executable}")
    configuration = {
        "type": "hnp-config", "name": "zed-git", "version": GIT_VERSION,
        "install": {"links": [
            {"source": "/bin/git", "target": "git"},
            {"source": "/bin/zed-askpass", "target": "zed-askpass"},
        ]},
    }
    (stage / "hnp.json").write_text(json.dumps(configuration, indent=2) + "\n")


def stage_ssh_runtime(runtime: Path, stage: Path):
    runtime = runtime.resolve()
    if (runtime / "version.txt").read_text().strip() != SSH_VERSION:
        raise ValueError(f"Expected OpenSSH {SSH_VERSION} from build-ssh.py")
    (stage / "bin").mkdir()
    for executable in SSH_CLIENTS:
        source = runtime / "bin" / executable
        if not source.resolve().is_relative_to(runtime):
            raise ValueError(f"SSH runtime link escapes the installation: {source}")
        with source.open("rb") as binary:
            header = binary.read(20)
        if header[:6] != b"\x7fELF\x02\x01" or header[18:20] != b"\xb7\x00":
            raise ValueError(f"SSH runtime must contain ARM64 ELF executables: {executable}")
        destination = stage / "bin" / executable
        shutil.copyfile(source, destination)
        destination.chmod(0o755)
    (stage / "licenses").mkdir()
    for filename in ("openssh.txt", "openssl.txt", "sources.txt"):
        source = runtime / "licenses" / filename
        if not source.resolve().is_relative_to(runtime):
            raise ValueError(f"SSH license link escapes the installation: {source}")
        shutil.copyfile(source, stage / "licenses" / filename)
    shutil.copyfile(runtime / "version.txt", stage / "version.txt")
    configuration = {
        "type": "hnp-config", "name": "zed-openssh", "version": SSH_VERSION.replace("p", "."),
        "install": {"links": [
            {"source": f"/bin/{executable}", "target": executable} for executable in SSH_CLIENTS
        ]},
    }
    (stage / "hnp.json").write_text(json.dumps(configuration, indent=2) + "\n")


def stage_package_version_server(runtime: Path, stage: Path):
    runtime = runtime.resolve()
    if (runtime / "version.txt").read_text().strip() != PACKAGE_VERSION_SERVER_VERSION:
        raise ValueError(f"Expected package-version-server {PACKAGE_VERSION_SERVER_VERSION}")
    executable = "bin/package-version-server"
    for filename in (executable, "version.txt", "licenses/package-version-server.txt",
                     "licenses/openssl.txt", "licenses/sources.txt", "licenses/Cargo.lock"):
        source = runtime / filename
        if not source.resolve().is_relative_to(runtime):
            raise ValueError(f"Language server runtime link escapes the installation: {source}")
        destination = stage / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(0o755 if filename == executable else 0o644)
    with (stage / executable).open("rb") as source:
        header = source.read(20)
    if header[:6] != b"\x7fELF\x02\x01" or header[18:20] != b"\xb7\x00":
        raise ValueError("package-version-server must be an ARM64 ELF executable")
    configuration = {
        "type": "hnp-config", "name": "zed-package-version-server",
        "version": PACKAGE_VERSION_SERVER_VERSION,
        "install": {"links": [{
            "source": "/bin/package-version-server", "target": "package-version-server",
        }]},
    }
    (stage / "hnp.json").write_text(json.dumps(configuration, indent=2) + "\n")


def stage_rust_analyzer_runtime(runtime: Path, stage: Path):
    runtime = runtime.resolve()
    if (runtime / "version.txt").read_text().strip() != RUST_ANALYZER_VERSION:
        raise ValueError(f"Expected rust-analyzer {RUST_ANALYZER_VERSION}")
    executable = "bin/rust-analyzer"
    for filename in (executable, "version.txt", "licenses/LICENSE-APACHE", "licenses/LICENSE-MIT",
                     "licenses/Cargo.lock", "licenses/sources.txt"):
        source = runtime / filename
        if not source.resolve().is_relative_to(runtime):
            raise ValueError(f"Language server runtime link escapes the installation: {source}")
        destination = stage / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(0o755 if filename == executable else 0o644)
    with (stage / executable).open("rb") as source:
        header = source.read(20)
    if header[:6] != b"\x7fELF\x02\x01" or header[18:20] != b"\xb7\x00":
        raise ValueError("rust-analyzer must be an ARM64 ELF executable")
    configuration = {
        "type": "hnp-config", "name": "zed-rust-analyzer",
        "version": RUST_ANALYZER_VERSION.replace("-", "."),
        "install": {"links": [{"source": "/bin/rust-analyzer", "target": "rust-analyzer"}]},
    }
    (stage / "hnp.json").write_text(json.dumps(configuration, indent=2) + "\n")


def stage_rust_toolchain_runtime(runtime: Path, stage: Path):
    runtime = runtime.resolve()
    metadata = json.loads((runtime.parent / "runtime.json").read_text())
    if metadata["version"] != RUST_TOOLCHAIN_VERSION:
        raise ValueError(f"Expected native Rust {RUST_TOOLCHAIN_VERSION}")
    if metadata["target"] != "aarch64-unknown-linux-ohos":
        raise ValueError("Rust tools must target OpenHarmony ARM64")
    files = metadata["files"]
    commands = ("cargo", "rustc", "rustfmt", "cargo-fmt")
    if "bin/rustdoc" in files:
        commands += ("rustdoc",)
    required = {f"bin/{command}" for command in commands}
    required.add("lib/rustlib/src/rust/library/core/src/lib.rs")
    if not required.issubset(files):
        raise ValueError("Rust runtime lacks tools or standard library sources")
    for filename, expected in files.items():
        relative = PurePosixPath(filename)
        source = runtime / filename
        if relative.is_absolute() or ".." in relative.parts or not source.resolve().is_relative_to(runtime):
            raise ValueError(f"Rust runtime file escapes the installation: {filename}")
        with source.open("rb") as content:
            if hashlib.file_digest(content, "sha256").hexdigest() != expected:
                raise ValueError(f"Modified Rust runtime file: {filename}")
        destination = stage / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)
    for command in commands:
        with (stage / "bin" / command).open("rb") as source:
            header = source.read(20)
        if header[:6] != b"\x7fELF\x02\x01" or header[18:20] != b"\xb7\x00":
            raise ValueError(f"{command} must be an ARM64 ELF executable")
    configuration = {
        "type": "hnp-config", "name": "zed-rust-toolchain",
        "version": metadata.get("packageVersion", RUST_TOOLCHAIN_VERSION),
        "install": {"links": [{"source": f"/bin/{command}", "target": command}
                              for command in commands]},
    }
    (stage / "hnp.json").write_text(json.dumps(configuration, indent=2) + "\n")


def stage_clang_toolchain_runtime(runtime: Path, stage: Path):
    metadata = json.loads((runtime.parent / "runtime.json").read_text())
    if metadata["version"] != CLANG_TOOLCHAIN_VERSION:
        raise ValueError(f"Expected native Clang {CLANG_TOOLCHAIN_VERSION}")
    if metadata["target"] != "aarch64-unknown-linux-ohos":
        raise ValueError("Clang tools must target OpenHarmony ARM64")
    files = metadata["files"]
    binaries = ("llvm/bin/clang", "llvm/bin/clang++", "llvm/bin/ld.lld", "llvm/bin/llvm-ar")
    required = {*binaries, "sysroot/usr/lib/aarch64-linux-ohos/Scrt1.o",
                "llvm/bin/aarch64-unknown-linux-ohos-clang", "licenses/clang-NOTICE"}
    if not required.issubset(files):
        raise ValueError("Clang runtime lacks the compiler, linker or sysroot")
    for filename, expected in files.items():
        relative = PurePosixPath(filename)
        source = runtime / filename
        if relative.is_absolute() or ".." in relative.parts or not source.resolve().is_relative_to(runtime):
            raise ValueError(f"Clang runtime file escapes the installation: {filename}")
        with source.open("rb") as content:
            if hashlib.file_digest(content, "sha256").hexdigest() != expected:
                raise ValueError(f"Modified Clang runtime file: {filename}")
        destination = stage / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)
    for filename in binaries:
        with (stage / filename).open("rb") as source:
            header = source.read(20)
        if header[:6] != b"\x7fELF\x02\x01" or header[18:20] != b"\xb7\x00":
            raise ValueError(f"{filename} must be an ARM64 ELF executable")
    links = []
    for command, source in metadata["commands"].items():
        if PurePosixPath(command).name != command or source not in files:
            raise ValueError(f"Invalid Clang command link: {command}")
        links.append({"source": "/" + source, "target": command})
    configuration = {"type": "hnp-config", "name": "zed-clang-toolchain",
                     "version": CLANG_TOOLCHAIN_VERSION, "install": {"links": links}}
    (stage / "hnp.json").write_text(json.dumps(configuration, indent=2) + "\n")


def normalize_archive(archive: Path, executables=None):
    if executables is None:
        executables = {f"{PACKAGE_NAME}/bin/node"}
    # macOS hnpcli can emit 0xffffffff as the Unix mode. Fix this before code signing.
    temporary = archive.with_suffix(".normalized")
    try:
        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(temporary, "w") as output:
            for entry in source.infolist():
                entry.create_system = 3
                if entry.is_dir():
                    entry.external_attr = ((stat.S_IFDIR | 0o755) << 16) | 0x10
                else:
                    mode = 0o755 if entry.filename in executables else 0o644
                    entry.external_attr = (stat.S_IFREG | mode) << 16
                output.writestr(entry, source.read(entry))
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)


def attach_runtime(hap: Path, package: Path, output: Path, package_name=PACKAGE_NAME):
    if hap.resolve() == output.resolve():
        raise ValueError("Output must differ from the input unsigned HAP")
    archive_path = f"hnp/arm64-v8a/{package_name}.hnp"
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(hap) as source, zipfile.ZipFile(output, "w") as destination:
        config = json.loads(source.read("module.json"))
        packages = config["module"].setdefault("hnpPackages", [])
        packages[:] = [package for package in packages if package["package"] != f"{package_name}.hnp"]
        packages.append({"package": f"{package_name}.hnp", "type": "public"})
        for entry in source.infolist():
            if entry.filename == archive_path:
                continue
            data = json.dumps(config).encode() if entry.filename == "module.json" else source.read(entry)
            if entry.filename.startswith("libs/") and config["module"].get("compressNativeLibs", True):
                entry.compress_type = zipfile.ZIP_DEFLATED
            destination.writestr(entry, data)
        destination.write(package, archive_path, compress_type=zipfile.ZIP_DEFLATED)
    with zipfile.ZipFile(output) as result:
        if result.read(archive_path) != package.read_bytes():
            raise ValueError(f"HAP {package_name} payload verification failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True, help=f"Download {ARCHIVE_URL}")
    parser.add_argument("--sdk", type=Path, required=True, help="OpenHarmony SDK directory")
    parser.add_argument("--hap", type=Path, required=True, help="Unsigned ARM64 HAP from hvigor")
    parser.add_argument("--output", type=Path, required=True, help="New unsigned HAP with Node and sh")
    parser.add_argument("--git-runtime", type=Path, help="Git installation produced by build-git.py")
    parser.add_argument("--ssh-runtime", type=Path, help="OpenSSH installation produced by build-ssh.py")
    parser.add_argument("--package-version-server-runtime", type=Path,
                        help="Language server installation produced by build-package-version-server.py")
    parser.add_argument("--rust-analyzer-runtime", type=Path,
                        help="Language server installation produced by build-rust-analyzer.py")
    parser.add_argument("--rust-toolchain-runtime", type=Path,
                        help="Native Rust installation produced by build-rust-toolchain.py")
    parser.add_argument("--clang-toolchain-runtime", type=Path,
                        help="Native compiler installation produced by build-clang-toolchain.py")
    args = parser.parse_args()
    if args.hap.resolve() == args.output.resolve():
        raise ValueError("Output must differ from the input unsigned HAP")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="zed-node-", dir=args.output.parent) as directory:
        staging_root = Path(directory)
        stage = staging_root / PACKAGE_NAME
        stage.mkdir()
        stage_runtime(args.archive, stage)
        subprocess.run([
            str(args.sdk / "toolchains/hnpcli"), "pack", "-i", str(stage), "-o", str(staging_root)
        ], check=True)
        package = staging_root / f"{PACKAGE_NAME}.hnp"
        normalize_archive(package)
        current_hap = staging_root / "node.hap"
        attach_runtime(args.hap, package, current_hap)
        for package_name, runtime, stage_function in (
            ("zed-shell", args.sdk, stage_shell_runtime),
            ("zed-git", args.git_runtime, stage_git_runtime),
            ("zed-openssh", args.ssh_runtime, stage_ssh_runtime),
            ("zed-package-version-server", args.package_version_server_runtime,
             stage_package_version_server),
            ("zed-rust-analyzer", args.rust_analyzer_runtime, stage_rust_analyzer_runtime),
            ("zed-rust-toolchain", args.rust_toolchain_runtime, stage_rust_toolchain_runtime),
            ("zed-clang-toolchain", args.clang_toolchain_runtime, stage_clang_toolchain_runtime),
        ):
            if runtime is None:
                continue
            runtime_stage = staging_root / package_name
            runtime_stage.mkdir()
            stage_function(runtime, runtime_stage)
            executables = {
                path.relative_to(staging_root).as_posix()
                for path in runtime_stage.rglob("*")
                if path.is_file() and path.stat().st_mode & 0o111
            }
            subprocess.run([
                str(args.sdk / "toolchains/hnpcli"), "pack", "-i", str(runtime_stage),
                "-o", str(staging_root),
            ], check=True)
            package = staging_root / f"{package_name}.hnp"
            normalize_archive(package, executables)
            next_hap = staging_root / f"{package_name}.hap"
            attach_runtime(current_hap, package, next_hap, package_name)
            current_hap.unlink()
            current_hap = next_hap
        current_hap.replace(args.output)
    runtimes = f"Node {VERSION} and shell runtime {SHELL_VERSION}"
    if args.git_runtime:
        runtimes += f" and Git {GIT_VERSION}"
    if args.ssh_runtime:
        runtimes += f" and OpenSSH {SSH_VERSION}"
    if args.package_version_server_runtime:
        runtimes += f" and package-version-server {PACKAGE_VERSION_SERVER_VERSION}"
    if args.rust_analyzer_runtime:
        runtimes += f" and rust-analyzer {RUST_ANALYZER_VERSION}"
    if args.rust_toolchain_runtime:
        runtimes += f" and Rust tools {RUST_TOOLCHAIN_VERSION}"
    if args.clang_toolchain_runtime:
        runtimes += f" and Clang tools {CLANG_TOOLCHAIN_VERSION}"
    print(f"{args.output}: {runtimes} HNP included; sign native code as required by the device")


if __name__ == "__main__":
    main()
