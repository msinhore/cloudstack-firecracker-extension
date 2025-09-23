import logging
import shutil
from pathlib import Path

from .base import StorageBackend

logger = logging.getLogger("fc-agent")


class FileBackend(StorageBackend):
    def __init__(self, image: Path, dst: Path):
        self.image = image
        self.dst = dst

    def prepare(self) -> None:
        logger.info("Preparing file storage: %s -> %s", self.image, self.dst)
        
        # Validate source image
        if not self.image.exists():
            raise FileNotFoundError(f"Source image not found: {self.image}")
        if not self.image.is_file():
            raise ValueError(f"Source image is not a file: {self.image}")
        
        self.dst.parent.mkdir(parents=True, exist_ok=True)
        if not self.dst.exists():
            shutil.copyfile(self.image, self.dst)
        self.dst.chmod(0o644)

    def device_path(self) -> str:
        return str(self.dst)

    def delete(self) -> None:
        try:
            self.dst.unlink()
            logger.info("Deleted file volume %s", self.dst)
        except FileNotFoundError:
            pass

    def cleanup(self, spec, paths) -> None:
        """Comprehensive cleanup for file backend."""
        try:
            self.delete()
            logger.info("File backend cleanup completed for VM %s", getattr(spec.vm, "name", "unknown"))
        except Exception as e:
            logger.warning("File backend cleanup failed: %s", e)
            # Continue with cleanup even if deletion fails
