#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
API handlers module for Firecracker Agent.
This module contains the API endpoint handlers for VM operations.
"""
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from backend.storage import Paths, StorageError, get_backend_by_driver
from config import ConfigManager
from models import Spec, SpecRequest
from orchestration import VMLifecycle, VMManager
from state import StateManager
from utils.filesystem import paths
from utils.validation import validate_name

logger = logging.getLogger("fc-agent")


class APIHandlers:

    def __init__(self, agent_defaults: Dict[str, Any]):
        self.agent_defaults = agent_defaults
        self.vm_manager = VMManager()
        self.vm_lifecycle = VMLifecycle(agent_defaults)
        self.config_manager = ConfigManager(agent_defaults)
        self.state_manager = StateManager(agent_defaults)

    def api_create(self, req: SpecRequest) -> Dict[str, Any]:
        """Prepare storage+network, write config and start the VM."""
        # Dump the raw incoming spec as early as possible (before any parsing/try),
        # so we always preserve the original request for debugging.
        raw_spec = req.spec if req and getattr(req, "spec", None) else {}
        vm_name = None
        try:
            vm_name = raw_spec.get("cloudstack.vm.details", {}).get("name")
        except Exception:
            pass
        if not vm_name:
            vm_name = f"unknown-{int(time.time())}"
        payload_dir_path = self.agent_defaults.get("host", {}).get("payload_dir")
        if not payload_dir_path:
            logger.warning("payload_dir not configured in agent defaults, skipping payload persistence")
        else:
            payload_dir = Path(payload_dir_path)
            payload_dir.mkdir(parents=True, exist_ok=True)
            payload_file = payload_dir / f"create-spec-{vm_name}.json"
            with payload_file.open("w", encoding="utf-8") as f:
                json.dump(raw_spec, f, indent=2)
        spec: Optional[Spec] = None  # type: ignore[assignment]
        paths_obj: Optional[Paths] = None
        vm_created = False
        storage_prepared = False
        network_prepared = False
        try:
            spec = self._to_spec(raw_spec)
            self._ensure_valid_vm_name(spec)
            paths_obj = paths(spec)
            self._storage_prepare(spec, paths_obj)
            storage_prepared = True
            # Optional SSH key injection before networking
            try:
                key = None
                details = raw_spec.get("cloudstack.vm.details", {}).get("details", {})
                if isinstance(details, dict):
                    key = details.get("SSH.PublicKey") or details.get("ssh_public_key")
                if not key:
                    key = raw_spec.get("ssh_public_key")
                if key:
                    from utils.filesystem import inject_ssh_key_into_path
                    inject_ssh_key_into_path(paths_obj.volume_file, key, username="root")
            except Exception:
                pass
            self._net_prepare(spec, paths_obj)
            network_prepared = True
            try:
                net_cfg = self.config_manager.build_network_config_from_spec(spec)
                self.config_manager.save_network_config(spec.vm.name, net_cfg)
            except Exception as exc:
                logger.warning("Failed to persist network config for VM %s: %s", spec.vm.name, exc)
            self.config_manager.write_config(spec, paths_obj)
            self.vm_manager.start_vm(spec, paths_obj)
            vm_created = True
            return {
                "status": "success",
                "message": f"VM {spec.vm.name} created and started successfully",
                "vm_name": spec.vm.name,
            }
        except Exception as e:
            logger.exception("VM creation failed: %s", e)
            # Cleanup on failure
            if vm_created and spec and paths_obj:
                try:
                    self.vm_manager.stop_vm(spec, paths_obj)
                except Exception:
                    pass
            if storage_prepared and spec and paths_obj:
                try:
                    self._storage_teardown(spec, paths_obj)
                except Exception:
                    pass
            if network_prepared and spec and paths_obj:
                try:
                    self._net_teardown(spec, paths_obj)
                except Exception:
                    pass
            status_code = 400 if isinstance(e, (ValueError, FileNotFoundError)) else 500
            detail = str(e) if status_code == 400 else f"VM creation failed: {e}"
            raise HTTPException(status_code=status_code, detail=detail)

    def v1_list_vms(self) -> Dict[str, Any]:
        """List all existing VMs discovered by the agent."""
        try:
            discovered_vms = self.vm_lifecycle.discover_existing_vms()
            # Format response
            vms = []
            for vm_info in discovered_vms:
                vm_data = {
                    "name": vm_info["name"],
                    "status": vm_info["status"],
                    "cpus": vm_info.get("cpus", 1),
                    "memory_mib": vm_info.get("memory_mib", 512),
                    "nics": vm_info.get("nics", 0),
                    "config_file": vm_info["config_file"],
                }
                vms.append(vm_data)
            return {"status": "success", "message": f"Found {len(vms)} VMs", "vms": vms, "count": len(vms)}
        except Exception as e:
            logger.exception("Failed to list VMs: %s", e)
            raise HTTPException(status_code=500, detail=f"Failed to list VMs: {e}")

    def v1_vm_recover_by_name(self, vm_name: str, req: Optional[SpecRequest] = None) -> Dict[str, Any]:
        """Attempt to recover networking for a specific VM."""
        try:
            fallback_spec: Optional[Spec] = None
            if req and getattr(req, "spec", None):
                fallback_spec = self._to_spec(req.spec)
                try:
                    self._ensure_valid_vm_name(fallback_spec)
                except HTTPException:
                    fallback_spec = None
            success = self.vm_lifecycle.recover_vm_networking(vm_name, fallback_spec)
            if success:
                return {"status": "success", "message": f"Recovered networking for VM {vm_name}"}
            else:
                raise HTTPException(status_code=404, detail=f"VM {vm_name} not found or recovery failed")
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("VM recovery failed: %s", e)
            raise HTTPException(status_code=500, detail=f"VM recovery failed: {e}")

    def v1_graceful_shutdown(self) -> Dict[str, Any]:
        """Gracefully shutdown all running VMs (for server restart)."""
        try:
            self.vm_lifecycle.graceful_vm_shutdown()
            return {"status": "success", "message": "All VMs shut down gracefully"}
        except Exception as e:
            logger.exception("Graceful shutdown failed: %s", e)
            raise HTTPException(status_code=500, detail=f"Graceful shutdown failed: {e}")

    def v1_save_states(self) -> Dict[str, Any]:
        """Save current VM states for recovery."""
        try:
            discovered_vms = self.vm_lifecycle.discover_existing_vms()
            self.state_manager.save_vm_states(discovered_vms)
            return {"status": "success", "message": "VM states saved successfully"}
        except Exception as e:
            logger.exception("Save states failed: %s", e)
            raise HTTPException(status_code=500, detail=f"Save states failed: {e}")

    def v1_get_saved_states(self) -> Dict[str, Any]:
        """Get saved VM states."""
        try:
            vm_states = self.state_manager.load_vm_states()
            return {
                "status": "success",
                "message": f"Retrieved {len(vm_states)} saved VM states",
                "vm_states": vm_states,
            }
        except Exception as e:
            logger.exception("Get saved states failed: %s", e)
            raise HTTPException(status_code=500, detail=f"Get saved states failed: {e}")

    def v1_get_network_config(self, vm_name: str) -> Dict[str, Any]:
        """Get network configuration for a VM."""
        try:
            network_config = self.config_manager.load_network_config(vm_name)
            if network_config:
                return {"status": "success", "vm_name": vm_name, "network_config": network_config}
            else:
                raise HTTPException(status_code=404, detail=f"No network config found for VM {vm_name}")
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Get network config failed: %s", e)
            raise HTTPException(status_code=500, detail=f"Get network config failed: {e}")

    def v1_apply_network_config(self, vm_name: str) -> Dict[str, Any]:
        """Apply saved network configuration to a VM."""
        try:
            network_config = self.config_manager.load_network_config(vm_name)
            if not network_config:
                raise HTTPException(status_code=404, detail=f"No network config found for VM {vm_name}")
            success = self.config_manager.apply_network_config_from_saved(vm_name, network_config)
            if success:
                return {"status": "success", "message": f"Network config applied to VM {vm_name}"}
            else:
                raise HTTPException(status_code=500, detail=f"Failed to apply network config to VM {vm_name}")
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Apply network config failed: %s", e)
            raise HTTPException(status_code=500, detail=f"Apply network config failed: {e}")

    def v1_delete_network_config(self, vm_name: str) -> Dict[str, Any]:
        """Delete network configuration for a VM."""
        try:
            self.config_manager.cleanup_network_config(vm_name)
            return {"status": "success", "message": f"Network config deleted for VM {vm_name}"}
        except Exception as e:
            logger.exception("Delete network config failed: %s", e)
            raise HTTPException(status_code=500, detail=f"Delete network config failed: {e}")

    def v1_recover_all_vms(self) -> Dict[str, Any]:
        """Recover all VMs (networking for running VMs)."""
        try:
            self.vm_lifecycle.startup_vm_recovery_only()
            return {"status": "success", "message": "VM recovery process completed"}
        except Exception as e:
            logger.exception("Recover all VMs failed: %s", e)
            raise HTTPException(status_code=500, detail=f"Recover all VMs failed: {e}")

    def v1_vm_status_by_name(self, vm_name: str) -> Dict[str, Any]:
        """Get VM status by name."""
        try:
            status = self.vm_lifecycle._get_vm_status_by_name(vm_name)
            return {"status": "success", "vm_name": vm_name, "power_state": status}
        except Exception as e:
            logger.exception("Get VM status failed: %s", e)
            raise HTTPException(status_code=500, detail=f"Get VM status failed: {e}")

    def v1_vm_stop_by_name(self, vm_name: str) -> Dict[str, Any]:
        """Stop VM by name."""
        try:
            from utils.filesystem import paths_by_name, read_cfg_json_by_name

            paths_obj = paths_by_name(vm_name)
            cfg = read_cfg_json_by_name(vm_name)
            if cfg:
                spec = self._cfg_to_spec(cfg, vm_name)
            else:
                # Idempotent: build a minimal spec using defaults to attempt best-effort stop
                from models import HostDetails, NetSpec, StorageSpec, VMDetails, VMExt

                vm_details = VMDetails(name=vm_name, cpus=1, minRam=512 * 1024 * 1024, nics=[])
                host_details = HostDetails(
                    firecracker_bin=self.agent_defaults.get("host", {}).get("firecracker_bin"),
                    conf_dir=self.agent_defaults.get("host", {}).get("conf_dir"),
                    run_dir=self.agent_defaults.get("host", {}).get("run_dir"),
                    log_dir=self.agent_defaults.get("host", {}).get("log_dir"),
                    payload_dir=self.agent_defaults.get("host", {}).get("payload_dir"),
                )
                vmext = VMExt(kernel="", boot_args="", mem_mib=512, image="")
                storage_spec = StorageSpec(driver="file", volume_file=paths_obj.volume_file)
                net_spec = NetSpec(driver="linux-bridge-vlan", bridge="", nics=[], host_bridge="", uplink="")
                spec = Spec(vm=vm_details, host=host_details, vmext=vmext, storage=storage_spec, net=net_spec)
            # Best-effort stop; treat non-existence as already stopped
            try:
                self.vm_manager.stop_vm(spec, paths_obj)
            except Exception:
                pass
            return {"status": "success", "message": f"VM {vm_name} stopped (idempotent)", "vm_name": vm_name}
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Stop VM failed: %s", e)
            raise HTTPException(status_code=500, detail=f"Stop VM failed: {e}")

    def v1_vm_delete_by_name(self, vm_name: str) -> Dict[str, Any]:
        """Delete VM by name."""
        try:
            from utils.filesystem import paths_by_name, read_cfg_json_by_name

            cfg = read_cfg_json_by_name(vm_name)
            if not cfg:
                raise HTTPException(status_code=404, detail=f"VM {vm_name} not found")
            spec = self._cfg_to_spec(cfg, vm_name)
            paths_obj = paths_by_name(vm_name)
            self.vm_manager.delete_vm(spec, paths_obj)
            try:
                self.config_manager.cleanup_network_config(vm_name)
            except Exception as exc:
                logger.warning("Failed to cleanup network config for VM %s: %s", vm_name, exc)
            return {"status": "success", "message": f"VM {vm_name} deleted successfully", "vm_name": vm_name}
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Delete VM failed: %s", e)
            raise HTTPException(status_code=500, detail=f"Delete VM failed: {e}")

    def v1_vm_reboot_by_name(self, vm_name: str) -> Dict[str, Any]:
        """Reboot VM by name."""
        try:
            from utils.filesystem import paths_by_name, read_cfg_json_by_name

            cfg = read_cfg_json_by_name(vm_name)
            if not cfg:
                raise HTTPException(status_code=404, detail=f"VM {vm_name} not found")
            spec = self._cfg_to_spec(cfg, vm_name)
            paths_obj = paths_by_name(vm_name)
            self.vm_manager.reboot_vm(spec, paths_obj)
            return {"status": "success", "message": f"VM {vm_name} rebooted successfully", "vm_name": vm_name}
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Reboot VM failed: %s", e)
            raise HTTPException(status_code=500, detail=f"Reboot VM failed: {e}")

    def v1_vm_start_by_name(self, vm_name: str, req: SpecRequest) -> Dict[str, Any]:
        """Start VM by name (uses existing config on disk)."""
        try:
            from utils.filesystem import paths_by_name, read_cfg_json_by_name

            cfg = read_cfg_json_by_name(vm_name)
            if not cfg:
                raise HTTPException(status_code=404, detail=f"VM {vm_name} not found")
            spec = self._cfg_to_spec(cfg, vm_name)
            paths_obj = paths_by_name(vm_name)
            self.vm_manager.start_vm(spec, paths_obj)
            return {"status": "success", "message": f"VM {vm_name} started successfully", "vm_name": vm_name}
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("VM start failed: %s", e)
            raise HTTPException(status_code=500, detail=f"VM start failed: {e}")

    def healthz(self) -> Dict[str, Any]:
        """Health check endpoint."""
        return {"status": "healthy", "message": "Firecracker Agent is running"}

    def v1_index(self) -> Dict[str, Any]:
        """API index endpoint."""
        return {
            "status": "success",
            "message": "Firecracker Agent API",
            "version": "1.0.0",
            "endpoints": [
                "/v1/vms",
                "/v1/vms/{name}/start",
                "/v1/vms/{name}/stop",
                "/v1/vms/{name}/reboot",
                "/v1/vms/{name}",
                "/v1/vms/{name}/recover",
                "/v1/vms/{name}/status",
                "/v1/network-config/{name}",
                "/v1/network-config/{name}/apply",
                "DELETE /v1/network-config/{name}",
                "/v1/graceful-shutdown",
                "/v1/save-states",
                "/v1/saved-states",
                "/v1/recover-all",
                "/healthz",
            ],
        }

    def v1_version(self) -> Dict[str, Any]:
        """Version endpoint."""
        return {"version": "1.0.0", "name": "Firecracker Agent"}

    def v1_health_alias(self) -> Dict[str, Any]:
        """Health check alias."""
        return self.healthz()

    def v1_config_effective(self) -> Dict[str, Any]:
        """Get effective configuration."""
        return {"status": "success", "config": self.agent_defaults}

    # Helper methods
    def _ensure_valid_vm_name(self, spec: Spec) -> None:
        try:
            validate_name("VM", spec.vm.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _to_spec(self, obj: Dict[str, Any]) -> Spec:
        """Convert request payload to Spec object."""
        # This is a simplified implementation - in practice, you'd need
        # to handle the full CloudStack payload structure
        from models import HostDetails, NetSpec, StorageSpec, VMDetails, VMExt, NIC

        vm_details = obj.get("cloudstack.vm.details", {})
        
        # Extract NIC information from CloudStack spec
        nics = []
        vm_nics = vm_details.get("nics", [])
        for nic_data in vm_nics:
            nic = NIC(
                deviceId=nic_data.get("deviceId", 0),
                mac=nic_data.get("mac", ""),
                ip=nic_data.get("ip", ""),
                netmask=nic_data.get("netmask", ""),
                gateway=nic_data.get("gateway", ""),
                vlan=int(nic_data.get("broadcastUri", "vlan://0").split("://")[1]) if "vlan://" in nic_data.get("broadcastUri", "") else None,
                broadcastUri=nic_data.get("broadcastUri", "")
            )
            nics.append(nic)
        
        vm = VMDetails(
            name=vm_details.get("name", "unknown"),
            cpus=vm_details.get("cpu", 1),
            minRam=vm_details.get("memory", 512) * 1024 * 1024,
            nics=nics,
        )
        host = HostDetails(
            firecracker_bin=self.agent_defaults.get("host", {}).get("firecracker_bin"),
            conf_dir=self.agent_defaults.get("host", {}).get("conf_dir"),
            run_dir=self.agent_defaults.get("host", {}).get("run_dir"),
            log_dir=self.agent_defaults.get("host", {}).get("log_dir"),
            payload_dir=self.agent_defaults.get("host", {}).get("payload_dir"),
        )
        # Get image path from externaldetails.virtualmachine.image or use a default
        external_details = obj.get("externaldetails", {})
        vm_ext_details = external_details.get("virtualmachine", {})
        image_name = vm_ext_details.get("image", "")
        
        if image_name:
            # Construct full path using image_dir and the image name from CloudStack
            image_dir = self.agent_defaults.get("host", {}).get("image_dir", "/var/lib/firecracker/images")
            image_path = f"{image_dir}/{image_name}"
        else:
            # Use a default image path from agent defaults
            image_dir = self.agent_defaults.get("host", {}).get("image_dir", "/var/lib/firecracker/images")
            image_path = f"{image_dir}/ubuntu-20.04.img"  # Default image
        
        # Get kernel path from externaldetails.virtualmachine.kernel or use a default
        kernel_name = vm_ext_details.get("kernel", "")
        
        if kernel_name:
            # Construct full path using kernel_dir and the kernel name from CloudStack
            kernel_dir = self.agent_defaults.get("host", {}).get("kernel_dir", "/var/lib/firecracker/kernel")
            kernel_path = f"{kernel_dir}/{kernel_name}"
        else:
            # Use a default kernel path from agent defaults
            kernel_dir = self.agent_defaults.get("host", {}).get("kernel_dir", "/var/lib/firecracker/kernel")
            kernel_path = f"{kernel_dir}/vmlinux.bin"  # Default kernel
        
        vmext = VMExt(
            kernel=kernel_path,
            boot_args=vm_ext_details.get("boot_args", ""),
            mem_mib=vm_details.get("memory", 512),
            image=image_path,
        )
        storage_volume_dir = self.agent_defaults.get("storage", {}).get("volume_dir")
        if not storage_volume_dir:
            logger.error("volume_dir not found in agent_defaults")
            logger.error("agent_defaults structure: %s", self.agent_defaults)
            raise HTTPException(status_code=500, detail="volume_dir not configured in agent defaults")
        storage = StorageSpec(
            driver="file", volume_file=Path(storage_volume_dir) / f"{vm.name}.img"
        )
        # Determine networking driver based on CloudStack networking configuration
        # Default to linux-bridge-vlan for VLAN-based networking
        net_driver = "linux-bridge-vlan"
        net_bridge = self.agent_defaults.get("net", {}).get("host_bridge", "")
        net_host_bridge = self.agent_defaults.get("net", {}).get("host_bridge", "")
        net_uplink = self.agent_defaults.get("net", {}).get("uplink", "")
        net = NetSpec(driver=net_driver, bridge=net_bridge, nics=nics, host_bridge=net_host_bridge, uplink=net_uplink)
        return Spec(vm=vm, host=host, vmext=vmext, storage=storage, net=net)

    def _cfg_to_spec(self, cfg: Dict[str, Any], vm_name: str) -> Spec:
        """Convert configuration to Spec object."""
        from models import HostDetails, NetSpec, NIC, StorageSpec, VMDetails, VMExt

        from utils.filesystem import paths_by_name

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
            firecracker_bin=self.agent_defaults.get("host", {}).get("firecracker_bin"),
            conf_dir=self.agent_defaults.get("host", {}).get("conf_dir"),
            run_dir=self.agent_defaults.get("host", {}).get("run_dir"),
            log_dir=self.agent_defaults.get("host", {}).get("log_dir"),
            payload_dir=self.agent_defaults.get("host", {}).get("payload_dir"),
        )
        # Get image path from config or use a default
        image_path = cfg.get("drives", [{}])[0].get("path_on_host", "")
        if not image_path:
            # Use a default image path from agent defaults
            image_dir = self.agent_defaults.get("host", {}).get("image_dir", "/var/lib/firecracker/images")
            image_path = f"{image_dir}/ubuntu-20.04.img"  # Default image
        
        # Get kernel path from config or use a default
        kernel_name = cfg.get("boot-source", {}).get("kernel_image_path", "")
        if not kernel_name:
            # Use a default kernel path from agent defaults
            kernel_dir = self.agent_defaults.get("host", {}).get("kernel_dir", "/var/lib/firecracker/kernel")
            kernel_name = f"{kernel_dir}/vmlinux.bin"  # Default kernel
        
        vmext = VMExt(
            kernel=kernel_name,
            boot_args=cfg.get("boot-source", {}).get("boot_args", ""),
            mem_mib=cfg.get("machine-config", {}).get("mem_size_mib", 512),
            image=image_path,
        )
        storage_spec = StorageSpec(driver="file", volume_file=paths_by_name(vm_name).volume_file)
        # Get networking driver from agent defaults
        net_defaults = self.agent_defaults.get("net", {})
        net_spec = NetSpec(
            driver=net_defaults.get("driver", "linux-bridge-vlan"),
            bridge=net_defaults.get("host_bridge", ""),
            nics=nic_entries,
            host_bridge=net_defaults.get("host_bridge", ""),
            uplink=net_defaults.get("uplink", ""),
        )
        return Spec(vm=vm_details, host=host_details, vmext=vmext, storage=storage_spec, net=net_spec)

    def _storage_prepare(self, spec: Spec, paths_obj: Paths) -> None:
        """Prepare storage for VM."""
        backend = get_backend_by_driver(spec.storage.driver, spec, paths_obj)
        backend.prepare()

    def _storage_teardown(self, spec: Spec, paths_obj: Paths) -> None:
        """Teardown storage for VM."""
        backend = get_backend_by_driver(spec.storage.driver, spec, paths_obj)
        backend.teardown()

    def _net_prepare(self, spec: Spec, paths_obj: Paths) -> None:
        """Prepare network for VM."""
        from backend.networking import get_backend_by_driver as get_networking_backend_by_driver

        backend = get_networking_backend_by_driver(spec.net.driver, spec, paths_obj)
        backend.prepare()

    def _net_teardown(self, spec: Spec, paths_obj: Paths) -> None:
        """Teardown network for VM."""
        from backend.networking import get_backend_by_driver as get_networking_backend_by_driver

        backend = get_networking_backend_by_driver(spec.net.driver, spec, paths_obj)
        backend.teardown()
