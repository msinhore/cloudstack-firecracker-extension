"""
Storage backend implementations for Firecracker agent.
This module provides a pluggable storage architecture with support for:
- File-based storage (raw files)
- LVM logical volumes
- LVM thin provisioning with snapshots
"""

# Import StorageBackend as a type for type hints
from pathlib import Path
from typing import TYPE_CHECKING

from .base import Paths, StorageError
from .file import FileBackend
from .lvm import LvmBackend
from .lvmthin import LvmThinBackend

if TYPE_CHECKING:
    from .base import StorageBackend


def _fail(message: str) -> None:
    """Helper function to raise ValueError with message."""
    raise ValueError(message)


def _base_lv_name_for_image(image_path: Path) -> str:
    """Generate a base LV name from image path."""
    return f"base-{image_path.stem}"


def get_storage_backend(spec, paths) -> "StorageBackend":
    """
    Helper function to automatically choose and create the appropriate storage backend.
    This is a convenience wrapper around the factory pattern that automatically
    selects the correct backend based on the storage driver configuration.
    Args:
        spec: VM specification containing storage configuration
        paths: Path configuration object
    Returns:
        StorageBackend instance configured for the specified driver
    Raises:
        ValueError: If required configuration is missing or driver is unknown
    """
    # Import here to avoid circular imports
    from .file import FileBackend
    from .lvm import LvmBackend
    from .lvmthin import LvmThinBackend
    
    driver = getattr(spec.storage, 'driver', 'file')
    if driver == "file":
        return FileBackend(Path(spec.vmext.image), paths.volume_file)
    elif driver == "lvm":
        vg = getattr(spec.storage, "vg", None) or _fail("storage.vg required for lvm")
        size = getattr(spec.storage, "size", None)
        return LvmBackend(vg, f"vm-{spec.vm.name}", Path(spec.vmext.image), size)
    elif driver == "lvmthin":
        vg = getattr(spec.storage, "vg", None) or _fail("storage.vg required for lvmthin")
        pool = getattr(spec.storage, "thinpool", None) or _fail("storage.thinpool required for lvmthin")
        size = getattr(spec.storage, "size", None)
        base_name = _base_lv_name_for_image(Path(spec.vmext.image))
        return LvmThinBackend(vg, pool, base_name, f"vm-{spec.vm.name}", Path(spec.vmext.image), size)
    else:
        raise ValueError(f"Unknown storage driver: {driver}")


def get_backend_by_driver(driver: str, spec, paths) -> "StorageBackend":
    """
    Helper function to get a storage backend instance by driver name.
    This creates a backend instance directly using the specified driver,
    useful for cases where you want to call methods like prepare() or cleanup()
    with explicit spec and paths parameters.
    Args:
        driver: Storage driver name ("file", "lvm", "lvmthin")
        spec: VM specification containing storage configuration
        paths: Path configuration object
    Returns:
        StorageBackend instance for the specified driver
    Raises:
        ValueError: If driver is unknown or configuration is missing
    """
    # Import here to avoid circular imports
    from .file import FileBackend
    from .lvm import LvmBackend
    from .lvmthin import LvmThinBackend
    
    if driver == "file":
        return FileBackend(Path(spec.vmext.image), paths.volume_file)
    elif driver == "lvm":
        vg = getattr(spec.storage, "vg", None) or _fail("storage.vg required for lvm")
        size = getattr(spec.storage, "size", None)
        return LvmBackend(vg, f"vm-{spec.vm.name}", Path(spec.vmext.image), size)
    elif driver == "lvmthin":
        vg = getattr(spec.storage, "vg", None) or _fail("storage.vg required for lvmthin")
        pool = getattr(spec.storage, "thinpool", None) or _fail("storage.thinpool required for lvmthin")
        size = getattr(spec.storage, "size", None)
        base_name = _base_lv_name_for_image(Path(spec.vmext.image))
        return LvmThinBackend(vg, pool, base_name, f"vm-{spec.vm.name}", Path(spec.vmext.image), size)
    else:
        raise ValueError(f"Unknown storage driver: {driver}")


__all__ = [
    "StorageError",
    "Paths",
    "FileBackend",
    "LvmBackend",
    "LvmThinBackend",
    "get_storage_backend",
    "get_backend_by_driver",
]
