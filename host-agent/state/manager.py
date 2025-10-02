#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
State management module for Firecracker Agent.
This module handles VM state persistence and recovery operations.
"""
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger("fc-agent")


class StateManager:
    """Manager for VM state persistence and recovery."""

    def __init__(self, agent_defaults: Dict[str, Any]):
        self.agent_defaults = agent_defaults

    def save_vm_states(self, discovered_vms: list) -> None:
        """Save current VM states to persistent storage for recovery after server restart."""
        try:
            run_dir_path = self.agent_defaults.get("host", {}).get("run_dir")
            if not run_dir_path:
                logger.warning("run_dir not configured in agent defaults, skipping VM states save")
                return
            state_file = Path(run_dir_path) / "vm-states.json"
            state_file.parent.mkdir(parents=True, exist_ok=True)
            vm_states = {}
            for vm_info in discovered_vms:
                vm_name = vm_info["name"]
                status = vm_info["status"]
                # Only save running VMs for restart recovery
                if status == "poweron":
                    vm_states[vm_name] = {
                        "name": vm_name,
                        "status": status,
                        "timestamp": time.time(),
                        "config_file": vm_info["config_file"],
                    }
            with state_file.open("w", encoding="utf-8") as f:
                json.dump(vm_states, f, indent=2)
            logger.info("Saved VM states: %d running VMs", len(vm_states))
        except Exception as e:
            logger.error("Failed to save VM states: %s", e)

    def load_vm_states(self) -> Dict[str, Any]:
        """Load VM states from persistent storage."""
        try:
            run_dir_path = self.agent_defaults.get("host", {}).get("run_dir")
            if not run_dir_path:
                logger.warning("run_dir not configured in agent defaults, skipping VM states load")
                return {}
            state_file = Path(run_dir_path) / "vm-states.json"
            if not state_file.exists():
                return {}
            with state_file.open("r", encoding="utf-8") as f:
                vm_states = json.load(f)
            logger.info("Loaded VM states: %d VMs", len(vm_states))
            return vm_states
        except Exception as e:
            logger.error("Failed to load VM states: %s", e)
            return {}

    def is_server_restart(self, discovered_vms: list) -> bool:
        """Detect if this is a server restart (vs daemon restart) by checking VM states."""
        try:
            vm_states = self.load_vm_states()
            if not vm_states:
                return False
            # Check if any VMs from saved state are still running
            running_vms = {vm["name"]: vm["status"] for vm in discovered_vms if vm["status"] == "poweron"}
            # If saved VMs are not running, this is likely a server restart
            saved_vm_names = set(vm_states.keys())
            current_running_names = set(running_vms.keys())
            # If no saved VMs are currently running, it's a server restart
            if not saved_vm_names.intersection(current_running_names):
                logger.info("Server restart detected: no saved VMs are currently running")
                return True
            logger.info("Daemon restart detected: saved VMs are still running")
            return False
        except Exception as e:
            logger.error("Failed to detect restart type: %s", e)
            return False
