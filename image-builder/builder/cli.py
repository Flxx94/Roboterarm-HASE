#!/usr/bin/env python3
"""Build a customized Raspberry Pi OS image from an existing .img file."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml


class BuildError(RuntimeError):
    pass


def run(args: list[str], *, check: bool = True, **kwargs: Any) -> subprocess.CompletedProcess:
    result = subprocess.run(args, check=False, **kwargs)
    if check and result.returncode:
        raise BuildError(f"Command failed ({result.returncode}): {' '.join(args)}")
    return result


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise BuildError(f"Required tool is missing from the builder container: {name}")


def load_config(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise BuildError(f"Cannot read configuration {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BuildError("Configuration root must be a YAML mapping")
    return data


def config_path(config_file: Path, value: str) -> Path:
    """Resolve a config-relative source path, including paths under /project."""
    path = Path(value)
    if path.is_absolute():
        return path
    candidate = (config_file.parent / path).resolve()
    project = Path("/project").resolve()
    try:
        candidate.relative_to(project)
    except ValueError as exc:
        raise BuildError(f"Config source must be inside the mounted project: {value}") from exc
    return candidate


def target_path(rootfs: Path, destination: str) -> Path:
    target = (rootfs / destination.lstrip("/")).resolve()
    try:
        target.relative_to(rootfs.resolve())
    except ValueError as exc:
        raise BuildError(f"Destination must stay inside the target root filesystem: {destination}") from exc
    return target


def detect_architecture(rootfs: Path) -> str:
    status = rootfs / "var/lib/dpkg/status"
    if status.is_file():
        for stanza in status.read_text(errors="replace").split("\n\n"):
            if re.search(r"(?m)^Package: dpkg$", stanza):
                match = re.search(r"(?m)^Architecture: (arm64|armhf)$", stanza)
                if match:
                    return match.group(1)
    raise BuildError("Cannot determine target architecture from the dpkg database")


def image_partitions(image: Path) -> tuple[list[dict[str, str]], list[str]]:
    """Attach individual image partitions by offset; does not depend on loop pN nodes."""
    table = json.loads(run(["sfdisk", "--json", str(image)], capture_output=True, text=True).stdout)["partitiontable"]
    sector_size = int(table.get("sectorsize", 512))
    rows: list[dict[str, str]] = []
    devices: list[str] = []
    try:
        for partition in table.get("partitions", []):
            offset = int(partition["start"]) * sector_size
            size = int(partition["size"]) * sector_size
            device = run(
                ["losetup", "--find", "--show", "--offset", str(offset), "--sizelimit", str(size), str(image)],
                capture_output=True,
                text=True,
            ).stdout.strip()
            devices.append(device)
            probe = run(["blkid", "-p", "-o", "export", device], check=False, capture_output=True, text=True)
            metadata = {}
            for line in probe.stdout.splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    metadata[key] = value
            rows.append(
                {
                    "PATH": device,
                    "LABEL": metadata.get("LABEL", ""),
                    "FSTYPE": metadata.get("TYPE", ""),
                    "PARTLABEL": str(partition.get("name", "")),
                    "TYPE": str(partition.get("type", "")),
                }
            )
    except Exception:
        for device in reversed(devices):
            run(["losetup", "--detach", device], check=False)
        raise
    return rows, devices


def select_partitions(rows: list[dict[str, str]]) -> tuple[str, str | None]:
    roots = [r["PATH"] for r in rows if r["LABEL"].lower() in {"rootfs", "root"} or r["PARTLABEL"].lower() in {"rootfs", "root"}]
    if not roots:
        roots = [r["PATH"] for r in rows if r["FSTYPE"] in {"ext4", "ext3", "ext2"}]
    if len(roots) != 1:
        summary = [f"{r['PATH']} label={r['LABEL']!r} partlabel={r['PARTLABEL']!r} fs={r['FSTYPE']!r}" for r in rows]
        raise BuildError(f"Could not identify exactly one Linux root partition; candidates: {roots}; partitions: {summary}")
    boots = [r["PATH"] for r in rows if r["LABEL"].lower() in {"boot", "bootfs"} or r["PARTLABEL"].lower() in {"boot", "bootfs"}]
    if len(boots) > 1:
        raise BuildError(f"Found multiple boot partitions: {boots}")
    return roots[0], boots[0] if boots else None


def bind_mounts(rootfs: Path) -> list[Path]:
    mounts: list[Path] = []
    for name in ("dev", "proc", "sys", "run"):
        target = rootfs / name
        target.mkdir(parents=True, exist_ok=True)
        run(["mount", "--rbind", f"/{name}", str(target)])
        run(["mount", "--make-rslave", str(target)])
        mounts.append(target)
    return mounts


def chroot(rootfs: Path, command: list[str], *, check: bool = True, **kwargs: Any) -> subprocess.CompletedProcess:
    return run(["chroot", str(rootfs), *command], check=check, **kwargs)


def package_stage(rootfs: Path, cfg: dict[str, Any]) -> None:
    packages = cfg.get("packages", {}) or {}
    if packages.get("update", True):
        print("[packages] Updating installed packages", flush=True)
        chroot(rootfs, ["/usr/bin/env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "update"])
        chroot(rootfs, ["/usr/bin/env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "-y", "full-upgrade"])

    wanted = list(dict.fromkeys(packages.get("keep", []) + packages.get("extra", [])))
    netplan = cfg.get("netplan", {}) or {}
    if netplan.get("enabled"):
        wanted.append("netplan.io")
        renderer = netplan.get("renderer")
        if renderer == "NetworkManager":
            wanted.append("network-manager")
        elif renderer == "networkd":
            wanted.append("systemd")
        elif renderer:
            raise BuildError("netplan.renderer must be NetworkManager or networkd")
    if wanted:
        print(f"[packages] Ensuring requested packages: {', '.join(wanted)}", flush=True)
        chroot(rootfs, ["/usr/bin/env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", *wanted])

    if not packages.get("prune", False):
        print("[packages] Pruning disabled; preserving the existing package set", flush=True)
        return

    # Mark the existing package set automatic, then mark the declared roots as
    # manual. APT computes and preserves their dependency closure on autoremove.
    installed = chroot(rootfs, ["dpkg-query", "-W", "-f=${binary:Package}\\n"], capture_output=True, text=True).stdout.splitlines()
    for start in range(0, len(installed), 250):
        batch = installed[start : start + 250]
        if batch:
            chroot(rootfs, ["apt-mark", "auto", *batch], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if wanted:
        chroot(rootfs, ["apt-mark", "manual", *wanted])
    print("[packages] Removing unneeded automatic packages", flush=True)
    chroot(rootfs, ["/usr/bin/env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "-y", "autoremove", "--purge"])


def copy_declared(config_file: Path, rootfs: Path, entries: list[dict[str, Any]], *, label: str) -> None:
    for entry in entries:
        source = config_path(config_file, str(entry["source"]))
        if not source.exists():
            raise BuildError(f"{label} source does not exist: {source}")
        destination = target_path(rootfs, str(entry["destination"]))
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)
        else:
            shutil.copy2(source, destination)
        if entry.get("mode"):
            destination.chmod(int(str(entry["mode"]), 8))
        print(f"[{label}] {source} -> /{destination.relative_to(rootfs)}", flush=True)


def apply_netplan(config_file: Path, rootfs: Path, cfg: dict[str, Any]) -> None:
    netplan = cfg.get("netplan", {}) or {}
    if not netplan.get("enabled"):
        return
    renderer = netplan.get("renderer", "networkd")
    if renderer not in {"NetworkManager", "networkd"}:
        raise BuildError("netplan.renderer must be NetworkManager or networkd")
    configs = netplan.get("configs", [])
    if not configs:
        raise BuildError("netplan.enabled requires at least one config file")
    for entry in configs:
        source = config_path(config_file, str(entry["source"]))
        try:
            document = yaml.safe_load(source.read_text())
        except (OSError, yaml.YAMLError) as exc:
            raise BuildError(f"Cannot parse Netplan config {source}: {exc}") from exc
        network = document.get("network") if isinstance(document, dict) else None
        if not isinstance(network, dict):
            raise BuildError(f"Netplan config must contain a network mapping: {source}")
        configured_renderer = network.get("renderer", netplan.get("renderer", "networkd"))
        if configured_renderer != renderer:
            raise BuildError(f"Netplan renderer in {source} ({configured_renderer}) does not match netplan.renderer ({renderer})")
    copy_declared(config_file, rootfs, configs, label="netplan")


def apply_systemd(config_file: Path, rootfs: Path, cfg: dict[str, Any]) -> None:
    systemd = cfg.get("systemd", {}) or {}
    units = systemd.get("units", [])
    copy_declared(config_file, rootfs, [u for u in units if "source" in u], label="systemd")
    for unit in units:
        if "name" not in unit:
            unit_name = Path(str(unit["destination"])).name
        else:
            unit_name = str(unit["name"])
        state = unit.get("state", "enabled" if unit.get("enabled", False) else "disabled")
        if state not in {"enabled", "disabled", "masked"}:
            raise BuildError(f"Invalid systemd state for {unit_name}: {state}")
        if state == "masked":
            target = rootfs / "etc/systemd/system" / unit_name
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to("/dev/null")
        else:
            verb = "enable" if state == "enabled" else "disable"
            run(["systemctl", f"--root={rootfs}", verb, unit_name])


def run_hooks(config_file: Path, rootfs: Path, hooks: list[str], stage: str) -> None:
    for hook in hooks:
        script = config_path(config_file, hook)
        if not script.is_file():
            raise BuildError(f"Hook does not exist: {script}")
        print(f"[hook:{stage}] {script}", flush=True)
        run(["bash", str(script)], env={**os.environ, "ROOTFS": str(rootfs), "BUILD_STAGE": stage})


def install_rustctl(rootfs: Path, cfg: dict[str, Any], architecture: str) -> None:
    settings = cfg.get("rustctl", {}) or {}
    if not settings.get("enabled", True):
        return
    if architecture != "arm64":
        raise BuildError("The bundled rustctl artifact currently supports arm64 images only")
    artifact = Path("/opt/image-builder/artifacts/rustctl-aarch64")
    if not artifact.is_file():
        raise BuildError(f"Bundled rustctl artifact is missing: {artifact}")
    destination = rootfs / str(settings.get("install_path", "/rustctl/rustctl")).lstrip("/")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(artifact, destination)
    destination.chmod(0o755)


def build(args: argparse.Namespace) -> None:
    input_image = Path(args.input).resolve()
    config_file = Path(args.config).resolve()
    output = Path(args.output).resolve()
    if not input_image.is_file():
        raise BuildError(f"Input image not found: {input_image}")
    if input_image == output:
        raise BuildError("Output image must not overwrite the input image")
    cfg = load_config(config_file)
    for tool in ("losetup", "sfdisk", "blkid", "mount", "umount", "chroot", "systemctl"):
        require_tool(tool)
    if input_image.suffix.lower() == ".xz":
        require_tool("xz")

    architecture = cfg.get("architecture", "auto")
    if architecture not in {"auto", "arm64", "armhf"}:
        raise BuildError("architecture must be auto, arm64, or armhf")

    work_dir = Path("/work")
    work_dir.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_image = work_dir / f"{output.stem}.working.img"
    if temp_image.exists():
        temp_image.unlink()
    if input_image.suffix.lower() == ".xz":
        print(f"[image] Decompressing {input_image.name} to working image", flush=True)
        with temp_image.open("wb") as uncompressed:
            run(["xz", "--decompress", "--stdout", "--", str(input_image)], stdout=uncompressed)
    else:
        print(f"[image] Copying input image to {temp_image}", flush=True)
        shutil.copyfile(input_image, temp_image)

    partition_devices: list[str] = []
    mounts: list[Path] = []
    try:
        partitions, partition_devices = image_partitions(temp_image)
        root_partition, boot_partition = select_partitions(partitions)
        rootfs = work_dir / "rootfs"
        rootfs.mkdir(exist_ok=True)
        run(["mount", root_partition, str(rootfs)])
        mounts.append(rootfs)
        detected_arch = detect_architecture(rootfs)
        if architecture == "auto":
            architecture = detected_arch
        elif architecture != detected_arch:
            raise BuildError(f"Configured architecture {architecture} does not match image architecture {detected_arch}")
        if boot_partition:
            candidates = [rootfs / "boot/firmware", rootfs / "boot"]
            boot_mount = next((p for p in candidates if p.exists()), candidates[0])
            boot_mount.mkdir(parents=True, exist_ok=True)
            run(["mount", boot_partition, str(boot_mount)])
            mounts.append(boot_mount)

        mounts.extend(bind_mounts(rootfs))
        if run(["chroot", str(rootfs), "/bin/true"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            raise BuildError("Cannot execute target binaries in chroot. On x86, enable ARM64 QEMU/binfmt in Docker; on Linux it may need host binfmt registration.")
        package_stage(rootfs, cfg)
        copy_declared(config_file, rootfs, cfg.get("files", []), label="file")
        copy_declared(config_file, rootfs, cfg.get("tools", []), label="tool")
        apply_netplan(config_file, rootfs, cfg)
        install_rustctl(rootfs, cfg, architecture)
        apply_systemd(config_file, rootfs, cfg)
        hooks = cfg.get("hooks", {}) or {}
        if hooks.get("mode", "container") != "container":
            raise BuildError("Only hooks.mode=container is implemented; host hooks are planned")
        run_hooks(config_file, rootfs, hooks.get("after_packages", []), "after_packages")
        run_hooks(config_file, rootfs, hooks.get("before_finalize", []), "before_finalize")

        if args.test or (cfg.get("test", {}) or {}).get("shell", False):
            print("[test] Entering target chroot; type exit to continue image finalization", flush=True)
            chroot(rootfs, ["/bin/bash", "-l"], check=False)
        run(["sync"])
    finally:
        for mountpoint in reversed(mounts):
            run(["umount", "--recursive", str(mountpoint)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for device in reversed(partition_devices):
            run(["losetup", "--detach", device], check=False)

    shutil.move(str(temp_image), str(output))
    print(f"[done] Wrote {output}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build", help="customize an existing Raspberry Pi OS image")
    build_parser.add_argument("--input", required=True, help="input Raspberry Pi OS .img file")
    build_parser.add_argument("--config", required=True, help="YAML builder configuration")
    build_parser.add_argument("--output", required=True, help="path for the generated .img file")
    build_parser.add_argument("--test", action="store_true", help="open a target chroot shell before finalizing")
    args = parser.parse_args()
    try:
        if args.command == "build":
            build(args)
        return 0
    except (BuildError, subprocess.CalledProcessError) as exc:
        print(f"image-builder: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
