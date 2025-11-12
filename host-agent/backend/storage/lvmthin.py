import logging
import subprocess
from pathlib import Path
from typing import Optional

from .base import StorageBackend, StorageError
from .lvm_helpers import copy_image_to_device, detect_fstype_from_image, lv_exists, mkfs_device, resolve_lv_dev_path

logger = logging.getLogger("fc-agent")


class LvmThinBackend(StorageBackend):
    def __init__(self, vg: str, pool: str, base_name: str, vm_lv: str, image: Path, size_hint: Optional[str]):
        self.vg = vg
        self.pool = pool
        self.base_name = base_name
        self.vm_lv = vm_lv
        self.image = image
        self.size_hint = size_hint

    def prepare(self) -> None:
        try:
            logger.info("Preparing thin snapshot %s/%s from base %s", self.vg, self.vm_lv, self.base_name)
            # 1. Ensure base LV exists
            if not lv_exists(self.vg, self.base_name):
                subprocess.run(
                    ["lvcreate", "-V", self.size_hint or "1G", "-T", f"{self.vg}/{self.pool}", "-n", self.base_name],
                    check=True,
                )
                dev_path = f"/dev/{self.vg}/{self.base_name}"
                fstype = detect_fstype_from_image(self.image)
                mkfs_device(dev_path, fstype)
                # Copy image to base LV
                copy_image_to_device(self.image, dev_path)
            # 2. Create snapshot for VM
            if not lv_exists(self.vg, self.vm_lv):
                subprocess.run(["lvcreate", "-s", "-n", self.vm_lv, f"{self.vg}/{self.base_name}"], check=True)
            else:
                subprocess.run(["lvchange", "-ay", f"{self.vg}/{self.vm_lv}"], check=True)
        except Exception as e:
            raise StorageError(f"Failed to prepare thin volume {self.vg}/{self.vm_lv}: {e}") from e

    def device_path(self) -> str:
        return resolve_lv_dev_path(self.vg, self.vm_lv) or f"/dev/{self.vg}/{self.vm_lv}"

    def delete(self) -> None:
        try:
            subprocess.run(["lvremove", "-f", f"{self.vg}/{self.vm_lv}"], check=True)
            logger.info("Deleted thin LV %s/%s", self.vg, self.vm_lv)
        except Exception as e:
            raise StorageError(f"Failed to delete thin LV {self.vg}/{self.vm_lv}: {e}") from e

    def cleanup(self, spec, paths) -> None:
        """Comprehensive cleanup for LVM thin backend."""
        try:
            self.delete()
            logger.info("LVM thin backend cleanup completed for VM %s", getattr(spec.vm, "name", "unknown"))
        except StorageError as e:
            logger.warning("LVM thin backend cleanup failed: %s", e)
            # Continue with cleanup even if deletion fails
        except Exception as e:
            logger.warning("Unexpected error during LVM thin cleanup: %s", e)
            # Continue with cleanup even if deletion fails
