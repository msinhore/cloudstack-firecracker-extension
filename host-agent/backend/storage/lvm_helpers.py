"""
Helper functions for LVM operations.
"""

import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fc-agent")


def lv_exists(vg: str, lv: str) -> bool:
    """Check if a logical volume exists."""
    try:
        result = subprocess.run(
            ["lvs", "--noheadings", "--options", "lv_name", f"{vg}/{lv}"], capture_output=True, text=True, check=False
        )
        return result.returncode == 0 and lv in result.stdout
    except Exception as e:
        logger.warning("Failed to check if LV %s/%s exists: %s", vg, lv, e)
        return False


def resolve_lv_dev_path(vg: str, lv: str) -> Optional[str]:
    """Resolve the device path for a logical volume."""
    try:
        result = subprocess.run(
            ["lvs", "--noheadings", "--options", "lv_path", f"{vg}/{lv}"], capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except Exception as e:
        logger.warning("Failed to resolve device path for %s/%s: %s", vg, lv, e)
        return None


def detect_fstype_from_image(image_path: Path) -> str:
    """Detect filesystem type from image file."""
    try:
        # Try to detect filesystem type using file command
        result = subprocess.run(["file", "-b", str(image_path)], capture_output=True, text=True, check=True)
        output = result.stdout.lower()
        # Map common filesystem types
        if "ext4" in output:
            return "ext4"
        elif "ext3" in output:
            return "ext3"
        elif "ext2" in output:
            return "ext2"
        elif "xfs" in output:
            return "xfs"
        elif "btrfs" in output:
            return "btrfs"
        else:
            logger.warning("Unknown filesystem type for %s, defaulting to ext4", image_path)
            return "ext4"
    except Exception as e:
        logger.warning("Failed to detect filesystem type for %s: %s, defaulting to ext4", image_path, e)
        return "ext4"


def mkfs_device(device_path: str, fstype: str) -> None:
    """Create filesystem on device."""
    try:
        logger.info("Creating %s filesystem on %s", fstype, device_path)
        # Map filesystem types to mkfs commands
        mkfs_commands = {
            "ext4": ["mkfs.ext4", "-F"],
            "ext3": ["mkfs.ext3", "-F"],
            "ext2": ["mkfs.ext2", "-F"],
            "xfs": ["mkfs.xfs", "-f"],
            "btrfs": ["mkfs.btrfs", "-f"],
        }
        if fstype not in mkfs_commands:
            raise ValueError(f"Unsupported filesystem type: {fstype}")
        cmd = mkfs_commands[fstype] + [device_path]
        subprocess.run(cmd, check=True)
        logger.info("Successfully created %s filesystem on %s", fstype, device_path)
    except Exception as e:
        raise RuntimeError(f"Failed to create {fstype} filesystem on {device_path}: {e}") from e


def copy_image_to_device(image_path: Path, device_path: str) -> None:
    """Copy image contents to device (for raw images)."""
    try:
        logger.info("Copying image %s to device %s", image_path, device_path)
        # Use dd to copy raw image data
        cmd = ["dd", f"if={image_path}", f"of={device_path}", "bs=1M", "status=progress"]
        subprocess.run(cmd, check=True)
        logger.info("Successfully copied image to device")
    except Exception as e:
        raise RuntimeError(f"Failed to copy image {image_path} to device {device_path}: {e}") from e
