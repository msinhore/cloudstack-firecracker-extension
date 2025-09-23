#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CLI commands module for Firecracker Agent.
This module contains the command-line interface commands for VM operations.
"""
import logging
from pathlib import Path
from typing import Any, Dict, List

from backend.networking import get_backend_by_driver as get_networking_backend_by_driver
from backend.storage import Paths, get_backend_by_driver
from config import ConfigManager
from models import NIC, Spec
from orchestration import VMManager
from utils.filesystem import paths
from utils.validation import extract_ssh_pubkey_from_payload, fail, read_json, succeed, validate_name

logger = logging.getLogger("fc-agent")


class CLICommands:
    """CLI commands handler."""

    def __init__(self, agent_defaults: Dict[str, Any]):
        defaults = agent_defaults or {}
        if not defaults:
            config = ConfigManager({}).load_agent_config()
            defaults = config.get("defaults", {})
        self.agent_defaults = defaults
        self.vm_manager = VMManager()
        self.config_manager = ConfigManager(self.agent_defaults)

    def prepare(self, spec_file: Path):
        """Prepare storage (volume) only."""
        try:
            obj = read_json(spec_file)
            spec = self._to_spec(obj)
            paths_obj = paths(spec)
            self._storage_prepare(spec, paths_obj)
            succeed({"status": "ok", "message": "volume prepared"}, is_api_mode=False)
        except Exception as e:
            fail(f"Storage preparation failed: {e}", is_api_mode=False)

    def create(self, spec_file: Path, timeout: int = 30):
        """Create and start a VM."""
        try:
            obj = read_json(spec_file)
            spec = self._to_spec(obj)
            validate_name("VM", spec.vm.name)
            paths_obj = paths(spec)
            # Prepare storage
            self._storage_prepare(spec, paths_obj)
            # Optional SSH key injection for CLI (read from the same payload
            # file)
            try:
                ssh_key = extract_ssh_pubkey_from_payload(obj)
                if ssh_key:
                    self._inject_ssh_key_into_image(paths_obj.volume_file, ssh_key, username="root")
            except Exception:
                pass
            # Prepare network
            taps = self._net_prepare(spec, paths_obj)
            try:
                net_cfg = self.config_manager.build_network_config_from_spec(spec)
                self.config_manager.save_network_config(spec.vm.name, net_cfg)
            except Exception as exc:
                logger.warning("Unable to persist network config for %s: %s", spec.vm.name, exc)
            # Write config and start VM
            self.config_manager.write_config(spec, paths_obj)
            self.vm_manager.start_vm(spec, paths_obj, timeout=timeout)
            succeed({"status": "success", "taps": taps}, is_api_mode=False)
        except Exception as e:
            fail(f"VM creation failed: {e}", is_api_mode=False)

    def start(self, spec_file: Path, timeout: int = 30):
        """Start an existing VM."""
        try:
            obj = read_json(spec_file)
            spec = self._to_spec(obj)
            paths_obj = paths(spec)
            if not paths_obj.config_file.exists():
                fail("config file not found; run create or prepare+write-config", is_api_mode=False)
            # Optional SSH key injection for CLI start
            try:
                # reload raw obj to read SSH.PublicKey, inject if present
                ssh_key = extract_ssh_pubkey_from_payload(obj)
                if ssh_key:
                    self._inject_ssh_key_into_image(paths_obj.volume_file, ssh_key, username="root")
            except Exception:
                pass
            # (Re)apply TAP + VLAN before starting (handles host reboots wiping bridge state)
            taps = self._net_prepare(spec, paths_obj)
            self.vm_manager.start_vm(spec, paths_obj, timeout=timeout)
            succeed({"status": "success", "taps": taps}, is_api_mode=False)
        except Exception as e:
            fail(f"VM start failed: {e}", is_api_mode=False)

    def stop(self, spec_file: Path, timeout: int = 30):
        """Stop a running VM."""
        try:
            obj = read_json(spec_file)
            spec = self._to_spec(obj)
            paths_obj = paths(spec)
            self.vm_manager.stop_vm(spec, paths_obj, timeout=timeout)
            succeed({"status": "success", "message": f"VM {spec.vm.name} stopped"}, is_api_mode=False)
        except Exception as e:
            fail(f"VM stop failed: {e}", is_api_mode=False)

    def reboot(self, spec_file: Path, timeout: int = 30):
        """Reboot a VM."""
        try:
            obj = read_json(spec_file)
            spec = self._to_spec(obj)
            paths_obj = paths(spec)
            self.vm_manager.reboot_vm(spec, paths_obj, timeout=timeout)
            succeed({"status": "success", "message": f"VM {spec.vm.name} rebooted"}, is_api_mode=False)
        except Exception as e:
            fail(f"VM reboot failed: {e}", is_api_mode=False)

    def delete(self, spec_file: Path):
        """Delete a VM."""
        try:
            obj = read_json(spec_file)
            spec = self._to_spec(obj)
            paths_obj = paths(spec)
            self.vm_manager.delete_vm(spec, paths_obj)
            try:
                self.config_manager.cleanup_network_config(spec.vm.name)
                self.config_manager.cleanup_payload(spec.vm.name)
            except Exception as exc:
                logger.warning("Unable to cleanup artifacts for %s: %s", spec.vm.name, exc)
            succeed({"status": "success", "message": f"VM {spec.vm.name} deleted"}, is_api_mode=False)
        except Exception as e:
            fail(f"VM delete failed: {e}", is_api_mode=False)

    def recover(self, spec_file: Path):
        """Recover networking/process state for an existing VM."""
        try:
            obj = read_json(spec_file)
            spec = self._to_spec(obj)
            paths_obj = paths(spec)
            taps = self._net_prepare(spec, paths_obj)
            succeed({"status": "success", "vm_name": spec.vm.name, "taps": taps}, is_api_mode=False)
        except Exception as e:
            fail(f"VM recover failed: {e}", is_api_mode=False)

    def vm_status(self, spec_file: Path):
        """Get VM status."""
        try:
            obj = read_json(spec_file)
            spec = self._to_spec(obj)
            paths_obj = paths(spec)
            status = self.vm_manager.status_vm(spec, paths_obj)
            succeed({"status": "success", "vm_name": spec.vm.name, "power_state": status})
        except Exception as e:
            fail(f"VM status check failed: {e}", is_api_mode=False)

    def net_prepare_cmd(self, spec_file: Path):
        """Prepare network for VM."""
        try:
            obj = read_json(spec_file)
            spec = self._to_spec(obj)
            paths_obj = paths(spec)
            taps = self._net_prepare(spec, paths_obj)
            succeed({"status": "success", "taps": taps}, is_api_mode=False)
        except Exception as e:
            fail(f"Network preparation failed: {e}", is_api_mode=False)

    def net_teardown_cmd(self, spec_file: Path):
        """Teardown network for VM."""
        try:
            obj = read_json(spec_file)
            spec = self._to_spec(obj)
            paths_obj = paths(spec)
            self._net_teardown(spec, paths_obj)
            try:
                self.config_manager.cleanup_network_config(spec.vm.name)
            except Exception as exc:
                logger.warning("Unable to cleanup network config for %s: %s", spec.vm.name, exc)
            succeed({"status": "success", "message": "Network torn down"}, is_api_mode=False)
        except Exception as e:
            fail(f"Network teardown failed: {e}", is_api_mode=False)

    def write_config_cmd(self, spec_file: Path):
        """Write VM configuration."""
        try:
            obj = read_json(spec_file)
            spec = self._to_spec(obj)
            paths_obj = paths(spec)
            self.config_manager.write_config(spec, paths_obj)
            succeed({"status": "success", "message": "Configuration written"}, is_api_mode=False)
        except Exception as e:
            fail(f"Config write failed: {e}", is_api_mode=False)

    # Helper methods
    def _to_spec(self, obj: Dict[str, Any]) -> Spec:
        """Convert request payload to Spec object."""
        from models import HostDetails, NetSpec, StorageSpec, VMDetails, VMExt

        vm_details = obj.get("cloudstack.vm.details", {})
        nic_entries: List[NIC] = []
        for nic_data in vm_details.get("nics", []) or []:
            nic_entries.append(
                NIC(
                    deviceId=nic_data.get("deviceId", 0),
                    mac=nic_data.get("mac", ""),
                    ip=nic_data.get("ip", ""),
                    netmask=nic_data.get("netmask", ""),
                    gateway=nic_data.get("gateway", ""),
                    vlan=int(nic_data.get("broadcastUri", "vlan://0").split("://")[1]) if "vlan://" in nic_data.get("broadcastUri", "") else None,
                )
            )
        vm = VMDetails(
            name=vm_details.get("name", "unknown"),
            cpus=vm_details.get("cpu", 1),
            minRam=vm_details.get("memory", 512) * 1024 * 1024,
            nics=nic_entries,
        )
        host = HostDetails(
            firecracker_bin=self.agent_defaults.get("host", {}).get("firecracker_bin"),
            conf_dir=self.agent_defaults.get("host", {}).get("conf_dir"),
            run_dir=self.agent_defaults.get("host", {}).get("run_dir"),
            log_dir=self.agent_defaults.get("host", {}).get("log_dir"),
        )
        # Get image path from VM details or use a default
        image_path = vm_details.get("image", "")
        if not image_path:
            # Use a default image path from agent defaults
            image_dir = self.agent_defaults.get("host", {}).get("image_dir", "/var/lib/firecracker/images")
            image_path = f"{image_dir}/ubuntu-20.04.img"  # Default image
        
        vmext = VMExt(
            kernel=vm_details.get("kernel", ""),
            boot_args=vm_details.get("boot_args", ""),
            mem_mib=vm_details.get("memory", 512),
            image=image_path,
        )
        storage_volume_dir = self.agent_defaults.get("storage", {}).get("volume_dir")
        if not storage_volume_dir:
            fail("volume_dir not configured in agent defaults", is_api_mode=False)
        storage = StorageSpec(
            driver="file", volume_file=Path(storage_volume_dir) / f"{vm.name}.img"
        )
        # Get networking driver from agent defaults
        net_driver = self.agent_defaults.get("net", {}).get("driver", "linux-bridge-vlan")
        net_bridge = self.agent_defaults.get("net", {}).get("host_bridge", "")
        net_host_bridge = self.agent_defaults.get("net", {}).get("host_bridge", "")
        net_uplink = self.agent_defaults.get("net", {}).get("uplink", "")
        net = NetSpec(driver=net_driver, bridge=net_bridge, nics=nic_entries, host_bridge=net_host_bridge, uplink=net_uplink)
        return Spec(vm=vm, host=host, vmext=vmext, storage=storage, net=net)

    def _storage_prepare(self, spec: Spec, paths_obj: Paths) -> None:
        """Prepare storage for VM."""
        backend = get_backend_by_driver(spec.storage.driver, spec, paths_obj)
        backend.prepare()

    def _net_prepare(self, spec: Spec, paths_obj: Paths) -> List[str]:
        """Prepare network for VM."""
        backend = get_networking_backend_by_driver(spec.net.driver, spec, paths_obj)
        return backend.prepare()

    def _net_teardown(self, spec: Spec, paths_obj: Paths) -> None:
        """Teardown network for VM."""
        backend = get_networking_backend_by_driver(spec.net.driver, spec, paths_obj)
        backend.teardown()

    def _inject_ssh_key_into_image(self, volume_file: Path, ssh_key: str, username: str = "root") -> None:
        """Inject SSH key into VM image."""
        # This is a simplified implementation - in practice, you'd need
        # to mount the image and inject the key properly
        logger.info("SSH key injection not implemented in CLI module")
        pass
