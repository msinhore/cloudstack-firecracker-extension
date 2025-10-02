import logging
import subprocess
from pathlib import Path
from typing import Optional

from .base import StorageBackend, StorageError
from .lvm_helpers import copy_image_to_device, detect_fstype_from_image, lv_exists, mkfs_device, resolve_lv_dev_path

logger = logging.getLogger("fc-agent")


class LvmBackend(StorageBackend):
    def __init__(self, vg: str, lv: str, image: Path, size_hint: Optional[str]):
        self.vg = vg
        self.lv = lv
        self.image = image
        self.size_hint = size_hint

    def prepare(self) -> None:
        logger.info("Preparing LVM volume %s/%s from %s", self.vg, self.lv, self.image)
        try:
            # 1. Ensure LV exists
            if not lv_exists(self.vg, self.lv):
                cmd = ["lvcreate", "-L", self.size_hint or "1G", "-n", self.lv, self.vg]
                subprocess.run(cmd, check=True)
                fstype = detect_fstype_from_image(self.image)
                dev_path = f"/dev/{self.vg}/{self.lv}"
                mkfs_device(dev_path, fstype)
            # 2. Copy image contents to LV (for raw images)
            # Note: This assumes raw image format. For other formats,
            # you might need to mount the LV and copy files instead
            dev_path = resolve_lv_dev_path(self.vg, self.lv) or f"/dev/{self.vg}/{self.lv}"
            copy_image_to_device(self.image, dev_path)
        except Exception as e:
            raise StorageError(f"Failed to prepare LVM volume {self.vg}/{self.lv}: {e}") from e

    def device_path(self) -> str:
        return resolve_lv_dev_path(self.vg, self.lv) or f"/dev/{self.vg}/{self.lv}"

    def delete(self) -> None:
        logger.info("Deleting LVM volume %s/%s", self.vg, self.lv)
        try:
            subprocess.run(["lvremove", "-f", f"{self.vg}/{self.lv}"], check=True)
        except Exception as e:
            raise StorageError(f"Failed to delete LVM volume {self.vg}/{self.lv}: {e}") from e

    def cleanup(self, spec, paths) -> None:
        """Comprehensive cleanup for LVM backend."""
        try:
            self.delete()
            logger.info("LVM backend cleanup completed for VM %s", getattr(spec.vm, "name", "unknown"))
        except StorageError as e:
            logger.warning("LVM backend cleanup failed: %s", e)
            # Continue with cleanup even if deletion fails
        except Exception as e:
            logger.warning("Unexpected error during LVM cleanup: %s", e)
            # Continue with cleanup even if deletion fails
