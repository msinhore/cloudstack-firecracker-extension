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
from typing import Any, Dict, List, Optional

from backend.networking import get_backend_by_driver as get_network_backend_by_driver
from backend.storage import Paths, get_backend_by_driver
from backend.networking.helpers import tap_name
from models import HostDetails, NetSpec, NIC, Spec, StorageSpec, VMDetails, VMExt
from utils.filesystem import paths_by_name

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
        # Provide a default payload directory for create-spec dumps so it can
        # be referenced elsewhere even if the config file omits it.
        host_defaults = cfg["defaults"].get("host", {})
        if isinstance(host_defaults, dict) and "payload_dir" not in host_defaults:
            host_defaults["payload_dir"] = "/var/lib/firecracker/payload"
        return cfg

    def _payload_path(self, vm_name: str) -> Path:
        """Return the expected payload JSON path for a VM."""
        host_defaults = self.agent_defaults.get("host", {}) if isinstance(self.agent_defaults, dict) else {}
        payload_dir = host_defaults.get("payload_dir") or "/var/lib/firecracker/payload"
        return Path(payload_dir) / f"{vm_name}-payload.json"

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
                payload = self._load_payload(vm_name)
                if payload:
                    logger.info(
                        "No persisted network config for VM %s, reconstructing from payload",
                        vm_name,
                    )
                    net_defaults = self.agent_defaults.get("net", {}) if isinstance(self.agent_defaults, dict) else {}
                    return {
                        "vm_name": vm_name,
                        "driver": net_defaults.get("driver", "linux-bridge-vlan"),
                        "bridge": net_defaults.get("host_bridge", ""),
                        "uplink": net_defaults.get("uplink", ""),
                        "nics": self._nics_from_payload(payload),
                    }
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

    def cleanup_payload(self, vm_name: str) -> None:
        """Remove the stored create payload for the VM (idempotent)."""
        try:
            payload_path = self._payload_path(vm_name)
            if payload_path.exists():
                payload_path.unlink()
                logger.info("Removed payload file for VM: %s", vm_name)
            # Legacy fallback: older agents stored payloads under log_dir with
            # the create-spec prefix. Clean them up to avoid stale files.
            host_defaults = self.agent_defaults.get("host", {}) if isinstance(self.agent_defaults, dict) else {}
            legacy_dir = host_defaults.get("log_dir")
            if legacy_dir:
                legacy_path = Path(legacy_dir) / f"create-spec-{vm_name}.json"
                if legacy_path.exists():
                    legacy_path.unlink()
        except Exception as e:
            logger.warning("Failed to cleanup payload for VM %s: %s", vm_name, e)

    def build_network_config_from_spec(self, spec: Spec) -> Dict[str, Any]:
        """Build network configuration from VM spec for persistence."""
        network_config = {
            "vm_name": spec.vm.name,
            "driver": spec.net.driver,
            "bridge": spec.net.bridge,
            "uplink": getattr(spec.net, "uplink", ""),
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
                    "broadcastUri": nic.broadcastUri,
                }
            )
        return network_config

    def apply_network_config_from_saved(
        self,
        vm_name: str,
        network_config: Optional[Dict[str, Any]] = None,
        fallback_spec: Optional[Spec] = None,
    ) -> bool:
        """Apply saved network configuration to running VM.

        The logic rebuilds a minimal Spec using the persisted network snapshot
        and the original CloudStack payload (if available), then re-runs the
        networking backend's ``prepare`` routine so TAPs and VLANs are ready
        before the VM is started.
        """
        try:
            net_cfg = network_config or self.load_network_config(vm_name)
            spec = self._build_spec_for_network_recovery(vm_name, net_cfg, fallback_spec)
            if not spec or not spec.vm.nics:
                logger.warning("No NIC data available to recover networking for VM %s", vm_name)
                return False
            paths_obj = paths_by_name(vm_name)
            backend = get_network_backend_by_driver(spec.net.driver, spec, paths_obj)
            backend.prepare()
            # Persist refreshed view so subsequent recoveries keep broadcastUri/VLAN data
            try:
                refreshed_cfg = self.build_network_config_from_spec(spec)
                self.save_network_config(vm_name, refreshed_cfg)
            except Exception as exc:
                logger.debug("Failed to refresh saved network config for VM %s: %s", vm_name, exc)
            logger.info(
                "Reapplied networking for VM %s using driver=%s bridge=%s uplink=%s",
                vm_name,
                spec.net.driver,
                spec.net.host_bridge,
                spec.net.uplink,
            )
            return True
        except Exception as e:
            logger.error("Failed to apply network config for VM %s: %s", vm_name, e)
            return False

    # ------------------------------------------------------------------
    # Internal helpers for reconstructing specs from persisted metadata
    # ------------------------------------------------------------------

    def _build_spec_for_network_recovery(
        self,
        vm_name: str,
        network_config: Optional[Dict[str, Any]],
        fallback_spec: Optional[Spec] = None,
    ) -> Optional[Spec]:
        payload = self._load_payload(vm_name)
        payload_nics = self._nics_from_payload(payload)
        fallback_nics = self._nics_from_spec(fallback_spec) if fallback_spec else []

        net_defaults = self.agent_defaults.get("net", {}) if isinstance(self.agent_defaults, dict) else {}
        host_defaults = self.agent_defaults.get("host", {}) if isinstance(self.agent_defaults, dict) else {}

        driver = (network_config or {}).get("driver") or net_defaults.get("driver") or "linux-bridge-vlan"
        bridge = (network_config or {}).get("bridge") or net_defaults.get("host_bridge", "")
        uplink = (network_config or {}).get("uplink") or net_defaults.get("uplink", "")
        if fallback_spec:
            driver = getattr(fallback_spec.net, "driver", driver) or driver
            bridge = getattr(fallback_spec.net, "host_bridge", bridge) or getattr(fallback_spec.net, "bridge", bridge) or bridge
            uplink = getattr(fallback_spec.net, "uplink", uplink) or uplink

        saved_nics = (network_config or {}).get("nics") or []
        if not saved_nics:
            saved_nics = payload_nics or fallback_nics

        merged_nics: List[NIC] = []
        payload_by_mac = {nic.get("mac"): nic for nic in payload_nics if nic.get("mac")}
        payload_by_id = {nic.get("device_id", idx): nic for idx, nic in enumerate(payload_nics)}
        fallback_by_mac = {nic.get("mac"): nic for nic in fallback_nics if nic.get("mac")}
        fallback_by_id = {nic.get("device_id", idx): nic for idx, nic in enumerate(fallback_nics)}

        for idx, nic_entry in enumerate(saved_nics):
            device_id = self._safe_int(nic_entry.get("device_id"), idx)
            mac = str(nic_entry.get("mac", "") or "")
            base = (
                payload_by_mac.get(mac)
                or payload_by_id.get(device_id)
                or fallback_by_mac.get(mac)
                or fallback_by_id.get(device_id)
                or {}
            )

            vlan = nic_entry.get("vlan")
            if vlan is None:
                vlan = self._safe_int(base.get("vlan"), None)

            broadcast_uri = nic_entry.get("broadcastUri") or base.get("broadcastUri")
            if not broadcast_uri and vlan is not None:
                broadcast_uri = f"vlan://{vlan}"
            if vlan is None and isinstance(broadcast_uri, str) and broadcast_uri.startswith("vlan://"):
                vlan = self._safe_int(broadcast_uri.split("vlan://", 1)[1], None)

            ip = nic_entry.get("ip") or base.get("ip") or ""
            netmask = nic_entry.get("netmask") or base.get("netmask") or ""
            gateway = nic_entry.get("gateway") or base.get("gateway") or ""

            merged_nics.append(
                NIC(
                    deviceId=device_id,
                    mac=mac,
                    ip=str(ip or ""),
                    netmask=str(netmask or ""),
                    gateway=str(gateway or ""),
                    vlan=vlan if vlan is None or isinstance(vlan, int) else self._safe_int(vlan, None),
                    broadcastUri=broadcast_uri if isinstance(broadcast_uri, str) else None,
                )
            )

        merged_nics.sort(key=lambda nic: nic.deviceId)

        if not merged_nics:
            return None

        payload_vm = payload.get("cloudstack.vm.details", {}) if isinstance(payload, dict) else {}

        fallback_vm = getattr(fallback_spec, "vm", None)
        cpus = (
            self._safe_int((network_config or {}).get("cpus"))
            or (fallback_vm.cpus if fallback_vm and getattr(fallback_vm, "cpus", None) else None)
            or self._safe_int(payload_vm.get("cpu"), 1)
            or 1
        )
        memory_mib = (
            self._safe_int(payload_vm.get("memory"), None)
            or (fallback_vm.minRam // (1024 * 1024) if fallback_vm and getattr(fallback_vm, "minRam", 0) else None)
            or 512
        )
        vm_details = VMDetails(
            name=vm_name,
            cpus=cpus,
            minRam=memory_mib * 1024 * 1024,
            nics=merged_nics,
        )

        host_details = HostDetails(
            firecracker_bin=str(host_defaults.get("firecracker_bin") or ""),
            conf_dir=str(host_defaults.get("conf_dir") or ""),
            run_dir=str(host_defaults.get("run_dir") or ""),
            log_dir=str(host_defaults.get("log_dir") or ""),
        )

        paths_obj = paths_by_name(vm_name)

        storage_driver = getattr(getattr(fallback_spec, "storage", None), "driver", None) or "file"
        storage_volume = getattr(getattr(fallback_spec, "storage", None), "volume_file", None) or paths_obj.volume_file
        storage_spec = StorageSpec(driver=storage_driver, volume_file=storage_volume)

        payload_vmext = (payload.get("externaldetails", {}) if isinstance(payload, dict) else {}).get("virtualmachine", {})
        kernel_path = payload_vmext.get("kernel") or payload_vmext.get("kernel_image_path") or ""
        image_path = payload_vmext.get("image") or str(paths_obj.volume_file)
        boot_args = payload_vmext.get("boot_args", "")

        vm_ext = VMExt(
            kernel=str(kernel_path or ""),
            boot_args=str(boot_args or ""),
            mem_mib=memory_mib,
            image=str(image_path or paths_obj.volume_file),
        )

        net_spec = NetSpec(
            driver=driver,
            bridge=bridge,
            nics=merged_nics,
            host_bridge=bridge,
            uplink=uplink,
        )

        return Spec(vm=vm_details, host=host_details, vmext=vm_ext, storage=storage_spec, net=net_spec)

    def _load_payload(self, vm_name: str) -> Optional[Dict[str, Any]]:
        try:
            payload_path = self._payload_path(vm_name)
            if payload_path.exists():
                with payload_path.open("r", encoding="utf-8") as fh:
                    return json.load(fh)
            # Legacy fallback for older installations
            host_defaults = self.agent_defaults.get("host", {}) if isinstance(self.agent_defaults, dict) else {}
            legacy_dir = host_defaults.get("log_dir")
            if legacy_dir:
                legacy_path = Path(legacy_dir) / f"create-spec-{vm_name}.json"
                if legacy_path.exists():
                    with legacy_path.open("r", encoding="utf-8") as fh:
                        return json.load(fh)
        except Exception as exc:
            logger.debug("Failed to read payload for VM %s: %s", vm_name, exc)
        return None

    def _nics_from_payload(self, payload: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        vm_details = payload.get("cloudstack.vm.details", {}) or {}
        raw_nics = vm_details.get("nics", []) or []
        result: List[Dict[str, Any]] = []
        for idx, nic in enumerate(raw_nics):
            if not isinstance(nic, dict):
                continue
            broadcast_uri = nic.get("broadcastUri")
            vlan = None
            if isinstance(broadcast_uri, str) and broadcast_uri.startswith("vlan://"):
                vlan = self._safe_int(broadcast_uri.split("vlan://", 1)[1], None)
            result.append(
                {
                    "device_id": self._safe_int(nic.get("deviceId"), idx),
                    "mac": nic.get("mac", ""),
                    "ip": nic.get("ip", ""),
                    "netmask": nic.get("netmask", ""),
                    "gateway": nic.get("gateway", ""),
                    "vlan": vlan,
                    "broadcastUri": broadcast_uri,
                }
            )
        return result

    def _nics_from_spec(self, spec: Spec) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        if not spec:
            return result
        for nic in getattr(spec.vm, "nics", []) or []:
            if not isinstance(nic, NIC):
                continue
            result.append(
                {
                    "device_id": nic.deviceId,
                    "mac": nic.mac,
                    "ip": nic.ip,
                    "netmask": nic.netmask,
                    "gateway": nic.gateway,
                    "vlan": nic.vlan,
                    "broadcastUri": nic.broadcastUri,
                }
            )
        return result

    @staticmethod
    def _safe_int(value: Any, default: Optional[int] = 0) -> Optional[int]:
        try:
            if value is None:
                return default
            return int(value)
        except (TypeError, ValueError):
            return default
