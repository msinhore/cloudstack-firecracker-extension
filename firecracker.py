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

The client does not send Authorization headers; the agent must be configured to
allow unauthenticated access or enforce auth server-side without requiring a token
from this client.

Alternatively, the extension may receive pre-extracted (flattened) fields:
- host_url, host_port
- image_file, kernel_file, boot_args
- vm_name, vm_cpus, vm_ram (bytes), vm_uuid
- vm_vlans (comma-separated), vm_macs (comma-separated), vm_nics (comma-separated)

These flattened keys are used for connectivity (host_url/host_port) and 
name resolution (vm_name) but the full payload is forwarded to the agent as-is.
"""

import json
import os
import re
import sys
from pathlib import Path
import requests
from typing import Any, Dict

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

# -------------------------- data models --------------------------
class Agent(object):
    """HTTP agent endpoint and auth context (Python 3.6 compatible)."""

    def __init__(self, base_url, token, timeout):
        self.base_url = base_url
        self.token = token
        self.timeout = int(timeout or 30)

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

    # If the provided URL already includes an explicit port, don't append another
    if "://" in url:
        # Extract the authority part after scheme:// and check for ':' in host:port
        authority = url.split("://", 1)[1]
        has_explicit_port = ":" in authority.split("/")[0]
    else:
        has_explicit_port = False
        # If no scheme, assume http for completeness
        url = f"http://{url}"

    base_url = f"{url}/v1" if has_explicit_port else f"{url}:{port}/v1"

    agent = Agent(base_url, token, int(timeout or 30))
    return Ctx(data, vm_name, agent)

# -------------------------- HTTP helpers --------------------------
def _headers(agent):
    """Default headers. Token is intentionally ignored (no Authorization)."""
    return {"Accept": "application/json"}

def _req(method, url, agent, json_body=None):
    """Perform an HTTP request with proper timeouts and map errors."""
    try:
        return requests.request(
            method,
            url,
            json=json_body,
            headers=_headers(agent),
            timeout=agent.timeout,
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
        "recover": op_recover,
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
