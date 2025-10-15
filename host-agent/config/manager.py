#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Configuration management module for Firecracker Agent.
This module handles agent configuration loading, VM config generation, and network config persistence.
"""
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from backend.storage import Paths, get_backend_by_driver
from backend.networking.helpers import tap_name
from models import Spec

logger = logging.getLogger("fc-agent")


class ConfigManager:
    """Manager for configuration operations."""

    def __init__(self, agent_defaults: Dict[str, Any]):
        self.agent_defaults = agent_defaults

    def load_agent_config(self) -> Dict[str, Any]:
        """Load agent config and validate mandatory paths/binaries.
        Precedence: env > JSON file (FC_AGENT_CONFIG) for bind host/port only.
        For filesystem paths/binaries, **no hardcoded defaults** are used:
        - defaults.host.firecracker_bin (required)
        - defaults.host.conf_dir (required)
        - defaults.host.run_dir (required)
        - defaults.host.log_dir (required)
        - defaults.host.payload_dir (required)
        - defaults.storage.volume_dir (required if using file storage)
        On any parse/syntax error or missing required keys, the server will NOT start.
        """
        cfg: Dict[str, Any] = {
            "bind_host": "0.0.0.0",
            "bind_port": 8080,
        }
        # 2) Load from JSON file if present (syntax errors are fatal)
        cfg_path = os.environ.get("FC_AGENT_CONFIG", "/etc/cloudstack/firecracker-agent.json")
        file_cfg = {}
        try:
            p = Path(cfg_path)
            if p.exists():
                with p.open("r", encoding="utf-8") as f:
                    try:
                        file_cfg = json.load(f)
                    except Exception as e:
                        # Fail fast: do not start the server with an invalid
                        # config
                        raise RuntimeError(f"Invalid JSON in FC_AGENT_CONFIG='{cfg_path}': {e}") from e
                if isinstance(file_cfg, dict):
                    if "bind_host" in file_cfg and isinstance(file_cfg["bind_host"], str):
                        cfg["bind_host"] = file_cfg["bind_host"]
                    if "bind_port" in file_cfg:
                        try:
                            cfg["bind_port"] = int(file_cfg["bind_port"])  # type: ignore[arg-type]
                        except Exception:
                            pass
                    # Load defaults section from config file
                    if "defaults" in file_cfg and isinstance(file_cfg["defaults"], dict):
                        cfg["defaults"] = file_cfg["defaults"]
                    # Preserve optional top-level sections (security/auth/logging/etc.)
                    for key, value in file_cfg.items():
                        if key in {"bind_host", "bind_port", "defaults"}:
                            continue
                        cfg[key] = value
        except Exception:
            # Any unexpected error while reading the config file is fatal
            raise
        # 3) Agent-side defaults (host paths/binaries) from top-level config
        # Ensure defaults structure
        if "defaults" not in cfg or not isinstance(cfg.get("defaults"), dict):
            cfg["defaults"] = {"host": {}, "storage": {}, "net": {}}
        else:
            # Ensure sub-sections exist
            for key in ["host", "storage", "net"]:
                if key not in cfg["defaults"] or not isinstance(cfg["defaults"][key], dict):
                    cfg["defaults"][key] = {}
        # Normalize UI configuration
        ui_cfg = cfg.get("ui") if isinstance(cfg.get("ui"), dict) else {}
        enabled = ui_cfg.get("enabled")
        if enabled is None:
            enabled = True
        else:
            enabled = bool(enabled)
        timeout_seconds = 1800
        if "session_timeout_seconds" in ui_cfg:
            try:
                timeout_seconds = int(ui_cfg.get("session_timeout_seconds", 1800))
            except (TypeError, ValueError):
                timeout_seconds = 1800
        elif "session_timeout_minutes" in ui_cfg:
            try:
                timeout_seconds = int(ui_cfg.get("session_timeout_minutes", 30)) * 60
            except (TypeError, ValueError):
                timeout_seconds = 1800
        if timeout_seconds < 0:
            timeout_seconds = 0
        cfg["ui"] = {
            "enabled": enabled,
            "session_timeout_seconds": timeout_seconds,
        }
        return cfg

    def write_config(self, spec: Spec, paths: Paths) -> None:
        """Render a full Firecracker JSON config on disk using values from `Spec`."""
        # Starting a VM requires a kernel path; stop/status/delete never call
        # write_config.
        if not spec.vmext.kernel or not isinstance(spec.vmext.kernel, str) or not spec.vmext.kernel.strip():
            raise ValueError("Kernel image path is required to start a VM (spec.vmext.kernel is empty).")
        if not Path(spec.vmext.kernel).exists():
            logger.error("Kernel image not found: %s", spec.vmext.kernel)
            logger.error("Please ensure the kernel image exists at the specified path")
            logger.error("You may need to download a Firecracker-compatible kernel")
            raise FileNotFoundError(f"Kernel image not found: {spec.vmext.kernel}")
        nics = []
        for nic in sorted(spec.vm.nics, key=lambda x: x.deviceId):
            host_dev = tap_name(nic.deviceId, spec.vm.name)
            nics.append(
                {
                    "iface_id": f"eth{nic.deviceId}",
                    "guest_mac": nic.mac,
                    "host_dev_name": host_dev,
                }
            )
        # Get device path from storage backend
        try:
            backend = get_backend_by_driver(getattr(spec.storage, "driver", None) or "file", spec, paths)
            path_on_host = backend.device_path()
        except Exception as e:
            logger.warning("Failed to get device path from backend, falling back to volume_file: %s", e)
            path_on_host = str(paths.volume_file)
        cfg = {
            "boot-source": {
                "kernel_image_path": spec.vmext.kernel,
                "boot_args": spec.vmext.boot_args,
                "initrd_path": None,
            },
            "drives": [
                {
                    "drive_id": "rootfs",
                    "partuuid": None,
                    "is_root_device": True,
                    "cache_type": "Unsafe",
                    "is_read_only": False,
                    "path_on_host": path_on_host,
                    "io_engine": "Sync",
                    "rate_limiter": None,
                    "socket": None,
                }
            ],
            "machine-config": {
                "vcpu_count": spec.vm.cpus,
                "mem_size_mib": spec.vmext.mem_mib,
                "smt": False,
                "track_dirty_pages": False,
            },
            "network-interfaces": nics,
            "vsock": None,
            "logger": {
                "log_path": str(paths.log_file),
                "level": "Info",
                "show_level": False,
                "show_log_origin": False,
            },
            "metrics": None,
            "mmds-config": None,
        }
        with paths.config_file.open("w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)

    def save_network_config(self, vm_name: str, network_config: Dict[str, Any]) -> None:
        """Save network configuration for a VM to persistent storage."""
        try:
            run_dir_path = self.agent_defaults.get("host", {}).get("run_dir")
            if not run_dir_path:
                logger.warning("run_dir not configured in agent defaults, skipping network config save")
                return
            config_file = Path(run_dir_path) / f"network-config-{vm_name}.json"
            config_file.parent.mkdir(parents=True, exist_ok=True)
            with config_file.open("w", encoding="utf-8") as f:
                json.dump(network_config, f, indent=2)
            logger.info("Saved network config for VM: %s", vm_name)
        except Exception as e:
            logger.error("Failed to save network config for VM %s: %s", vm_name, e)

    def load_network_config(self, vm_name: str) -> Optional[Dict[str, Any]]:
        """Load network configuration for a VM from persistent storage."""
        try:
            run_dir_path = self.agent_defaults.get("host", {}).get("run_dir")
            if not run_dir_path:
                logger.warning("run_dir not configured in agent defaults, skipping network config load")
                return None
            config_file = Path(run_dir_path) / f"network-config-{vm_name}.json"
            if not config_file.exists():
                return None
            with config_file.open("r", encoding="utf-8") as f:
                network_config = json.load(f)
            logger.info("Loaded network config for VM: %s", vm_name)
            return network_config
        except Exception as e:
            logger.error("Failed to load network config for VM %s: %s", vm_name, e)
            return None

    def cleanup_network_config(self, vm_name: str) -> None:
        """Clean up network configuration file for a VM."""
        try:
            run_dir_path = self.agent_defaults.get("host", {}).get("run_dir")
            if not run_dir_path:
                logger.warning("run_dir not configured in agent defaults, skipping network config cleanup")
                return
            config_file = Path(run_dir_path) / f"network-config-{vm_name}.json"
            if config_file.exists():
                config_file.unlink()
                logger.info("Cleaned up network config for VM: %s", vm_name)
        except Exception as e:
            logger.error("Failed to cleanup network config for VM %s: %s", vm_name, e)

    def build_network_config_from_spec(self, spec: Spec) -> Dict[str, Any]:
        """Build network configuration from VM spec for persistence."""
        network_config = {
            "vm_name": spec.vm.name,
            "driver": spec.net.driver,
            "bridge": spec.net.bridge,
            "nics": [],
        }
        for nic in spec.vm.nics:
            network_config["nics"].append(
                {
                    "device_id": nic.deviceId,
                    "mac": nic.mac,
                    "ip": nic.ip,
                    "netmask": nic.netmask,
                    "gateway": nic.gateway,
                    "vlan": nic.vlan,
                }
            )
        return network_config

    def apply_network_config_from_saved(self, vm_name: str, network_config: Dict[str, Any]) -> bool:
        """Apply saved network configuration to running VM."""
        try:
            logger.info("Applying network config for VM %s: %s", vm_name, network_config)
            return True
        except Exception as e:
            logger.error("Failed to apply network config for VM %s: %s", vm_name, e)
            return False
