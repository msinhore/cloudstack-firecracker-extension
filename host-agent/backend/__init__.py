from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .storage.base import StorageBackend


def _fail(message: str) -> None:
    """Helper function to raise ValueError with message."""
    raise ValueError(message)


def _base_lv_name_for_image(image_path: Path) -> str:
    """Generate a base LV name from image path."""
    return f"base-{image_path.stem}"


def make_storage_backend(spec, paths) -> "StorageBackend":
    """
    Factory function to create storage backend instances.
    Args:
        spec: VM specification containing storage configuration
        paths: Path configuration object
    Returns:
        StorageBackend instance configured for the specified driver
    Raises:
        ValueError: If required configuration is missing or driver is unknown
    """
    from .storage.file import FileBackend
    from .storage.lvm import LvmBackend
    from .storage.lvmthin import LvmThinBackend

    drv = (getattr(spec.storage, "driver", None) or "file").lower()
    if drv == "file":
        return FileBackend(Path(spec.vmext.image), paths.volume_file)
    if drv == "lvm":
        vg = getattr(spec.storage, "vg", None) or _fail("storage.vg required for lvm")
        size = getattr(spec.storage, "size", None)
        return LvmBackend(vg, f"vm-{spec.vm.name}", Path(spec.vmext.image), size)
    if drv == "lvmthin":
        vg = getattr(spec.storage, "vg", None) or _fail("storage.vg required for lvmthin")
        pool = getattr(spec.storage, "thinpool", None) or _fail("storage.thinpool required for lvmthin")
        size = getattr(spec.storage, "size", None)
        base_name = _base_lv_name_for_image(Path(spec.vmext.image))
        return LvmThinBackend(vg, pool, base_name, f"vm-{spec.vm.name}", Path(spec.vmext.image), size)
    _fail(f"Unknown storage driver: {drv}")
