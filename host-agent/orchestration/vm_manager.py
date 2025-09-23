#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VM Manager module for Firecracker Agent.
This module handles VM lifecycle operations (start, stop, status, delete, reboot).
"""
import json
import logging
import shlex
import time

import psutil
from libtmux import Server as TmuxServer

from backend.storage import Paths, StorageError, get_backend_by_driver as get_storage_backend_by_driver
from models import Spec
from utils.filesystem import ensure_dirs
from utils.tmux import TmuxManager
from utils.validation import validate_name
from backend.networking import get_backend_by_driver as get_networking_backend_by_driver

logger = logging.getLogger("fc-agent")


class VMManager:
    """Manager for VM lifecycle operations."""

    def __init__(self):
        self.tmux_manager = TmuxManager()

    def start_vm(self, spec: Spec, paths: Paths, timeout: int = 30) -> None:
        """Start Firecracker inside a detached tmux session and wait for API readiness."""
        validate_name("VM", spec.vm.name)
        # ensure directories
        ensure_dirs(paths)
        # remove any stale socket; prepare log file
        try:
            paths.socket_file.unlink()
        except FileNotFoundError:
            pass
        # vsock is not configured; no vsock socket cleanup required
        paths.log_file.touch(exist_ok=True)
        paths.log_file.chmod(0o644)
        # start via tmux (avoid deprecated libtmux list_sessions/new_session
        # helpers)
        server = TmuxServer()
        session_name = f"fc-{spec.vm.name}"
        # If a previous session exists, kill it first (best-effort)
        if self.tmux_manager.session_exists(server, session_name):
            self.tmux_manager.kill_session(server, session_name)
        cmd = [
            spec.host.firecracker_bin,
            "--api-sock",
            str(paths.socket_file),
            "--config-file",
            str(paths.config_file),
        ]
        self.tmux_manager.new_session(server, session_name, "fc", cmd)
        # discover PID using multiple strategies
        pid = self.tmux_manager.find_fc_pid(paths, spec.host.firecracker_bin)
        if not pid:
            # give it a brief moment and retry once more (tmux spawning
            # latency)
            time.sleep(0.5)
            pid = self.tmux_manager.find_fc_pid(paths, spec.host.firecracker_bin)
        if pid:
            paths.pid_file.write_text(str(pid))
            logger.info("firecracker started (pid=%s) for vm=%s", str(pid), spec.vm.name)
        else:
            logger.warning("firecracker PID not found for vm=%s", spec.vm.name)
        # Non-blocking readiness: return immediately if PID exists, else brief grace window
        if pid:
            return
        # brief grace window to allow socket appearance
        end_grace = time.time() + 2.0
        while time.time() < end_grace:
            try:
                if paths.socket_file.exists():
                    return
            except Exception:
                pass
            time.sleep(0.1)
        # Do not block create/start; continue even if API not ready yet

    def stop_vm(self, spec: Spec, paths: Paths, timeout: int = 30) -> None:
        """Request a guest reboot (Ctrl-Alt-Del), then terminate the process gracefully."""
        # try Ctrl-Alt-Del via Firecracker API
        if paths.socket_file.exists():
            body = json.dumps({"action_type": "SendCtrlAltDel"}).encode("utf-8")
            self._make_api_request(paths.socket_file, "PUT", "/actions", body=body)
        pid = None
        try:
            pid = int(paths.pid_file.read_text().strip()) if paths.pid_file.exists() else None
        except Exception:
            pid = None
        end = time.time() + timeout
        if pid and psutil.pid_exists(pid):
            try:
                psutil.Process(pid).terminate()
            except Exception:
                pass
            while time.time() < end and psutil.pid_exists(pid):
                time.sleep(0.2)
            if psutil.pid_exists(pid):
                try:
                    psutil.Process(pid).kill()
                except Exception:
                    pass
        # cleanup artifacts
        for p in [paths.pid_file, paths.socket_file]:
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    def status_vm(self, spec: Spec, paths: Paths) -> str:
        """Report VM power state (poweron, poweroff, unknown) based on socket and PID."""
        pid_running = False
        try:
            if paths.pid_file.exists():
                pid_val = int(paths.pid_file.read_text().strip())
                pid_running = bool(pid_val and psutil.pid_exists(pid_val))
        except Exception:
            pid_running = False
        live_pid = self.tmux_manager.find_fc_pid(paths, spec.host.firecracker_bin)
        server = TmuxServer()
        session_name = f"fc-{spec.vm.name}"
        tmux_exists = self.tmux_manager.session_exists(server, session_name)
        socket_exists = paths.socket_file.exists()

        if pid_running or live_pid or tmux_exists:
            return "poweron"

        if socket_exists:
            code_ver, _ = self._make_api_request(paths.socket_file, "GET", "/version")
            if code_ver == 200:
                return "poweron"
            code_mc, _ = self._make_api_request(paths.socket_file, "GET", "/machine-config")
            if code_mc == 200:
                return "poweron"
            # Unresponsive socket with no PID/tmux -> poweroff (stale socket)
            return "poweroff"

        return "poweroff"

    def delete_vm(self, spec: Spec, paths: Paths) -> None:
        """Delete VM and clean up all associated files."""
        # Stop VM first
        self.stop_vm(spec, paths)
        # Teardown networking before removing config, so we can read host_dev_name if needed
        try:
            net_backend = get_networking_backend_by_driver(spec.net.driver, spec, paths)
            net_backend.teardown()
        except Exception:
            # Best-effort: continue even if network teardown fails
            pass
        try:
            storage_backend = get_storage_backend_by_driver(spec.storage.driver, spec, paths)
            storage_backend.cleanup(spec, paths)
        except StorageError as exc:
            logger.error("Storage cleanup failed for VM %s: %s", spec.vm.name, exc)
            raise
        except Exception as exc:
            logger.error("Unexpected storage cleanup error for VM %s: %s", spec.vm.name, exc)
            raise
        # Clean up configuration files
        for p in [paths.config_file, paths.log_file]:
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    def reboot_vm(self, spec: Spec, paths: Paths, timeout: int = 30) -> None:
        """Reboot VM by stopping and starting it."""
        self.stop_vm(spec, paths, timeout)
        time.sleep(2)  # Brief pause before restart
        self.start_vm(spec, paths, timeout)

    def _wait_for_api_readiness(self, paths: Paths, timeout: int) -> None:
        """Wait for Firecracker API to become ready."""
        end_time = time.time() + timeout
        while time.time() < end_time:
            try:
                code, _ = self._make_api_request(paths.socket_file, "GET", "/version")
                if code == 200:
                    logger.info("Firecracker API is ready")
                    return
            except Exception:
                pass
            time.sleep(0.5)
        logger.warning("Firecracker API did not become ready within %d seconds", timeout)
        return False

    def _make_api_request(self, socket_path, method, path, body=None):
        """Make HTTP request to Firecracker API via UNIX domain socket.
        Returns (status_code: int, body: bytes). Uses short timeouts and stops after headers
        to avoid blocking on persistent connections.
        """
        import socket

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(2.0)
                sock.connect(str(socket_path))
                # Normalize body to bytes
                body_bytes = b""
                if body is not None:
                    if isinstance(body, bytes):
                        body_bytes = body
                    else:
                        try:
                            body_bytes = str(body).encode("utf-8")
                        except Exception:
                            body_bytes = b""
                # Build HTTP/1.1 request
                req = (
                    f"{method} {path} HTTP/1.1\r\n"
                    + "Host: localhost\r\n"
                    + (f"Content-Length: {len(body_bytes)}\r\n" if body_bytes else "")
                    + "Connection: close\r\n"
                    + "\r\n"
                )
                req_bytes = req.encode("ascii") + body_bytes
                sock.sendall(req_bytes)
                # Read until headers complete or timeout
                data = b""
                header_end = b"\r\n\r\n"
                while header_end not in data:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
        except Exception:
            return 0, b""

        header, _, body_part = data.partition(b"\r\n\r\n")
        first = header.split(b"\r\n", 1)[0] if header else b""
        try:
            status = int(first.split()[1])
        except Exception:
            status = 0
        return status, body_part
