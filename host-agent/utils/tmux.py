#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tmux management utilities for Firecracker Agent.
This module contains tmux session management functions.
"""
import shlex
from typing import List, Optional

import psutil
from libtmux import Server as TmuxServer

from backend.storage import Paths


class TmuxManager:
    """Manager for tmux session operations."""

    @staticmethod
    def session_exists(server: TmuxServer, name: str) -> bool:
        """Return True if a tmux session exists, using `has-session` to avoid deprecated APIs."""
        try:
            res = server.cmd("has-session", "-t", name)
            # tmux returns exit code 0 if session exists
            return getattr(res, "returncode", None) == 0 or (hasattr(res, "proc") and res.proc.returncode == 0)
        except Exception:
            return False

    @staticmethod
    def kill_session(server: TmuxServer, name: str) -> None:
        """Kill a tmux session by name; ignore errors if it doesn't exist."""
        try:
            server.cmd("kill-session", "-t", name)
        except Exception:
            pass

    @staticmethod
    def new_session(server: TmuxServer, name: str, window_name: str, command: List[str]) -> None:
        """Create a detached tmux session running the provided command, avoiding deprecated libtmux helpers."""
        try:
            # Use `sh -lc` so $PATH and shell expansions behave as expected
            cmd_str = " ".join(shlex.quote(x) for x in command)
            server.cmd(
                "new-session",
                "-d",
                "-s",
                name,
                "-n",
                window_name,
                "sh",
                "-lc",
                cmd_str,
            )
        except Exception as e:
            raise RuntimeError(f"Failed to create tmux session: {e}") from e

    @staticmethod
    def find_fc_pid(paths: Paths, firecracker_bin: Optional[str]) -> Optional[int]:
        """Best-effort discovery of the firecracker PID.
        Strategies:
        1) Match cmdline that contains both the firecracker binary and the API socket path.
        2) Look for a process that has the UNIX socket open (psutil.net_connections or open_files).
        """
        if not firecracker_bin:
            return None
        socket_path = str(paths.socket_file)
        # Strategy 1: Match cmdline
        try:
            for proc in psutil.process_iter(["pid", "cmdline"]):
                try:
                    cmdline = proc.info.get("cmdline") or []
                    if not cmdline:
                        continue
                    if firecracker_bin in cmdline[0] and any(socket_path in arg for arg in cmdline):
                        return proc.info["pid"]
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except Exception:
            pass
        try:
            for proc in psutil.process_iter(["pid", "cmdline"]):
                try:
                    for opened in proc.open_files():
                        if opened.path == socket_path:
                            return proc.info["pid"]
                    cmdline = proc.info.get("cmdline") or []
                    if cmdline and firecracker_bin in cmdline[0] and any(socket_path in arg for arg in cmdline):
                        return proc.info["pid"]
                    for conn in proc.connections(kind="unix"):
                        if conn.laddr and conn.laddr.path == socket_path:
                            return proc.info["pid"]
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except Exception:
            pass
        return None
