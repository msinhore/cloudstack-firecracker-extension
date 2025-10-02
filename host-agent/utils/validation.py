#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Utilities module for Firecracker Agent.
This module contains common utility functions used across the application.
"""
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

import typer


def fail(msg: str, is_api_mode: bool = False) -> None:
    """Emit an error. In API mode raise a runtime error (handled by endpoints);
    in CLI mode print JSON and exit with code 1.
    """
    if is_api_mode:
        raise RuntimeError(msg)
    typer.echo(json.dumps({"error": msg}))
    raise typer.Exit(code=1)


def succeed(data: Dict[str, Any], is_api_mode: bool = False) -> None:
    """Emit success response. In API mode return data; in CLI mode print JSON and exit with code 0."""
    if is_api_mode:
        # In API mode, this should be called from endpoints that return success
        # The actual return happens in the endpoint
        return data
    else:
        typer.echo(json.dumps(data))
        raise typer.Exit(code=0)


def validate_name(entity: str, name: str) -> None:
    """Validate a resource name (alnum and dashes only). Raise ValueError on error."""
    if not re.match(r"^[A-Za-z0-9-]+$", name or ""):
        raise ValueError(f"Invalid {entity} name '{name}'. Only A-Z, a-z, 0-9 and '-' allowed")


def mem_mib(min_ram_bytes: int) -> int:
    """Convert a byte value to MiB (ceil) when larger than 1 MiB."""
    try:
        if min_ram_bytes > 1048576:
            return (min_ram_bytes + 1048575) // 1048576
    except Exception:
        pass
    return int(min_ram_bytes)


def read_json(path: Path) -> Dict[str, Any]:
    """Load and parse a JSON file, raising an agent error on failure."""
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        fail(f"Invalid JSON '{path}': {e}", is_api_mode=False)


def extract_ssh_pubkey_from_payload(obj: Dict[str, Any]) -> Optional[str]:
    """
    Extract 'SSH.PublicKey' from:
      obj['cloudstack.vm.details']['details']['SSH.PublicKey']
    Returns a trimmed string or None.
    """
    try:
        d = (obj or {}).get("cloudstack.vm.details", {}).get("details", {}) or {}
        key = d.get("SSH.PublicKey")
        if isinstance(key, str) and key.strip():
            return key.strip()
    except Exception:
        pass
    return None


def deep_update(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-merge src into dst and return dst. Dicts are merged recursively; lists/scalars are replaced."""
    for k, v in (src or {}).items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_update(dst[k], v)
        else:
            dst[k] = v
    return dst


def is_probably_ssh_key(s: str) -> bool:
    """Check if a string looks like an SSH public key."""
    return bool(s and s.strip() and ("ssh-rsa" in s or "ssh-ed25519" in s or "ecdsa-sha2" in s))


def parse_uid_gid_from_passwd(root_mnt: Path, username: str) -> tuple[int, int]:
    """Parse UID/GID from /etc/passwd in the mounted root filesystem."""
    passwd_path = root_mnt / "etc" / "passwd"
    if not passwd_path.exists():
        return 1000, 1000  # Default fallback
    try:
        with passwd_path.open("r") as f:
            for line in f:
                parts = line.strip().split(":")
                if len(parts) >= 4 and parts[0] == username:
                    return int(parts[2]), int(parts[3])
    except Exception:
        pass
    return 1000, 1000  # Default fallback
