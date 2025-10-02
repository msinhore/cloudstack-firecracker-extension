# backend/networking/__init__.py
from __future__ import annotations

from typing import Dict, Type

from .base import NetworkingBackend, NetworkingError
from .linux_bridge_vlan import LinuxBridgeVlanBackend
from .ovs_vlan import OvsVlanBackend

# Map of supported drivers
_BACKENDS: Dict[str, Type[NetworkingBackend]] = {
    "linux-bridge-vlan": LinuxBridgeVlanBackend,
    "ovs-vlan": OvsVlanBackend,
}


def get_backend_by_driver(driver: str, spec, paths) -> NetworkingBackend:
    """
    Returns a networking backend instance for the specified 'driver'.
    """
    key = (driver or "").strip().lower()
    cls = _BACKENDS.get(key)
    if not cls:
        raise NetworkingError(f"Unsupported networking driver '{driver}'")
    return cls(spec, paths)
