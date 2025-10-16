#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VM Lifecycle module for Firecracker Agent.
This module handles VM recovery, discovery, and startup operations.
"""
import logging
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil

from config import ConfigManager
from models import HostDetails, NIC, NetSpec, Spec, StorageSpec, VMDetails, VMExt
from state import StateManager
from utils.filesystem import paths, paths_by_name, read_cfg_json_by_name
from backend.networking import get_backend_by_driver as get_network_backend_by_driver
from .vm_manager import VMManager

logger = logging.getLogger("fc-agent")


class VMLifecycle:
    """Manager for VM lifecycle operations including recovery and discovery."""

    def __init__(self, agent_defaults: Dict[str, Any]):
        self.agent_defaults = agent_defaults
        self.state_manager = StateManager(agent_defaults)
        self.config_manager = ConfigManager(agent_defaults)

    def startup_vm_recovery(self) -> None:
        """Discover and attempt to recover existing VMs on agent startup."""
        logger.info("Starting VM discovery and recovery process...")
        discovered_vms = self.discover_existing_vms()
        # Detect restart type
        if self.state_manager.is_server_restart(discovered_vms):
            logger.info("Server restart detected - will restart previously running VMs")
            self.startup_vm_restart()
        else:
            logger.info("Daemon restart detected - will recover networking for running VMs")
            self.startup_vm_recovery_only()

    def startup_vm_recovery_only(self) -> None:
        """Recover networking for VMs that are already running (daemon restart)."""
        discovered_vms = self.discover_existing_vms()
        logger.info("Discovered %d existing VMs", len(discovered_vms))
        for vm_info in discovered_vms:
            vm_name = vm_info["name"]
            status = vm_info["status"]
            logger.info("VM %s status: %s", vm_name, status)
            if status == "poweron":
                # VM is running, try to recover networking
                logger.info("VM %s is running, recovering networking...", vm_name)
                self.recover_vm_networking(vm_name)
            elif status == "unknown":
                # VM might be running but not responding, try networking
                # recovery
                logger.info("VM %s status unknown, attempting networking recovery...", vm_name)
                self.recover_vm_networking(vm_name)
            else:
                # VM is stopped, no action needed
                logger.info("VM %s is stopped, no recovery needed", vm_name)
        logger.info("VM networking recovery process completed")

    def startup_vm_restart(self) -> None:
        """Restart VMs that were running before server restart."""
        vm_states = self.state_manager.load_vm_states()
        if not vm_states:
            logger.info("No VMs to restart")
            return
        logger.info("Restarting %d VMs that were running before server restart", len(vm_states))
        restart_count = 0
        failed_vms = []
        for vm_name, vm_state in vm_states.items():
            try:
                logger.info("Restarting VM: %s", vm_name)
                # Load VM configuration
                cfg = read_cfg_json_by_name(vm_name)
                if not cfg:
                    logger.error("Failed to load config for VM: %s", vm_name)
                    failed_vms.append(vm_name)
                    continue
                # Convert config to Spec
                spec = self._cfg_to_spec(cfg, vm_name)
                paths = paths_by_name(vm_name)
                # Start VM
                vm_manager = VMManager()
                vm_manager.start_vm(spec, paths)
                restart_count += 1
                logger.info("Successfully restarted VM: %s", vm_name)
            except Exception as e:
                logger.error("Failed to restart VM %s: %s", vm_name, e)
                failed_vms.append(vm_name)
        logger.info("VM restart process completed: %d successful, %d failed", restart_count, len(failed_vms))
        if failed_vms:
            logger.warning("Failed to restart VMs: %s", ", ".join(failed_vms))

    def graceful_vm_shutdown(self) -> None:
        """Gracefully shutdown all running VMs before server restart."""
        logger.info("Starting graceful VM shutdown...")
        try:
            discovered_vms = self.discover_existing_vms()
            shutdown_count = 0
            for vm_info in discovered_vms:
                vm_name = vm_info["name"]
                status = vm_info["status"]
                if status == "poweron":
                    try:
                        logger.info("Gracefully shutting down VM: %s", vm_name)
                        # Use the stop endpoint logic
                        paths = paths_by_name(vm_name)
                        spec = Spec(
                            vm=VMDetails(name=vm_name, cpus=1, minRam=512 * 1024 * 1024, nics=[]),
                            host=HostDetails(
                                firecracker_bin=self.agent_defaults.get("host", {}).get("firecracker_bin"),
                                conf_dir=self.agent_defaults.get("host", {}).get("conf_dir"),
                                run_dir=self.agent_defaults.get("host", {}).get("run_dir"),
                                log_dir=self.agent_defaults.get("host", {}).get("log_dir"),
                                payload_dir=self.agent_defaults.get("host", {}).get("payload_dir"),
                            ),
                            vmext=VMExt(kernel="", boot_args="", mem_mib=512, image=""),
                            storage=StorageSpec(driver="file", volume_file=paths.volume_file),
                            net=NetSpec(driver="linux-bridge-vlan", bridge="br0", nics=[], host_bridge="br0", uplink=""),
                        )
                        vm_manager = VMManager()
                        vm_manager.stop_vm(spec, paths)
                        shutdown_count += 1
                        logger.info("Successfully shut down VM: %s", vm_name)
                    except Exception as e:
                        logger.error("Failed to shutdown VM %s: %s", vm_name, e)
            logger.info("Graceful VM shutdown completed: %d VMs shut down", shutdown_count)
        except Exception as e:
            logger.error("Failed to perform graceful VM shutdown: %s", e)

    def discover_existing_vms(self) -> List[Dict[str, Any]]:
        """Discover existing VMs by scanning configuration directory."""
        try:
            conf_dir_path = self.agent_defaults.get("host", {}).get("conf_dir")
            if not conf_dir_path:
                logger.warning("conf_dir not configured in agent defaults, skipping VM discovery")
                return []
            conf_dir = Path(conf_dir_path)
            if not conf_dir.exists():
                return []
            discovered_vms = []
            # Scan for VM configuration files
            for config_file in conf_dir.glob("*.json"):
                vm_name = config_file.stem
                # Skip non-VM config files
                if vm_name.startswith("network-config-") or vm_name == "vm-states":
                    continue
                try:
                    # Get VM status
                    status = self._get_vm_status_by_name(vm_name)
                    memory_mib = None
                    try:
                        with config_file.open("r", encoding="utf-8") as cfg_fp:
                            config_data = json.load(cfg_fp)
                        machine_cfg = config_data.get("machine-config")
                        if isinstance(machine_cfg, dict):
                            memory_mib = machine_cfg.get("mem_size_mib")
                    except Exception:
                        memory_mib = None
                    vm_info = {
                        "name": vm_name,
                        "status": status,
                        "config_file": str(config_file),
                    }
                    if memory_mib:
                        vm_info["memory_mib"] = memory_mib
                    discovered_vms.append(vm_info)
                except Exception as e:
                    logger.warning("Failed to get status for VM %s: %s", vm_name, e)
            logger.info("Discovered %d existing VMs", len(discovered_vms))
            return discovered_vms
        except Exception as e:
            logger.error("Failed to discover existing VMs: %s", e)
            return []

    def recover_vm_networking(self, vm_name: str, fallback_spec: Optional[Spec] = None) -> bool:
        """Recover networking for a specific VM."""
        try:
            # Load saved network configuration
            network_config = self.config_manager.load_network_config(vm_name)
            if not network_config:
                logger.warning("No saved network config found for VM: %s", vm_name)
            else:
                success = self.config_manager.apply_network_config_from_saved(vm_name, network_config)
                if success:
                    logger.info("Successfully recovered networking for VM: %s", vm_name)
                    return True
                logger.error("Failed to recover networking for VM: %s", vm_name)
            # Fallback using provided spec
            if fallback_spec:
                try:
                    paths_obj = paths(fallback_spec)
                    backend = get_network_backend_by_driver(fallback_spec.net.driver, fallback_spec, paths_obj)
                    backend.prepare()
                    net_cfg = self.config_manager.build_network_config_from_spec(fallback_spec)
                    self.config_manager.save_network_config(fallback_spec.vm.name, net_cfg)
                    logger.info("Recovered networking via provided spec for VM: %s", vm_name)
                    return True
                except Exception as exc:
                    logger.error("Spec-based recovery failed for VM %s: %s", vm_name, exc)
            # Try to reconstruct spec from stored config file
            cfg = read_cfg_json_by_name(vm_name)
            if not cfg:
                return False
            spec = self._cfg_to_spec(cfg, vm_name)
            paths_obj = paths_by_name(vm_name)
            backend = get_network_backend_by_driver(spec.net.driver, spec, paths_obj)
            backend.prepare()
            logger.info("Recovered networking from saved config for VM: %s", vm_name)
            return True
        except Exception as e:
            logger.error("Failed to recover networking for VM %s: %s", vm_name, e)
            return False

    def _get_vm_status_by_name(self, vm_name: str) -> str:
        """Get VM status by VM name."""
        try:
            paths = paths_by_name(vm_name)
            # Build a minimal Spec to use the unified status logic in VMManager
            defaults = self.agent_defaults
            vm_details = VMDetails(name=vm_name, cpus=1, minRam=512 * 1024 * 1024, nics=[])
            host_details = HostDetails(
                firecracker_bin=defaults.get("host", {}).get("firecracker_bin"),
                conf_dir=defaults.get("host", {}).get("conf_dir"),
                run_dir=defaults.get("host", {}).get("run_dir"),
                log_dir=defaults.get("host", {}).get("log_dir"),
                payload_dir=defaults.get("host", {}).get("payload_dir"),
            )
            vmext = VMExt(kernel="", boot_args="", mem_mib=512, image="")
            storage_spec = StorageSpec(driver="file", volume_file=paths.volume_file)
            net_spec = NetSpec(driver="linux-bridge-vlan", bridge="", nics=[], host_bridge="", uplink="")
            spec = Spec(vm=vm_details, host=host_details, vmext=vmext, storage=storage_spec, net=net_spec)

            vm_manager = VMManager()
            return vm_manager.status_vm(spec, paths)
        except Exception as e:
            logger.warning("Failed to get status for VM %s: %s", vm_name, e)
            return "unknown"

    def _cfg_to_spec(self, cfg: Dict[str, Any], vm_name: str) -> Spec:
        """Convert configuration dictionary to Spec object."""
        # This is a simplified conversion - in practice, you'd need to handle
        # the full configuration structure properly
        defaults = self.agent_defaults
        nic_entries: List[NIC] = []
        for iface in cfg.get("network-interfaces") or []:
            iface_id = iface.get("iface_id", "")
            try:
                device_id = int("".join(filter(str.isdigit, iface_id))) if iface_id else len(nic_entries)
            except ValueError:
                device_id = len(nic_entries)
            nic_entries.append(
                NIC(
                    deviceId=device_id,
                    mac=iface.get("guest_mac", ""),
                    ip="",
                    netmask="",
                    gateway="",
                    vlan=None,
                )
            )
        vm_details = VMDetails(
            name=vm_name,
            cpus=cfg.get("machine-config", {}).get("vcpu_count", 1),
            minRam=cfg.get("machine-config", {}).get("mem_size_mib", 512) * 1024 * 1024,
            nics=nic_entries,
        )
        host_details = HostDetails(
            firecracker_bin=defaults.get("host", {}).get("firecracker_bin"),
            conf_dir=defaults.get("host", {}).get("conf_dir"),
            run_dir=defaults.get("host", {}).get("run_dir"),
            log_dir=defaults.get("host", {}).get("log_dir"),
            payload_dir=defaults.get("host", {}).get("payload_dir"),
        )
        # Get image path from config or use a default
        image_path = cfg.get("drives", [{}])[0].get("path_on_host", "")
        if not image_path:
            # Use a default image path from agent defaults
            image_dir = self.agent_defaults.get("host", {}).get("image_dir", "/var/lib/firecracker/images")
            image_path = f"{image_dir}/ubuntu-20.04.img"  # Default image
        
        vmext = VMExt(
            kernel=cfg.get("boot-source", {}).get("kernel_image_path", ""),
            boot_args=cfg.get("boot-source", {}).get("boot_args", ""),
            mem_mib=cfg.get("machine-config", {}).get("mem_size_mib", 512),
            image=image_path,
        )
        storage_spec = StorageSpec(driver="file", volume_file=paths_by_name(vm_name).volume_file)
        net_defaults = defaults.get("net", {})
        net_spec = NetSpec(
            driver=net_defaults.get("driver", "linux-bridge-vlan"),
            bridge=net_defaults.get("host_bridge", ""),
            nics=nic_entries,
            host_bridge=net_defaults.get("host_bridge", ""),
            uplink=net_defaults.get("uplink", ""),
        )
        return Spec(vm=vm_details, host=host_details, vmext=vmext, storage=storage_spec, net=net_spec)
