"""
Open vSwitch (OVS) Networking Backend
=====================================
This module implements the OVS networking backend for Firecracker VMs.
It provides TAP interface creation, OVS bridge attachment, and VLAN configuration.
"""

from __future__ import annotations

import importlib.util
import logging
from typing import List, Optional, Set

from pyroute2 import IPRoute, NetlinkError

from .base import NetworkingBackend, NetworkingError
from .helpers import tap_name, vid_from_buri

logger = logging.getLogger(__name__)


def _check_ovs_libraries() -> bool:
    """Check if OVS libraries are available."""
    try:
        # Check if OVS modules are available
        ovs_db_spec = importlib.util.find_spec("ovs.db")
        ovsdbapp_spec = importlib.util.find_spec("ovsdbapp")
        if ovs_db_spec is None or ovsdbapp_spec is None:
            return False
        # Try to import to verify they work
        import ovs.db.idl  # noqa: F401
        from ovsdbapp.backend.ovs_idl import connection, idlutils  # noqa: F401
        from ovsdbapp.schema.open_vswitch import impl_idl  # noqa: F401

        return True
    except ImportError:
        return False


def _get_uplink_mtu(ip: IPRoute, uplink: Optional[str]) -> Optional[int]:
    """Get MTU from uplink interface to avoid fragmentation."""
    if not uplink:
        return None
    try:
        # Get interface information
        links = ip.get_links()
        for link in links:
            if link.get_attr("IFLA_IFNAME") == uplink:
                mtu = link.get_attr("IFLA_MTU")
                if mtu and mtu > 0:
                    logger.debug("OvsVlanBackend: uplink %s MTU=%d", uplink, mtu)
                    return mtu
        logger.warning("OvsVlanBackend: could not get MTU for uplink %s", uplink)
        return None
    except Exception as e:
        logger.warning("OvsVlanBackend: failed to get MTU for uplink %s: %s", uplink, e)
        return None


class OvsVlanBackend(NetworkingBackend):
    """Open vSwitch with VLAN support networking backend."""

    def __init__(self, spec, paths):
        """Initialize the OVS VLAN backend.
        Args:
            spec: VM specification containing network configuration
            paths: VM file paths
        """
        super().__init__(spec, paths)
        self.bridge = spec.net.host_bridge
        self.uplink = spec.net.uplink

    def prepare(self) -> List[str]:
        """Create/attach TAPs to OVS bridge and configure VLAN access + uplink tagging.
        Returns:
            List of TAP names created/ensured
        Raises:
            NetworkingError: If network preparation fails
        """
        try:
            # Check OVS libraries availability
            if not _check_ovs_libraries():
                raise NetworkingError("OVS libraries not available. Install python3-openvswitch and python3-ovsdbapp.")
            # Require uplink configuration (no autodetect)
            if not self.uplink:
                raise NetworkingError("OVS-VLAN requires net.uplink in configuration (no autodetect)")
            # Get OVS API connection
            api = self._get_ovs_api()
            self._ensure_bridge(api, self.bridge)
            created_taps: List[str] = []
            vids_needed: Set[int] = set()
            ip = IPRoute()
            try:
                # Uplink: use provided value only (no auto-detection)
                uplink = self.uplink
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("OvsVlanBackend.prepare: bridge=%s, uplink=%s", self.bridge, uplink)
                # Get uplink MTU to avoid fragmentation
                uplink_mtu = _get_uplink_mtu(ip, uplink)
                if uplink:
                    self._ensure_port(api, self.bridge, uplink)
                for nic in self.spec.vm.nics:
                    if not nic.mac:
                        continue
                    tap = tap_name(nic.deviceId, self.spec.vm.name)
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            "OvsVlanBackend.prepare: nic deviceId=%s mac=%s tap=%s",
                            nic.deviceId,
                            nic.mac,
                            tap,
                        )
                    # Create TAP if missing
                    tap_idx_list = ip.link_lookup(ifname=tap)
                    if not tap_idx_list:
                        ip.link("add", ifname=tap, kind="tuntap", mode="tap")
                        tap_idx_list = ip.link_lookup(ifname=tap)
                    tap_idx = tap_idx_list[0]
                    # Set MAC, MTU and bring it up (OVS will enslave it)
                    try:
                        ip.link("set", index=tap_idx, state="down")
                    except NetlinkError:
                        pass
                    ip.link("set", index=tap_idx, address=nic.mac)
                    # Apply uplink MTU to avoid fragmentation
                    if uplink_mtu:
                        try:
                            ip.link("set", index=tap_idx, mtu=uplink_mtu)
                            if logger.isEnabledFor(logging.DEBUG):
                                logger.debug("Setting MTU %d on TAP %s", uplink_mtu, tap)
                        except NetlinkError as e:
                            logger.warning("OvsVlanBackend: failed to set MTU %d on TAP %s: %s", uplink_mtu, tap, e)
                    # Add to OVS and set access VLAN if present
                    self._ensure_port(api, self.bridge, tap)
                    vid = vid_from_buri(nic.broadcastUri, None)
                    if vid is None:
                        raise NetworkingError(f"OVS-VLAN requires VLAN for TAP (deviceId={nic.deviceId})")
                    vid = int(vid)
                    vids_needed.add(vid)
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            "OvsVlanBackend.prepare: added VID %s for tap %s (broadcastUri=%s)",
                            vid,
                            tap,
                            nic.broadcastUri,
                        )
                    self._set_port_tag(api, tap, vid)
                    # Bring the TAP up after it's been added to OVS
                    ip.link("set", index=tap_idx, state="up")
                    created_taps.append(tap)
                # Trunk required VLANs on uplink if provided (additive)
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("OvsVlanBackend.prepare: uplink=%s, vids_needed=%s", uplink, sorted(vids_needed))
                if uplink and vids_needed:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            "OvsVlanBackend.prepare: adding VLANs %s to uplink %s trunks", sorted(vids_needed), uplink
                        )
                    self._add_uplink_trunks(api, uplink, sorted(vids_needed))
                elif not uplink:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug("OvsVlanBackend.prepare: no uplink configured, skipping trunk setup")
                elif not vids_needed:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug("OvsVlanBackend.prepare: no VLANs needed, skipping trunk setup")
            finally:
                ip.close()
            logger.info("OVS VLAN networking prepared successfully for VM %s", self.spec.vm.name)
            return created_taps
        except Exception as e:
            logger.error("OVS VLAN networking preparation failed: %s", e)
            raise NetworkingError(f"Network preparation failed: {e}") from e

    def teardown(self) -> None:
        """Detach and delete TAP devices from OVS bridge; best-effort cleanup."""
        try:
            # Check OVS libraries availability
            if not _check_ovs_libraries():
                # Configure bridge port flags
                return
            bridge = self.bridge
            api = self._get_ovs_api()
            taps: List[str] = []
            # 1) Collect TAPs from VM specification (from client payload)
            for nic in self.spec.vm.nics:
                taps.append(tap_name(nic.deviceId, self.spec.vm.name))
            # 2) Also collect TAPs from config file (ground truth - saved
            # during create)
            if self.paths.config_file.exists():
                try:
                    import json

                    cfg = json.loads(self.paths.config_file.read_text())
                    for ni in cfg.get("network-interfaces") or []:
                        hd = ni.get("host_dev_name")
                        if isinstance(hd, str) and hd:
                            taps.append(hd)
                            if logger.isEnabledFor(logging.DEBUG):
                                logger.debug("OvsVlanBackend.teardown: found TAP %s in config file", hd)
                except Exception as e:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug("OvsVlanBackend.teardown: error reading config file: %s", e)
            # Remove duplicates and sort
            taps = sorted(set(taps))
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("OvsVlanBackend.teardown: TAPs to remove: %s", taps)
            # Remove TAPs from OVS bridge and delete tuntap devices
            ip = IPRoute()
            try:
                for tap in taps:
                    # Remove from OVS bridge first
                    try:
                        api.del_port(tap).execute(check_error=False)
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug("OvsVlanBackend.teardown: removed port %s from bridge %s", tap, bridge)
                    except Exception as e:
                        if logger.isEnabledFor(logging.DEBUG):
                            logger.debug("OvsVlanBackend.teardown: error removing port %s: %s", tap, e)
                    # Delete tuntap device
                    idx_list = ip.link_lookup(ifname=tap)
                    if idx_list:
                        idx = idx_list[0]
                        try:
                            ip.link("set", index=idx, state="down")
                        except NetlinkError:
                            pass
                        try:
                            ip.link("del", index=idx)
                            if logger.isEnabledFor(logging.DEBUG):
                                logger.debug("OvsVlanBackend.teardown: deleted tuntap device %s", tap)
                        except NetlinkError:
                            pass
            finally:
                ip.close()
            # Clean up uplink VLANs that are no longer needed
            try:
                uplink = self.uplink
                if uplink:
                    self._remove_unused_uplink_trunks(api, bridge, uplink)
            except Exception as e:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("OvsVlanBackend.teardown: error cleaning up uplink VLANs: %s", e)
            logger.info("OVS VLAN networking teardown completed for VM %s", self.spec.vm.name)
        except NetworkingError as e:
            logger.warning("OVS VLAN networking teardown failed: %s", e)
        except Exception as e:
            logger.warning("Unexpected error during OVS VLAN networking teardown: %s", e)

    def _get_ovs_api(self, server: str = "unix:/var/run/openvswitch/db.sock"):
        """Return an ovsdbapp API handle connected to the local OVSDB."""
        # Import OVS libraries (only when needed)
        from ovs.db import idl as _ovs_idl
        from ovsdbapp.backend.ovs_idl import connection as _ovs_connection
        from ovsdbapp.backend.ovs_idl import idlutils as _ovs_idlutils
        from ovsdbapp.schema.open_vswitch import impl_idl as _ovs_impl

        # Build IDL connection and API
        helper = _ovs_idlutils.get_schema_helper(server, "Open_vSwitch")
        helper.register_all()
        idl = _ovs_idl.Idl(server, helper)
        conn = _ovs_connection.Connection(idl, timeout=5)
        api = _ovs_impl.OvsdbIdl(conn)
        conn.start()
        return api

    def _ensure_bridge(self, api, br: str) -> None:
        """Ensure OVS bridge exists."""
        try:
            exists = api.br_exists(br).execute(check_error=True)
        except Exception:
            exists = False
        if not exists:
            try:
                api.add_br(br).execute(check_error=True)
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("OvsVlanBackend: created bridge %s", br)
            except Exception as e:
                raise NetworkingError(f"Failed to create bridge {br}: {e}")

    def _ensure_port(self, api, br: str, port: str) -> None:
        """Ensure port exists on OVS bridge."""
        try:
            exists = api.port_exists(port).execute(check_error=True)
        except Exception:
            exists = False
        if not exists:
            try:
                api.add_port(br, port).execute(check_error=True)
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("OvsVlanBackend: added port %s to bridge %s", port, br)
            except Exception as e:
                raise NetworkingError(f"Failed to add port {port} to bridge {br}: {e}")

    def _set_port_tag(self, api, port: str, vid: int) -> None:
        """Set VLAN tag on OVS port and force access mode."""
        try:
            # Find port by name to get _uuid
            rows = api.db_list("Port", columns=["_uuid", "name"]).execute(check_error=True) or []
            port_uuid = None
            for r in rows:
                if r.get("name") == port:
                    port_uuid = r.get("_uuid")
                    break
            if not port_uuid:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("_set_port_tag: port %s not found", port)
                return
            # Force access mode and apply VLAN tag
            api.db_set("Port", port_uuid, ("tag", vid)).execute(check_error=True)
            api.db_set("Port", port_uuid, ("vlan_mode", "access")).execute(check_error=False)
            # Add external IDs for better identification
            api.db_set(
                "Port",
                port_uuid,
                (
                    "external_ids",
                    {
                        "fc_vm_name": self.spec.vm.name,
                        "fc_device_id": str(self._get_device_id_for_port(port)),
                    },
                ),
            ).execute(check_error=False)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("_set_port_tag: set VLAN tag %s on port %s (access mode)", vid, port)
        except Exception as e:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("_set_port_tag: error setting VLAN tag %s on port %s: %s", vid, port, e)

    def _get_device_id_for_port(self, port: str) -> int:
        """Get device ID for a TAP port by matching with VM NICs."""
        for nic in self.spec.vm.nics:
            if tap_name(nic.deviceId, self.spec.vm.name) == port:
                return nic.deviceId
        return 0  # fallback

    def _add_uplink_trunks(self, api, uplink: str, vids: List[int]) -> None:
        """Add VLANs to uplink trunk."""
        try:
            # Get current trunks
            rows = api.db_list("Port", columns=["_uuid", "name", "trunks"]).execute(check_error=True) or []
            uplink_uuid = None
            current_trunks = set()
            for r in rows:
                if r.get("name") == uplink:
                    uplink_uuid = r.get("_uuid")
                    current_trunks = set(r.get("trunks", []))
                    break
            if not uplink_uuid:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("_add_uplink_trunks: uplink %s not found", uplink)
                return
            # Configure uplink as trunk-only (no native VLAN)
            api.db_clear("Port", uplink_uuid, "tag").execute(check_error=False)
            api.db_set("Port", uplink_uuid, ("vlan_mode", "trunk")).execute(check_error=False)
            # Add new VLANs to trunks
            new_trunks = current_trunks.union(vids)
            if new_trunks != current_trunks:
                api.db_set("Port", uplink_uuid, ("trunks", sorted(list(new_trunks)))).execute(check_error=True)
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("_add_uplink_trunks: added VLANs %s to uplink %s trunks", vids, uplink)
        except Exception as e:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("_add_uplink_trunks: error adding VLANs %s to uplink %s: %s", vids, uplink, e)

    def _remove_unused_uplink_trunks(self, api, bridge: str, uplink: str) -> None:
        """Remove unused VLANs from uplink trunk."""
        try:
            # Get all VLANs in use on the bridge
            vids_in_use = self._get_bridge_vids_in_use(api, bridge)
            # Get current uplink trunks
            rows = api.db_list("Port", columns=["_uuid", "name", "trunks"]).execute(check_error=True) or []
            uplink_uuid = None
            current_trunks = set()
            for r in rows:
                if r.get("name") == uplink:
                    uplink_uuid = r.get("_uuid")
                    current_trunks = set(r.get("trunks", []))
                    break
            if not uplink_uuid:
                return
            # Configure uplink as trunk-only (no native VLAN)
            api.db_clear("Port", uplink_uuid, "tag").execute(check_error=False)
            api.db_set("Port", uplink_uuid, ("vlan_mode", "trunk")).execute(check_error=False)
            # Remove unused VLANs
            new_trunks = current_trunks.intersection(vids_in_use)
            if new_trunks != current_trunks:
                api.db_set("Port", uplink_uuid, ("trunks", sorted(list(new_trunks)))).execute(check_error=True)
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("_remove_unused_uplink_trunks: removed unused VLANs from uplink %s", uplink)
        except Exception as e:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("_remove_unused_uplink_trunks: error cleaning up uplink %s: %s", uplink, e)

    def _get_bridge_vids_in_use(self, api, bridge: str, exclude_ports: Optional[Set[str]] = None) -> Set[int]:
        """Get all VLAN IDs in use on the bridge."""
        vids_in_use = set()
        try:
            # Get all ports on the bridge
            rows = api.db_list("Port", columns=["_uuid", "name", "tag"]).execute(check_error=True) or []
            for r in rows:
                port_name = r.get("name")
                if exclude_ports and port_name in exclude_ports:
                    continue
                tag = r.get("tag")
                if tag is not None:
                    vids_in_use.add(tag)
        except Exception as e:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("_get_bridge_vids_in_use: error getting VLANs in use: %s", e)
        return vids_in_use
