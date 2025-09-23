"""
Linux Bridge VLAN Networking Backend
====================================
This module implements the linux-bridge-vlan networking backend for Firecracker VMs.
It provides TAP interface creation, bridge attachment, and VLAN configuration.
"""

from __future__ import annotations

import logging
from typing import List, Optional
from pathlib import Path

from pyroute2 import IPRoute, NetlinkError

from .base import NetworkingBackend, NetworkingError
from .helpers import (
    bridge_vlan,
    cleanup_uplink_vlans,
    configure_bridge_port_flags,
    detect_uplink,
    ifname,
    setup_fdb_entry,
    setup_fdb_entry_bridge,
    tap_name,
    vid_from_buri,
)

logger = logging.getLogger(__name__)


def _get_uplink_mtu(ip: IPRoute, uplink: Optional[str]) -> Optional[int]:
    """Get MTU from uplink interface to avoid fragmentation."""
    if not uplink:
        return None
    try:
        for link in ip.get_links():
            if link.get_attr("IFLA_IFNAME") == uplink:
                mtu = link.get_attr("IFLA_MTU")
                if mtu and mtu > 0:
                    return mtu
        logger.warning("LinuxBridgeVlanBackend: could not get MTU for uplink %s", uplink)
        return None
    except Exception as e:
        logger.warning("LinuxBridgeVlanBackend: failed to get MTU for uplink %s: %s", uplink, e)
        return None


class LinuxBridgeVlanBackend(NetworkingBackend):
    """Linux bridge with VLAN support networking backend."""

    def __init__(self, spec, paths):
        """Initialize the Linux bridge VLAN backend.
        Args:
            spec: VM specification containing network configuration
            paths: VM file paths
        """
        super().__init__(spec, paths)
        self.bridge = spec.net.host_bridge
        self.uplink = spec.net.uplink

    def _uplink_is_bridge_port(self, uplink_name: Optional[str]) -> bool:
        """Return True if the given uplink is enslaved to this backend's bridge."""
        if not uplink_name:
            return False
        try:
            return Path(f"/sys/class/net/{self.bridge}/brif/{uplink_name}").exists()
        except Exception:
            return False

    def prepare(self) -> List[str]:
        """Create/attach TAPs to the bridge and configure VLAN access + uplink tagging.
        Also primes FDB entries for a short time to help first DHCP unicast reach the TAP.
        Returns:
            List of TAP names created/ensured
        Raises:
            NetworkingError: If network preparation fails
        """
        try:
            uplink = self.uplink or detect_uplink(self.bridge)
            created_taps: List[str] = []
            ip = IPRoute()
            try:
                # Get uplink MTU to avoid fragmentation
                uplink_mtu = _get_uplink_mtu(ip, uplink)
                uplink_is_port = self._uplink_is_bridge_port(uplink)
                br_idx_list = ip.link_lookup(ifname=self.bridge)
                if not br_idx_list:
                    raise NetworkingError(f"Bridge not found: {self.bridge}")
                br_idx = br_idx_list[0]
                upl_idx = None
                if uplink:
                    ul = ip.link_lookup(ifname=uplink)
                    upl_idx = ul[0] if ul else None
                for nic in self.spec.vm.nics:
                    if not nic.mac:
                        continue
                    tap = tap_name(nic.deviceId, self.spec.vm.name)
                    # Create TAP if necessary
                    tap_idx_list = ip.link_lookup(ifname=tap)
                    if not tap_idx_list:
                        ip.link("add", ifname=tap, kind="tuntap", mode="tap")
                        tap_idx_list = ip.link_lookup(ifname=tap)
                    tap_idx = tap_idx_list[0]
                    # Set MAC, MTU (down first)
                    try:
                        ip.link("set", index=tap_idx, state="down")
                    except NetlinkError:
                        pass
                    ip.link("set", index=tap_idx, address=nic.mac)
                    # Apply uplink MTU to avoid fragmentation
                    if uplink_mtu:
                        try:
                            ip.link("set", index=tap_idx, mtu=uplink_mtu)
                        except NetlinkError as e:
                            logger.warning("Failed to set MTU on TAP %s: %s", tap, e)
                    # Attach to bridge
                    ip.link("set", index=tap_idx, master=br_idx)
                    # VLAN access (if bridge is VLAN-aware)
                    vid = vid_from_buri(nic.broadcastUri, None)
                    if vid is None:
                        raise NetworkingError(f"linux-bridge-vlan requires VLAN for TAP (deviceId={nic.deviceId})")
                    if vid:
                        # Remove VLAN 1 and mark PVID+untagged
                        try:
                            bridge_vlan(ip, tap_idx, "del", 1)
                        except Exception:
                            pass
                        bridge_vlan(ip, tap_idx, "add", int(vid), flags={"PVID", "EgressUntagged"})
                        if upl_idx is not None and uplink_is_port:
                            # Ensure VLAN is present on uplink (tagged)
                            try:
                                bridge_vlan(ip, upl_idx, "add", int(vid))
                            except Exception:
                                pass
                    # Configure bridge port flags
                    try:
                        tap_name_str = ifname(ip, tap_idx)
                        configure_bridge_port_flags(tap_name_str)
                        if upl_idx is not None and uplink_is_port:
                            upl_name = ifname(ip, upl_idx)
                            configure_bridge_port_flags(upl_name)
                    except Exception:
                        pass
                    # Bring the interface up
                    ip.link("set", index=tap_idx, state="up")
                    # FDB prime (remove after ~8s)
                    if vid:
                        setup_fdb_entry(ip, tap_idx, nic.mac, int(vid))
                        setup_fdb_entry_bridge(tap, nic.mac, int(vid))
                    # Debug logging
                    created_taps.append(tap)
            finally:
                ip.close()
            logger.info("Linux bridge VLAN networking prepared successfully for VM %s", self.spec.vm.name)
            return created_taps
        except Exception as e:
            logger.error("Linux bridge VLAN networking preparation failed: %s", e)
            raise NetworkingError(f"Network preparation failed: {e}") from e

    def teardown(self) -> None:
        """Detach and delete TAP devices associated with the VM; best-effort cleanup."""
        try:
            ip = IPRoute()
            try:
                taps: List[str] = []
                # 1) From computed NIC names
                for nic in self.spec.vm.nics:
                    taps.append(tap_name(nic.deviceId, self.spec.vm.name))
                # 2) From config file (if present)
                if self.paths.config_file.exists():
                    try:
                        import json

                        cfg = json.loads(self.paths.config_file.read_text())
                        for ni in cfg.get("network-interfaces") or []:
                            hd = ni.get("host_dev_name")
                            if isinstance(hd, str) and hd:
                                taps.append(hd)
                    except Exception:
                        pass
                taps = sorted(set(taps))
                for tap in taps:
                    idx_list = ip.link_lookup(ifname=tap)
                    if not idx_list:
                        continue
                    idx = idx_list[0]
                    # Down + detach from bridge + delete tuntap
                    try:
                        ip.link("set", index=idx, state="down")
                    except NetlinkError:
                        pass
                    # Remove master (bridge)
                    try:
                        ip.link("set", index=idx, master=0)
                    except NetlinkError:
                        pass
                    # Delete tuntap
                    try:
                        ip.link("del", index=idx)
                    except NetlinkError:
                        pass
            finally:
                ip.close()
            # Uplink VLAN cleanup: remove VIDs from uplink that are no longer
            # used by any TAP
            try:
                uplink = self.uplink or detect_uplink(self.bridge)
                if uplink:
                    cleanup_uplink_vlans(self.bridge, uplink)
            except Exception:
                pass
            logger.info("Linux bridge VLAN networking teardown completed for VM %s", self.spec.vm.name)
        except NetworkingError as e:
            logger.warning("Linux bridge VLAN networking teardown failed: %s", e)
        except Exception as e:
            logger.warning("Unexpected error during Linux bridge VLAN networking teardown: %s", e)
