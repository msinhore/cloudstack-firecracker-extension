#!/usr/bin/env python3
"""Standalone service that gracefully stops Firecracker VMs via the agent API."""
from __future__ import annotations

import base64
import json
import logging
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_CONFIG = "/etc/cloudstack/firecracker-agent.json"
DEFAULT_TIMEOUT = 60
POLL_INTERVAL = 2

LOG = logging.getLogger("fc-shutdown-service")


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def load_agent_config(cfg_path: Path) -> Dict[str, Any]:
    if not cfg_path.exists():
        raise FileNotFoundError(f"Agent config not found at {cfg_path}")
    with cfg_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("Agent config is not a JSON object")
    return data


def resolve_base_url(cfg: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    bind_host = str(cfg.get("bind_host", "127.0.0.1")).strip()
    if not bind_host or bind_host in {"0.0.0.0", "::"}:
        bind_host = "127.0.0.1"
    try:
        bind_port = int(cfg.get("bind_port", 8080))
    except Exception as exc:
        raise ValueError(f"Invalid bind_port value: {cfg.get('bind_port')}") from exc
    security_cfg: Dict[str, Any] = {}
    top_security = cfg.get("security")
    if isinstance(top_security, dict):
        security_cfg = top_security.get("tls") if isinstance(top_security.get("tls"), dict) else top_security
    scheme = "https" if security_cfg.get("enabled", False) else "http"
    base_url = f"{scheme}://{bind_host}:{bind_port}"
    return base_url.rstrip("/"), security_cfg


def build_ssl_context(tls_cfg: Dict[str, Any]) -> Optional[ssl.SSLContext]:
    if not tls_cfg or not tls_cfg.get("enabled", False):
        return None
    skip_verify = str(os.environ.get("FC_SHUTDOWN_SKIP_TLS_VERIFY", "")).lower() in {"1", "true", "yes"}
    ca_file = os.environ.get("FC_SHUTDOWN_CA_FILE") or tls_cfg.get("ca_file")
    client_cert = os.environ.get("FC_SHUTDOWN_CLIENT_CERT")
    client_key = os.environ.get("FC_SHUTDOWN_CLIENT_KEY")
    if skip_verify:
        context = ssl._create_unverified_context()  # type: ignore[attr-defined]
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        LOG.warning("TLS verification disabled for shutdown requests")
    else:
        context = ssl.create_default_context()
        if ca_file and Path(ca_file).exists():
            context.load_verify_locations(cafile=ca_file)
        else:
            LOG.warning("No CA file available for TLS verification; set FC_SHUTDOWN_SKIP_TLS_VERIFY=1 to override")
    if client_cert:
        try:
            context.load_cert_chain(certfile=client_cert, keyfile=client_key)
        except Exception as exc:
            raise RuntimeError(f"Failed to load client certificate: {exc}") from exc
    return context


def build_auth_header() -> Optional[str]:
    username = os.environ.get("FC_SHUTDOWN_USERNAME")
    password = os.environ.get("FC_SHUTDOWN_PASSWORD")
    if username and password:
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        return f"Basic {token}"
    if username or password:
        LOG.warning("Shutdown authentication credentials incomplete; ignoring")
    return None


@dataclass
class APIClient:
    base_url: str
    opener: urllib.request.OpenerDirector
    auth_header: Optional[str]

    def request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 10) -> Tuple[int, Optional[Dict[str, Any]]]:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if self.auth_header:
            headers["Authorization"] = self.auth_header
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                body = json.loads(raw.decode("utf-8")) if raw and "application/json" in resp.headers.get("Content-Type", "") else None
                return resp.getcode(), body
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            body = None
            if raw and exc.headers and "application/json" in exc.headers.get("Content-Type", ""):
                try:
                    body = json.loads(raw.decode("utf-8"))
                except Exception:
                    body = None
            return exc.code, body
        except urllib.error.URLError as exc:
            raise ConnectionError(f"Failed to reach agent API at {url}: {exc}") from exc


def create_client(base_url: str, ssl_context: Optional[ssl.SSLContext], auth_header: Optional[str]) -> APIClient:
    handlers: List[urllib.request.BaseHandler] = []
    if base_url.startswith("https://"):
        handlers.append(urllib.request.HTTPSHandler(context=ssl_context))
    opener = urllib.request.build_opener(*handlers) if handlers else urllib.request.build_opener()
    return APIClient(base_url=base_url, opener=opener, auth_header=auth_header)


def list_target_vms(client: APIClient) -> List[str]:
    status_code, body = client.request("GET", "/v1/vms")
    if status_code != 200 or not body or body.get("status") != "success":
        raise RuntimeError(f"Failed to list VMs (status={status_code}, body={body})")
    running: List[str] = []
    for vm in body.get("vms", []):
        name = vm.get("name")
        state = (vm.get("status") or "").lower()
        if not name:
            continue
        if state in {"poweron", "power_on", "power-on", "running"}:
            running.append(name)
    return running


def stop_vm(client: APIClient, vm_name: str, timeout: int) -> bool:
    LOG.info("Stopping VM %s", vm_name)
    status_code, body = client.request("POST", f"/v1/vms/{vm_name}/stop", payload={})
    if status_code not in {200, 202}:
        LOG.warning("Stop request for %s returned status=%s body=%s", vm_name, status_code, body)
    deadline = time.time() + timeout
    while time.time() < deadline:
        status_code, body = client.request("GET", f"/v1/vms/{vm_name}/status")
        if status_code == 200 and body and body.get("status") == "success":
            power_state = (body.get("power_state") or "").lower()
            if power_state not in {"poweron", "power_on", "power-on", "running"}:
                LOG.info("VM %s reported power state %s", vm_name, power_state or "unknown")
                return True
        time.sleep(POLL_INTERVAL)
    LOG.warning("Timed out waiting for VM %s to stop", vm_name)
    return False


def snapshot_running_vms(paths: Iterable[str]) -> None:
    run_dir = Path("/var/run/firecracker")
    lines = []
    for vm_name in paths:
        pid_file = run_dir / f"{vm_name}.pid"
        if pid_file.exists():
            try:
                pid = pid_file.read_text(encoding="utf-8").strip()
            except Exception:
                pid = "?"
        else:
            pid = "missing"
        lines.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} vm={vm_name} pid={pid}")
    if not lines:
        return
    log_dir = Path("/var/log/firecracker")
    log_dir.mkdir(parents=True, exist_ok=True)
    snapshot_file = log_dir / f"fc-shutdown-pids-{time.strftime('%Y-%m-%d')}.log"
    try:
        with snapshot_file.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except Exception as exc:
        LOG.warning("Unable to write PID snapshot: %s", exc)


def shutdown_sequence() -> int:
    cfg_file = Path(os.environ.get("FC_SHUTDOWN_CONFIG", DEFAULT_CONFIG))
    cfg = load_agent_config(cfg_file)
    base_override = os.environ.get("FC_SHUTDOWN_BASE_URL")
    base_url, tls_cfg = resolve_base_url(cfg)
    if base_override:
        base_url = base_override.rstrip("/")
    ssl_context = build_ssl_context(tls_cfg)
    auth_header = build_auth_header()
    timeout_env = os.environ.get("FC_SHUTDOWN_TIMEOUT")
    timeout = DEFAULT_TIMEOUT
    if timeout_env:
        try:
            timeout = max(5, int(timeout_env))
        except Exception:
            LOG.warning("Invalid FC_SHUTDOWN_TIMEOUT value %s, using default %s", timeout_env, DEFAULT_TIMEOUT)
    client = create_client(base_url, ssl_context, auth_header)
    running_vms = list_target_vms(client)
    if not running_vms:
        LOG.info("No running VMs reported by agent")
        return 0
    snapshot_running_vms(running_vms)
    failures = 0
    for vm_name in running_vms:
        try:
            if not stop_vm(client, vm_name, timeout):
                failures += 1
        except Exception as exc:
            LOG.exception("Failed to stop VM %s: %s", vm_name, exc)
            failures += 1
    if failures:
        LOG.warning("Graceful shutdown completed with %s failures", failures)
        return 1
    LOG.info("Graceful shutdown completed successfully")
    return 0


def main() -> int:
    _setup_logging()
    try:
        return shutdown_sequence()
    except Exception as exc:
        LOG.exception("Unhandled shutdown error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
