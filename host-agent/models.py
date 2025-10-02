#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Data models for Firecracker Agent.
This module contains the data classes used throughout the application.
"""
import dataclasses
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


@dataclasses.dataclass
class NIC:
    """Network interface configuration."""

    deviceId: int
    mac: str
    ip: str
    netmask: str
    gateway: str
    vlan: Optional[int] = None
    broadcastUri: Optional[str] = None


@dataclasses.dataclass
class VMDetails:
    """VM configuration details."""

    name: str
    cpus: int
    minRam: int
    nics: List[NIC]


@dataclasses.dataclass
class HostDetails:
    """Host configuration details."""

    firecracker_bin: str
    conf_dir: str
    run_dir: str
    log_dir: str


@dataclasses.dataclass
class VMExt:
    """VM extension configuration."""

    kernel: str
    boot_args: str
    mem_mib: int
    image: str


@dataclasses.dataclass
class StorageSpec:
    """Storage configuration specification."""

    driver: str
    volume_file: str


@dataclasses.dataclass
class NetSpec:
    """Network configuration specification."""

    driver: str
    bridge: str
    nics: List[NIC]
    host_bridge: str = ""
    uplink: str = ""


@dataclasses.dataclass
class Spec:
    """Top-level specification combining VM, host, storage and network details."""

    vm: VMDetails
    host: HostDetails
    vmext: VMExt
    storage: StorageSpec
    net: NetSpec
    fc_extra: Optional[Dict[str, Any]] = None


class SpecRequest(BaseModel):
    """FastAPI model for endpoints that accept a full spec and optional timeout."""

    spec: Dict[str, Any]
    timeout: Optional[int] = 30
    ssh_public_key: Optional[str] = None

