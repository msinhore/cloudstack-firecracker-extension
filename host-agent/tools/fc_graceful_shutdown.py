#!/usr/bin/env python3
"""Gracefully shut down running Firecracker VMs via the agent API."""
from __future__ import annotations

import json
import logging
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, List, Optional

API_BASE = "http://127.0.0.1:8080"
LOG_DIR = Path("/var/log/firecracker")
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / f"fc-shutdown-{time.strftime('%Y-%m-%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger("fc-shutdown")

STOP_TIMEOUT = 30  # seconds
POLL_INTERVAL = 2  # seconds


def _api_request(method: str, path: str, payload: Optional[Dict] = None) -> Optional[Dict]:
    url = f"{API_BASE}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.headers.get("Content-Type", "").startswith("application/json"):
                return json.load(resp)
            return None
    except urllib.error.HTTPError as exc:
        logger.warning("API %s %s failed: %s", method, path, exc)
    except urllib.error.URLError as exc:
        logger.warning("API %s %s unreachable: %s", method, path, exc)
    return None


def list_running_vms() -> List[str]:
    response = _api_request("GET", "/v1/vms")
    if not response or response.get("status") != "success":
        logger.warning("Unable to fetch VM list; response=%s", response)
        return []
    running: List[str] = []
    for vm in response.get("vms", []):
        name = vm.get("name")
        status = (vm.get("status") or "").lower()
        if not name:
            continue
        if status in {"running", "poweron", "power_on", "power-on"}:
            running.append(name)
    logger.info("Discovered %d running VMs via API", len(running))
    return running


def stop_vm(vm_name: str) -> None:
    logger.info("Requesting stop for VM %s", vm_name)
    response = _api_request("POST", f"/v1/vms/{vm_name}/stop", payload={})
    if not response or response.get("status") != "success":
        logger.warning("Stop request for %s returned %s", vm_name, response)
    wait_for_vm_shutdown(vm_name)


def wait_for_vm_shutdown(vm_name: str) -> None:
    deadline = time.time() + STOP_TIMEOUT
    while time.time() < deadline:
        status_resp = _api_request("GET", f"/v1/vms/{vm_name}/status")
        if status_resp and status_resp.get("status") == "success":
            power_state = (status_resp.get("power_state") or "").lower()
            if power_state in {"stopped", "stop", "poweroff", "power_off", "power-off", "halt", "unknown"}:
                logger.info("VM %s reported power state %s", vm_name, power_state)
                return
        time.sleep(POLL_INTERVAL)
    logger.warning("VM %s still reported running after %ds", vm_name, STOP_TIMEOUT)


def snapshot_running_pids(vm_names: Iterable[str]) -> None:
    snapshot_file = LOG_DIR / f"fc-shutdown-pids-{time.strftime('%Y-%m-%d')}.log"
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    entries = []
    for vm_name in vm_names:
        pid_file = Path("/var/run/firecracker") / f"{vm_name}.pid"
        if pid_file.exists():
            try:
                pid = pid_file.read_text(encoding="utf-8").strip()
            except Exception:
                pid = "?"
        else:
            pid = "missing"
        entries.append(f"{timestamp} vm={vm_name} pid={pid}")
    if entries:
        try:
            with snapshot_file.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(entries) + "\n")
        except Exception as exc:
            logger.warning("Unable to append PID snapshot: %s", exc)


def main() -> int:
    running = list_running_vms()
    if not running:
        logger.info("No running VMs reported; nothing to do.")
        return 0
    snapshot_running_pids(running)
    for vm_name in running:
        stop_vm(vm_name)
    logger.info("Graceful shutdown sequence complete.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # pragma: no cover
        logger.exception("Unhandled error during Firecracker graceful shutdown: %s", exc)
        sys.exit(1)
