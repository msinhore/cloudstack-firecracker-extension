"""
Networking Helper Functions
==========================
This module contains helper functions for networking operations,
particularly for Linux bridge and VLAN management.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from pathlib import Path
from typing import List, Optional, Set, Tuple

from pyroute2 import IPRoute, NetlinkError
from pyroute2.netlink.rtnl import ndmsg

logger = logging.getLogger(__name__)


def tap_name(device_id: int, vm_name: str) -> str:
    """Stable TAP name from deviceId and *VM name* (not UUID).
    Format: f<dev>-<sanitized_vmname>
    - sanitize: keep only [A-Za-z0-9], lowercase, max 10 chars
    - total ifname must stay <=15 chars
    """
    v = re.sub(r"[^a-zA-Z0-9]", "", vm_name or "").lower()
    if len(v) > 10:
        v = v[:10]
    return f"f{int(device_id)}-{v}"


def vid_from_buri(broadcast_uri: Optional[str], fallback: Optional[str]) -> Optional[int]:
    """Extract VLAN ID from a `vlan://<id>` broadcastUri or fallback string."""
    if broadcast_uri:
        m = re.match(r"^vlan://(\d+)$", broadcast_uri)
        if m:
            return int(m.group(1))
    if fallback and str(fallback).isdigit():
        return int(fallback)
    return None


def bridge_check(bridge: str) -> Tuple[bool, int]:
    """Return (vlan_filtering_enabled, default_pvid) for the given bridge."""
    try:
        with open(f"/sys/class/net/{bridge}/bridge/vlan_filtering", "r") as f:
            vf = f.read().strip()
    except Exception:
        raise RuntimeError(f"Bridge {bridge} not found or not a bridge")
    try:
        with open(f"/sys/class/net/{bridge}/bridge/default_pvid", "r") as f:
            pvid = int(f.read().strip())
    except Exception:
        pvid = 1
    return vf == "1", pvid


def detect_uplink(bridge: str) -> Optional[str]:
    """Guess the uplink enslaved to a bridge by skipping TAP names (f<id>-...)."""
    brif = Path(f"/sys/class/net/{bridge}/brif")
    if not brif.exists():
        return None
    for name in sorted(p.name for p in brif.iterdir() if p.is_dir() or p.is_symlink()):
        if re.match(r"^f\d+-", name):
            continue
        return name
    return None


def bridge_tap_ports(bridge: str) -> List[str]:
    """Return TAP-like ports (f<id>-*) enslaved to the given bridge."""
    brif = Path(f"/sys/class/net/{bridge}/brif")
    ports: List[str] = []
    try:
        if brif.exists():
            for p in brif.iterdir():
                name = p.name
                if re.match(r"^f\d+-", name):
                    ports.append(name)
    except Exception:
        pass
    return sorted(ports)


def port_vids(dev: str) -> Set[int]:
    """Return VLAN IDs configured on a given port using `bridge -j vlan show dev <dev>`.
    Returns an empty set on errors."""
    try:
        out = subprocess.check_output(["bridge", "-j", "vlan", "show", "dev", dev], text=True)
        arr = json.loads(out)
        vids: Set[int] = set()
        for entry in arr:
            for v in entry.get("vlans", []) or []:
                try:
                    vids.add(int(v.get("vlan", 0)))
                except Exception:
                    pass
        return vids
    except Exception:
        return set()


def ifname(ip: IPRoute, ifindex: int) -> str:
    """Return interface name for a given ifindex using pyroute2."""
    try:
        link = ip.get_links(ifindex)[0]
        return link.get_attr("IFLA_IFNAME")
    except Exception:
        return str(ifindex)


def bridge_vlan(ip: IPRoute, ifindex: int, action: str, vid: int, flags: Optional[Set[str]] = None) -> None:
    """Set or delete a VLAN on a bridge port, with fallback to the `bridge` userspace tool.
    action: 'add' or 'del'
    flags: e.g., {"PVID", "EgressUntagged"}
    """
    # Try native pyroute2 API if available
    try:
        if hasattr(ip, "bridge_vlan_filter"):
            ip.bridge_vlan_filter(
                ifindex,
                vid=vid,
                flags=set(flags) if flags else set(),
                add=(action == "add"),
            )
            return
    except NetlinkError:
        pass
    except Exception:
        # if pyroute2 doesn't support this attribute, fall back to userspace
        # tool
        pass
    # Fallback: use `bridge vlan` command
    try:
        dev = ifname(ip, ifindex)
        if action == "del":
            subprocess.run(["bridge", "vlan", "del", "dev", dev, "vid", str(vid)], check=False)
        elif action == "add":
            cmd = ["bridge", "vlan", "add", "dev", dev, "vid", str(vid)]
            if flags:
                # Map pyroute2 flag names to bridge(8) args
                if "PVID" in flags:
                    cmd.append("pvid")
                if "EgressUntagged" in flags:
                    cmd.append("untagged")
            subprocess.run(cmd, check=False)
    except Exception:
        # best-effort; ignore errors here
        pass


def setup_fdb_entry(ip: IPRoute, tap_idx: int, mac: str, vid: int) -> None:
    """Setup FDB entry for TAP interface with automatic cleanup after 8 seconds."""
    try:
        ip.fdb(
            "replace",
            ifindex=tap_idx,
            lladdr=mac,
            vlan=int(vid),
            state=ndmsg.NUD_PERMANENT,
        )

        def _del_fdb():
            try:
                ip.fdb(
                    "del",
                    ifindex=tap_idx,
                    lladdr=mac,
                    vlan=int(vid),
                    state=ndmsg.NUD_PERMANENT,
                )
            except Exception:
                pass

        threading.Timer(8.0, _del_fdb).start()
    except NetlinkError:
        pass


def setup_fdb_entry_bridge(tap_name: str, mac: str, vid: int) -> None:
    """Setup FDB entry via bridge command with automatic cleanup after 8 seconds."""
    try:
        subprocess.run(
            [
                "bridge",
                "fdb",
                "replace",
                mac,
                "dev",
                tap_name,
                "master",
                "vlan",
                str(int(vid)),
                "static",
            ],
            check=False,
        )

        def _del_fdb_bridge():
            try:
                subprocess.run(
                    [
                        "bridge",
                        "fdb",
                        "del",
                        mac,
                        "dev",
                        tap_name,
                        "master",
                        "vlan",
                        str(int(vid)),
                        "static",
                    ],
                    check=False,
                )
            except Exception:
                pass

        threading.Timer(8.0, _del_fdb_bridge).start()
    except Exception:
        pass


def configure_bridge_port_flags(dev_name: str) -> None:
    """Configure bridge port flags: learning on, flood on, mcast_flood on, neigh_suppress off, bcast_flood on."""
    try:
        subprocess.run(
            [
                "bridge",
                "link",
                "set",
                "dev",
                dev_name,
                "learning",
                "on",
                "flood",
                "on",
                "mcast_flood",
                "on",
                "neigh_suppress",
                "off",
                "bcast_flood",
                "on",
            ],
            check=False,
        )
    except Exception:
        pass


def cleanup_uplink_vlans(bridge: str, uplink: str) -> None:
    """Remove VLANs from uplink that are no longer used by any TAP."""
    try:
        # Assume bridge is properly configured and proceed with cleanup
        # VLANs currently present on uplink (ignore default 1)
        uplink_vids = {v for v in port_vids(uplink) if v != 1}
        # VLANs still in use by any TAP on this bridge
        in_use: Set[int] = set()
        for port in bridge_tap_ports(bridge):
            in_use.update({v for v in port_vids(port) if v != 1})
        # Remove from uplink any VID not in use anymore
        for vid in sorted(uplink_vids - in_use):
            try:
                subprocess.run(
                    ["bridge", "vlan", "del", "dev", uplink, "vid", str(vid)],
                    check=False,
                )
            except Exception:
                pass
    except Exception:
        pass
