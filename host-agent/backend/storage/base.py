from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class Paths:
    """Computed file paths (volume, config, socket, PID, log) for a VM."""

    volume_file: Path
    config_file: Path
    socket_file: Path
    pid_file: Path
    log_file: Path


class StorageError(Exception):
    """Generic storage backend error."""

    pass


@runtime_checkable
class StorageBackend(Protocol):
    """Minimal contract for storage backends.
    Semantics:
      - prepare(): create/ensure the VM volume (idempotent).
      - device_path(): return the host path that Firecracker will use (e.g., file or /dev/mapper/...).
      - delete(): remove the VM volume (idempotent; must not fail if already absent).
      - cleanup(): comprehensive cleanup including spec and paths (idempotent).
    Notes:
      - Each implementation decides how to receive its parameters in __init__ (e.g., image/dst for file; vg/lv for LVM).
      - Raise StorageError for recoverable failures; other exceptions may propagate.
    """

    def prepare(self) -> None:
        ...

    def device_path(self) -> str:
        ...

    def delete(self) -> None:
        ...

    def cleanup(self, spec, paths) -> None:
        ...
