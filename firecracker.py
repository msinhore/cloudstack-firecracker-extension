#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""
CloudStack Firecracker extension (HTTP/REST client)

This module talks to a remote Firecracker Agent (FastAPI) over HTTP 
using RESTful endpoints.

CLI compatibility is preserved:

    firecracker.py <operation> <file.json> [timeout]

Operations: create, start, stop, reboot, delete, status, recover

Input JSON is the same CloudStack payload used previously. In addition, the
remote agent connection details are read from `externaldetails.host`.

- externaldetails.host.url      (required, e.g. http://10.0.0.1)
- externaldetails.host.port     (default: 8000)

The client supports optional HTTP Basic authentication (host_username/host_password)
and can also attach a bearer token (host_token) when provided.

Alternatively, the extension may receive pre-extracted (flattened) fields:
- host_url, host_port, skip_ssl_verification
- host_username, host_password
- image_file, kernel_file, boot_args
- vm_name, vm_cpus, vm_ram (bytes), vm_uuid
- vm_vlans (comma-separated), vm_macs (comma-separated), vm_nics (comma-separated)

These flattened keys are used for connectivity (host_url/host_port) and 
name resolution (vm_name) but the full payload is forwarded to the agent as-is.
When HTTPS is used, certificate verification is enabled by default; setting
`skip_ssl_verification=true` bypasses it. If `host_username` and
`host_password` (or their equivalents inside `externaldetails.host`) are
provided, the client sends HTTP Basic credentials to the agent.
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Union
from urllib.parse import urlparse

import requests
from requests.auth import HTTPBasicAuth

# -------------------------- small helpers --------------------------
def _ok(payload: Dict[str, Any]) -> None:
    """Print a JSON object and exit 0."""
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(0)

def _fail(message: str, code: int = 1) -> None:
    """Print an error JSON object and exit non-zero."""
    print(json.dumps({"error": message}, ensure_ascii=False))
    sys.exit(code)

def _read_json(path: str) -> Dict[str, Any]:
    """Load JSON file into a Python dict with helpful errors."""
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        _fail(f"JSON file not found: {path}")
    except json.JSONDecodeError as e:
        _fail(f"Invalid JSON in file: {e}")

def _validate_name(entity: str, name: str) -> None:
    """Validate names against [A-Za-z0-9-]+ (mirrors the shell script)."""
    if not re.match(r"^[A-Za-z0-9-]+$", name or ""):
        _fail(
            (
                f"Invalid {entity} name '{name}'. "
                "Only alphanumeric characters and dashes (-) are allowed."
            )
        )


def _first_non_empty(*values: Any) -> Optional[Any]:
    """Return the first value that is not None/empty string."""
    for value in values:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
        elif value is not None:
            return value
    return None


def _is_truthy(value: Any) -> bool:
    """Interpret common truthy representations."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _ensure_file(path: str, label: str) -> str:
    """Ensure a filesystem path exists and return its absolute form."""
    resolved = Path(path).expanduser()
    if not resolved.exists():
        _fail(f"{label} not found: {resolved}")
    return str(resolved)

# -------------------------- data models --------------------------
class Agent(object):
    """HTTP agent endpoint and auth context (Python 3.6 compatible)."""

    def __init__(
        self,
        base_url: str,
        token: Optional[str],
        timeout: int,
        verify: Union[bool, str] = True,
        auth: Optional[HTTPBasicAuth] = None,
        cert: Optional[Union[str, tuple]] = None,
        console_host: Optional[str] = None,
    ) -> None:
        self.base_url = base_url
        self.token = token
        self.timeout = int(timeout or 30)
        self.verify = verify
        self.auth = auth
        self.cert = cert
        self.console_host = console_host or ""

class Ctx(object):
    """Execution context parsed from JSON + CLI timeout (Python 3.6 compatible)."""

    def __init__(self, raw, vm_name, agent):
        self.raw = raw
        self.vm_name = vm_name
        self.agent = agent

# -------------------------- context parsing --------------------------
def _to_ctx(config_path: str, timeout: int):
    """Translate CloudStack input JSON to a context for HTTP calls."""
    data = _read_json(config_path)

    det = data.get("cloudstack.vm.details", {})
    ext = data.get("externaldetails", {})
    host = ext.get("host", {})

    # Prefer flattened keys when present
    vm_name = (data.get("vm_name") or det.get("name") or det.get("uuid") or "vm").strip()
    _validate_name("VM", vm_name)

    url = (data.get("host_url") or host.get("url") or "").strip()
    if not url:
        _fail("Agent host not provided (host_url or externaldetails.host.url)")

    # host_port may be a string; accept both and default to 8000
    port_val = data.get("host_port") or host.get("port") or host.get("agent_port") or 8000
    try:
        port = int(port_val)
    except (TypeError, ValueError):
        _fail(f"Invalid host_port value: {port_val}")

    token = (data.get("host_token") or host.get("token") or host.get("agent_token") or None)
    if isinstance(token, str):
        token = token.strip() or None

    # If the provided URL already includes an explicit port, don't append another
    if "://" not in url:
        url = f"http://{url}"
    parsed = urlparse(url)
    authority = parsed.netloc or ""
    has_explicit_port = ":" in authority

    base_url = f"{url}/v1" if has_explicit_port else f"{url}:{port}/v1"

    username = _first_non_empty(
        host.get("username"),
        host.get("user"),
        host.get("login"),
        data.get("host_username"),
        data.get("username"),
    )
    password = _first_non_empty(
        host.get("password"),
        host.get("pass"),
        host.get("secret"),
        data.get("host_password"),
        data.get("password"),
    )

    if username and not isinstance(username, str):
        username = str(username)
    if password and not isinstance(password, str):
        password = str(password)
    if username and not password:
        _fail("host_password is required when host_username is provided")
    if password and not username:
        _fail("host_username is required when host_password is provided")
    auth = HTTPBasicAuth(username, password) if username and password else None

    skip_candidates = (
        data.get("skip_ssl_verification"),
        data.get("host_skip_ssl_verification"),
        host.get("skip_ssl_verification"),
        host.get("skip_ssl_verify"),
    )
    skip_verify = False
    for candidate in skip_candidates:
        if candidate is None:
            continue
        skip_verify = _is_truthy(candidate)
        break

    ca_bundle = _first_non_empty(
        data.get("ca_bundle"),
        data.get("ca_cert"),
        host.get("ca_bundle"),
        host.get("ca_cert"),
        host.get("ca_file"),
    )
    verify: Union[bool, str] = True
    if skip_verify:
        verify = False
        try:
            import urllib3
            from urllib3.exceptions import InsecureRequestWarning

            urllib3.disable_warnings(InsecureRequestWarning)
        except Exception:
            pass
    elif isinstance(ca_bundle, str):
        verify = _ensure_file(ca_bundle, "CA bundle")

    client_cert = _first_non_empty(
        host.get("client_cert"),
        host.get("tls_client_cert"),
        data.get("client_cert"),
        data.get("tls_client_cert"),
    )
    client_key = _first_non_empty(
        host.get("client_key"),
        host.get("tls_client_key"),
        data.get("client_key"),
        data.get("tls_client_key"),
    )
    cert: Optional[Union[str, tuple]] = None
    if isinstance(client_cert, str) and client_cert:
        cert_path = _ensure_file(client_cert, "TLS client certificate")
        if isinstance(client_key, str) and client_key:
            key_path = _ensure_file(client_key, "TLS client key")
            cert = (cert_path, key_path)
        else:
            cert = cert_path
    elif isinstance(client_key, str) and client_key:
        _fail("TLS client key provided but client certificate missing")

    console_host = (host.get("console_host") or parsed.hostname or "").strip()
    agent = Agent(base_url, token, int(timeout or 30), verify=verify, auth=auth, cert=cert, console_host=console_host)
    return Ctx(data, vm_name, agent)

# -------------------------- HTTP helpers --------------------------
def _headers(agent):
    """Default headers with optional bearer token."""
    headers = {"Accept": "application/json"}
    if getattr(agent, "token", None):
        headers["Authorization"] = f"Bearer {agent.token}"
    return headers

def _req(method, url, agent, json_body=None):
    """Perform an HTTP request with proper timeouts and map errors."""
    try:
        return requests.request(
            method,
            url,
            json=json_body,
            headers=_headers(agent),
            timeout=agent.timeout,
            auth=getattr(agent, "auth", None),
            verify=getattr(agent, "verify", True),
            cert=getattr(agent, "cert", None),
        )
    except requests.exceptions.RequestException as e:
        _fail(f"HTTP error contacting agent: {e}")

def _json_or_fail(resp):
    """Return parsed JSON or fail with a helpful message."""
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text[:500]}
    if resp.status_code >= 400:
        msg = data.get("error") if isinstance(data, dict) else str(data)
        _fail(f"Agent error ({resp.status_code}): {msg}")
    if not isinstance(data, dict):
        _fail("Agent returned non-JSON or unexpected payload")
    return data

# -------------------------- operations (HTTP) --------------------------
def op_create(ctx):
    """POST /v1/vms — create and start the microVM on the agent."""
    url = f"{ctx.agent.base_url}/vms"
    payload = {"spec": ctx.raw, "timeout": ctx.agent.timeout}
    data = _json_or_fail(_req("POST", url, ctx.agent, json_body=payload))
    _ok(data)

def op_start(ctx):
    """POST /v1/vms/start — start an existing VM using saved config."""
    url = f"{ctx.agent.base_url}/vms/{ctx.vm_name}/start"
    payload = {"spec": ctx.raw, "timeout": ctx.agent.timeout}
    data = _json_or_fail(_req("POST", url, ctx.agent, json_body=payload))
    _ok(data)

def op_stop(ctx):
    """POST /v1/vms/{name}/stop — graceful then force stop."""
    url = f"{ctx.agent.base_url}/vms/{ctx.vm_name}/stop"
    payload = {"timeout": ctx.agent.timeout}
    data = _json_or_fail(_req("POST", url, ctx.agent, json_body=payload))
    _ok(data)

def op_reboot(ctx):
    """POST /v1/vms/{name}/reboot — stop and start."""
    url = f"{ctx.agent.base_url}/vms/{ctx.vm_name}/reboot"
    payload = {"timeout": ctx.agent.timeout}
    data = _json_or_fail(_req("POST", url, ctx.agent, json_body=payload))
    _ok(data)

def op_delete(ctx):
    """DELETE /v1/vms/{name} — delete resources (net/config/socket/pid/volume)."""
    url = f"{ctx.agent.base_url}/vms/{ctx.vm_name}"
    data = _json_or_fail(_req("DELETE", url, ctx.agent))
    _ok(data)

def op_status(ctx):
    """GET /v1/vms/{name}/status — return power_state."""
    url = f"{ctx.agent.base_url}/vms/{ctx.vm_name}/status"
    data = _json_or_fail(_req("GET", url, ctx.agent))
    _ok(data)


def op_recover(ctx):
    """POST /v1/vms/{name}/recover — restore networking/process state."""
    url = f"{ctx.agent.base_url}/vms/{ctx.vm_name}/recover"
    payload = {"spec": ctx.raw, "timeout": ctx.agent.timeout}
    data = _json_or_fail(_req("POST", url, ctx.agent, json_body=payload))
    _ok(data)

def op_console(ctx):
    """POST /v1/vms/{name}/console — obtain VNC bridge connection info."""
    url = f"{ctx.agent.base_url}/vms/{ctx.vm_name}/console"
    data = _json_or_fail(_req("POST", url, ctx.agent))
    console_obj = data.get("console")
    if isinstance(console_obj, dict):
        host = console_obj.get("host")
        port = console_obj.get("port")
        password = console_obj.get("password")
        if host and port and password:
            try:
                console_obj["port"] = int(port)
            except (TypeError, ValueError):
                _fail(f"Invalid port value returned by agent: {port}")
            _ok(data)
        # fallthrough if malformed
    console_host = getattr(ctx.agent, "console_host", "") or ""
    resp_host = data.get("host")
    if console_host and (not resp_host or resp_host in {"0.0.0.0", "127.0.0.1", "::"}):
        data["host"] = console_host
    host = data.get("host")
    port = data.get("port")
    password = data.get("password")
    if not host or not port or not password:
        _fail("Agent response missing host/port/password for console")
    try:
        port_int = int(port)
    except (TypeError, ValueError):
        _fail(f"Invalid port value returned by agent: {port}")
    console_obj = {
        "host": host,
        "port": port_int,
        "password": password,
        "protocol": "vnc",
        "passwordonetimeuseonly": False,
    }
    response = {
        "status": data.get("status", "success"),
        "message": data.get("message", "Console ready"),
        "console": console_obj,
    }
    _ok(response)

# -------------------------- main --------------------------
def main():
    """Parse CLI, build context, dispatch operation over HTTP."""
    if len(sys.argv) < 3:
        _fail("Usage: firecracker.py <operation> <file.json> [timeout]")

    operation = sys.argv[1].lower()
    json_file = sys.argv[2]
    timeout = int(sys.argv[3]) if len(sys.argv) > 3 else int(os.getenv("FC_AGENT_TIMEOUT", 30))
    ctx = _to_ctx(json_file, timeout)

    ops = {
        "create": op_create,
        "start": op_start,
        "stop": op_stop,
        "reboot": op_reboot,
        "delete": op_delete,
        "status": op_status,
        "state": op_status,
        "recover": op_recover,
        "console": op_console,
        "getconsole": op_console,
    }

    if operation not in ops:
        _fail("Invalid action")

    try:
        ops[operation](ctx)
    except SystemExit:
        raise
    except Exception as e:
        _fail(str(e))

if __name__ == "__main__":
    main()
