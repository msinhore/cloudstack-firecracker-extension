#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VNC console bridge utilities for Firecracker Agent.
This module spawns Xvfb + xterm + x11vnc to expose the tmux-backed serial console.
"""
import json
import logging
import os
import secrets
import signal
import socket
import select
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import psutil
from libtmux import Server as TmuxServer

from .tmux import TmuxManager

logger = logging.getLogger("fc-agent")


class VNCConsoleManager:
    """Manage lifecycle of Xvfb/xterm/x11vnc bridges for VM consoles."""

    def __init__(self, agent_defaults: Dict[str, Any]):
        self.agent_defaults = agent_defaults or {}
        host_defaults = self.agent_defaults.get("host", {}) if isinstance(self.agent_defaults, dict) else {}
        console_defaults = self.agent_defaults.get("console", {}) if isinstance(self.agent_defaults, dict) else {}

        run_dir = host_defaults.get("run_dir") or "/var/run/firecracker"
        self.run_dir = Path(run_dir)
        self.state_dir = self.run_dir / "vnc"
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # Console tuning knobs (minimise surprises by keeping sensible defaults).
        self.bind_host = console_defaults.get("bind_host") or "0.0.0.0"
        self.port_min = int(console_defaults.get("port_min", 5900))
        self.port_max = int(console_defaults.get("port_max", 5999))
        self.geometry = console_defaults.get("geometry") or "1024x768x24"
        self.xterm_geometry = console_defaults.get("xterm_geometry") or "132x44"
        self.font_family = console_defaults.get("font_family") or "Monospace"
        self.font_size = int(console_defaults.get("font_size", 14))
        self.read_only = bool(console_defaults.get("read_only", False))

        self.tmux = TmuxManager()

    # ------------------------------ public API ------------------------------
    def ensure_console(self, vm_name: str) -> Dict[str, Any]:
        """Start (or reuse) a VNC bridge for the given VM."""
        vm_state_path = self._state_path(vm_name)
        current_state = self._load_state(vm_state_path)
        if current_state and self._state_active(current_state):
            return self._response_payload(current_state)

        if current_state:
            self._cleanup_state(current_state)

        session_name = f"fc-{vm_name}"
        server = TmuxServer()
        if not self.tmux.session_exists(server, session_name):
            raise RuntimeError(f"tmux session {session_name} not found; VM console is not available")

        port = self._allocate_port()
        password = self._generate_password()
        password_file = self._write_password_file(vm_name, password)
        display, xvfb_proc = self._start_xvfb()
        xterm_proc = self._start_xterm(display, vm_name, session_name)
        x11vnc_proc = self._start_x11vnc(display, port, password_file)

        state = {
            "vm_name": vm_name,
            "created_at": time.time(),
            "display": display,
            "xvfb_pid": xvfb_proc.pid,
            "xterm_pid": xterm_proc.pid,
            "x11vnc_pid": x11vnc_proc.pid,
            "port": port,
            "password": password,
            "password_file": str(password_file),
            "bind_host": self.bind_host,
            "session_name": session_name,
        }
        self._write_state(vm_state_path, state)
        return self._response_payload(state)

    def stop_console(self, vm_name: str) -> Dict[str, Any]:
        """Terminate VNC bridge for the VM."""
        vm_state_path = self._state_path(vm_name)
        state = self._load_state(vm_state_path)
        if not state:
            return {"status": "success", "message": f"No VNC console running for {vm_name}", "vm_name": vm_name}
        self._cleanup_state(state)
        self._remove_state_file(vm_state_path)
        return {"status": "success", "message": f"VNC console stopped for {vm_name}", "vm_name": vm_name}

    # ------------------------------ helpers ------------------------------
    def _state_path(self, vm_name: str) -> Path:
        return self.state_dir / f"{vm_name}.json"

    @staticmethod
    def _load_state(path: Path) -> Optional[Dict[str, Any]]:
        try:
            if not path.exists():
                return None
            with path.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
            if not isinstance(data, dict):
                return None
            return data
        except Exception:
            return None

    @staticmethod
    def _write_state(path: Path, state: Dict[str, Any]) -> None:
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fp:
            json.dump(state, fp, indent=2)
        tmp.replace(path)

    @staticmethod
    def _remove_state_file(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

    def _state_active(self, state: Dict[str, Any]) -> bool:
        for key in ("xvfb_pid", "xterm_pid", "x11vnc_pid"):
            pid = state.get(key)
            if not pid:
                return False
            try:
                proc = psutil.Process(int(pid))
                if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                    return False
            except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
                return False
        return True

    def _response_payload(self, state: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "status": "success",
            "vm_name": state.get("vm_name"),
            "host": state.get("bind_host", self.bind_host),
            "port": int(state.get("port")),
            "password": state.get("password"),
            "created_at": state.get("created_at"),
        }

    def _cleanup_state(self, state: Dict[str, Any]) -> None:
        for key in ("x11vnc_pid", "xterm_pid", "xvfb_pid"):
            pid = state.get(key)
            if not pid:
                continue
            try:
                proc = psutil.Process(int(pid))
                if proc.is_running():
                    proc.send_signal(signal.SIGTERM)
                    try:
                        proc.wait(timeout=5)
                    except psutil.TimeoutExpired:
                        proc.kill()
            except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
                continue
            except Exception as exc:
                logger.warning("Failed to terminate %s (pid=%s): %s", key, pid, exc)
        password_file = state.get("password_file")
        if password_file:
            try:
                Path(password_file).unlink(missing_ok=True)
            except Exception:
                pass

    def _allocate_port(self) -> int:
        family = socket.AF_INET6 if ":" in self.bind_host else socket.AF_INET
        for port in range(self.port_min, self.port_max + 1):
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    if family == socket.AF_INET6:
                        sock.bind((self.bind_host, port, 0, 0))
                    else:
                        sock.bind((self.bind_host, port))
                    return port
                except OSError:
                    continue
        raise RuntimeError(f"No free VNC ports available in range {self.port_min}-{self.port_max}")

    @staticmethod
    def _generate_password() -> str:
        return secrets.token_urlsafe(8)

    def _write_password_file(self, vm_name: str, password: str) -> Path:
        path = self.state_dir / f"{vm_name}.pass"
        cmd = ["x11vnc", "-storepasswd", password, str(path)]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"x11vnc -storepasswd failed: {exc}") from exc
        os.chmod(path, 0o600)
        return path

    def _start_xvfb(self) -> Tuple[str, subprocess.Popen]:
        cmd = ["Xvfb", "-screen", "0", self.geometry, "-nolisten", "tcp", "-displayfd", "1"]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            close_fds=True,
        )
        display = ""
        deadline = time.time() + 2.0
        if proc.stdout:
            while time.time() < deadline:
                try:
                    rlist, _, _ = select.select([proc.stdout], [], [], 0.2)
                except Exception:
                    break
                if rlist:
                    try:
                        line = proc.stdout.readline()
                    except Exception:
                        break
                    if not line:
                        break
                    display = line.strip()
                    break
                if proc.poll() is not None:
                    break
        if not display:
            if proc.poll() is not None:
                stderr = ""
                if proc.stderr:
                    try:
                        stderr = proc.stderr.read().strip()
                    except Exception:
                        pass
                raise RuntimeError(f"Xvfb failed to start (exit={proc.returncode}): {stderr}")
            raise RuntimeError("Xvfb did not report a display number within timeout")
        if not display.startswith(":"):
            display = f":{display}"
        return display, proc

    def _start_xterm(self, display: str, vm_name: str, session_name: str) -> subprocess.Popen:
        env = os.environ.copy()
        env["DISPLAY"] = display
        cmd = [
            "xterm",
            "-geometry",
            self.xterm_geometry,
            "-T",
            f"Firecracker console: {vm_name}",
            "-fa",
            self.font_family,
            "-fs",
            str(self.font_size),
            "-e",
            "tmux",
            "attach",
            "-t",
            session_name,
        ]
        if self.read_only:
            cmd.extend(["-r"])
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        return proc

    def _start_x11vnc(self, display: str, port: int, password_file: Path) -> subprocess.Popen:
        env = os.environ.copy()
        env["DISPLAY"] = display
        cmd = [
            "x11vnc",
            "-display",
            display,
            "-rfbport",
            str(port),
            "-rfbauth",
            str(password_file),
            "-forever",
            "-shared",
            "-noxdamage",
            "-nolookup",
            "-quiet",
            "-scale",
            "1x1",
        ]
        if self.bind_host not in ("127.0.0.1", "::1"):
            cmd.extend(["-listen", self.bind_host])
        else:
            cmd.append("-localhost")
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        return proc
