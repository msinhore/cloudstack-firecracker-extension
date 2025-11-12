#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Filesystem utilities module for Firecracker Agent.
This module contains filesystem-related utility functions.
"""
import json
import logging
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from backend.storage import Paths
from models import Spec

logger = logging.getLogger("fc-agent")


_AGENT_DEFAULTS: Dict[str, Any] = {}


def ensure_dirs(paths: Paths) -> None:
    """Ensure all required directories exist."""
    paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    paths.socket_file.parent.mkdir(parents=True, exist_ok=True)
    paths.pid_file.parent.mkdir(parents=True, exist_ok=True)
    paths.log_file.parent.mkdir(parents=True, exist_ok=True)


def paths(spec: Spec) -> Paths:
    """Generate Paths object from Spec."""
    conf_dir = Path(spec.host.conf_dir)
    run_dir = Path(spec.host.run_dir)
    log_dir = Path(spec.host.log_dir)
    return Paths(
        volume_file=spec.storage.volume_file,
        config_file=conf_dir / f"{spec.vm.name}.json",
        socket_file=run_dir / f"{spec.vm.name}.socket",
        pid_file=run_dir / f"{spec.vm.name}.pid",
        log_file=log_dir / f"{spec.vm.name}.log",
    )


def paths_by_name(vm_name: str) -> Paths:
    """Generate Paths object from VM name using agent defaults."""
    host_defaults = _AGENT_DEFAULTS.get("host", {}) if isinstance(_AGENT_DEFAULTS, dict) else {}
    storage_defaults = _AGENT_DEFAULTS.get("storage", {}) if isinstance(_AGENT_DEFAULTS, dict) else {}

    conf_dir = Path(host_defaults.get("conf_dir", "/etc/cloudstack/firecracker"))
    run_dir = Path(host_defaults.get("run_dir", "/var/run/firecracker"))
    log_dir = Path(host_defaults.get("log_dir", "/var/log/firecracker"))
    driver = (storage_defaults.get("driver") or "file").lower()
    if driver == "lvmthin" or driver == "lvm":
        vg = storage_defaults.get("volume_group") or storage_defaults.get("vg")
        if vg:
            volume_file = Path(f"/dev/{vg}/vm-{vm_name}")
        else:
            volume_file = Path("/dev") / f"vm-{vm_name}"
    else:
        volume_dir = storage_defaults.get("volume_dir")
        if isinstance(volume_dir, str) and volume_dir.strip():
            volume_file = Path(volume_dir) / f"{vm_name}.img"
        else:
            volume_file = Path("/tmp") / f"{vm_name}.img"
    return Paths(
        volume_file=volume_file,
        config_file=conf_dir / f"{vm_name}.json",
        socket_file=run_dir / f"{vm_name}.socket",
        pid_file=run_dir / f"{vm_name}.pid",
        log_file=log_dir / f"{vm_name}.log",
    )


def set_agent_defaults(defaults: Dict[str, Any]) -> None:
    """Persist agent defaults for components that only know the VM name."""
    global _AGENT_DEFAULTS
    _AGENT_DEFAULTS = defaults or {}


def read_cfg_json_by_name(vm_name: str) -> Optional[Dict[str, Any]]:
    """Read VM configuration JSON by VM name."""
    try:
        paths = paths_by_name(vm_name)
        if not paths.config_file.exists():
            return None
        with paths.config_file.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _is_block_device(p: Path) -> bool:
    try:
        st = os.stat(str(p))
        return stat.S_ISBLK(st.st_mode)
    except Exception:
        return False


def inject_ssh_key_into_path(host_path: Path, ssh_key: str, username: str = "root") -> None:
    """Mount a filesystem (file image via loop or block device) and append an SSH key.
    Best-effort; logs warnings on failure and cleans up mounts.
    """
    if not isinstance(host_path, Path):
        host_path = Path(str(host_path))
    if not ssh_key or not isinstance(ssh_key, str):
        return
    mnt_dir = Path(tempfile.mkdtemp(prefix="fc-mnt-"))
    loop_dev = None
    mapper_parts = []
    mounted = False
    try:
        # Try direct mount (block device or loop mount for file)
        if _is_block_device(host_path):
            subprocess.run(["mount", str(host_path), str(mnt_dir)], check=True)
            mounted = True
        else:
            # Try loop-mounting the image directly
            rc = subprocess.run(["mount", "-o", "loop", str(host_path), str(mnt_dir)])
            if rc.returncode == 0:
                mounted = True
            else:
                # Fallback: losetup + kpartx (first partition)
                loop_dev = subprocess.check_output(["losetup", "--show", "-f", str(host_path)], text=True).strip()
                subprocess.run(["kpartx", "-av", loop_dev], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                # Assume p1
                part = f"/dev/mapper/{Path(loop_dev).name}p1"
                mapper_parts.append(part)
                subprocess.run(["mount", part, str(mnt_dir)], check=True)
                mounted = True

        # Write authorized_keys
        home_dir = mnt_dir / username
        ssh_dir = home_dir / ".ssh"
        auth_file = ssh_dir / "authorized_keys"
        try:
            ssh_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            # In some images, root home may be /root
            home_dir_alt = mnt_dir / "root"
            ssh_dir = home_dir_alt / ".ssh"
            auth_file = ssh_dir / "authorized_keys"
            ssh_dir.mkdir(parents=True, exist_ok=True)

        with auth_file.open("a", encoding="utf-8") as f:
            f.write(ssh_key.strip() + "\n")
        os.chmod(ssh_dir, 0o700)
        os.chmod(auth_file, 0o600)
        try:
            # chown to root if possible
            os.chown(ssh_dir, 0, 0)
            os.chown(auth_file, 0, 0)
        except Exception:
            pass
        logger.info("Injected SSH key into %s", str(auth_file))
    except Exception as e:
        logger.warning("SSH key injection failed for %s: %s", str(host_path), e)
    finally:
        # Cleanup mounts and loop devices
        try:
            if mounted:
                subprocess.run(["umount", str(mnt_dir)], check=False)
        except Exception:
            pass
        if mapper_parts:
            try:
                subprocess.run(["kpartx", "-dv", loop_dev], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
        if loop_dev:
            try:
                subprocess.run(["losetup", "-d", loop_dev], check=False)
            except Exception:
                pass
        try:
            shutil.rmtree(mnt_dir, ignore_errors=True)
        except Exception:
            pass


def delete_volume_from_cfg(cfg: Dict[str, Any], default_volume_path: Path) -> None:
    """Remove volume configuration from VM config."""
    try:
        volumes = cfg.get("volumes", [])
        if not volumes:
            return
        cfg["volumes"] = [
            volume
            for volume in volumes
            if not (isinstance(volume, dict) and volume.get("path") == str(default_volume_path))
        ]
        if not cfg["volumes"]:
            cfg.pop("volumes", None)
        drives = cfg.get("drives", [])
        cfg["drives"] = [
            drive
            for drive in drives
            if not (isinstance(drive, dict) and drive.get("path_on_host") == str(default_volume_path))
        ]
        if not cfg["drives"]:
            cfg.pop("drives", None)
    except Exception:
        pass
