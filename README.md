# Firecracker CloudStack Extension & Host Agent

Firecracker CloudStack bridges Apache CloudStack with [Firecracker microVMs](https://firecracker-microvm.github.io/). It offers an external hypervisor extension for the CloudStack management server and a host-side agent that turns each Firecracker node into an HTTPS-controlled API endpoint with PAM authentication and configurable storage/network backends.

---
## Components
- **`firecracker.py`** – Python client invoked by CloudStack. It validates the deployment payload and forwards it to a remote agent, handling HTTPS, Basic Auth and optional mTLS.
- **Host Agent** – FastAPI service packaged as `firecracker-cloudstack-agent`. It prepares storage, networking, renders Firecracker configs and drives the microVM lifecycle.
- **Debian Package** – Installs the agent, generates TLS material under `/etc/cloudstack/tls-cert`, ships systemd integration and all Python dependencies (including PAM support).

---
## Features
- REST API and Typer CLI for VM lifecycle (`create`, `start`, `stop`, `reboot`, `delete`, `recover`).
- Pluggable storage (`file`, `lvm`, `lvmthin`) and networking backends (`linux-bridge-vlan`, `ovs-vlan`).
- HTTPS listeners with auto-generated self-signed CA/server certificates and optional client-auth (`none`, `optional`, `required`).
- PAM-backed HTTP Basic authentication; disable it when not needed.
- Persistent VM metadata for crash recovery and rebuild.
- Companion shutdown service that calls the agent API to stop running microVMs during host shutdown.

---
## Host Installation (Ubuntu 24.04)
1. **Install Firecracker** – place the binary at `/usr/local/bin/firecracker`:
   ```bash
   FC_VERSION=1.13.1
   ARCH=$(uname -m)
   curl -LO https://github.com/firecracker-microvm/firecracker/releases/download/v${FC_VERSION}/firecracker-v${FC_VERSION}-${ARCH}.tgz
   tar -xzf firecracker-v${FC_VERSION}-${ARCH}.tgz
   sudo install -m 0755 firecracker-v${FC_VERSION}-${ARCH}/firecracker /usr/local/bin/firecracker
   ```

2. **Install the agent package**:
   ```bash
   sudo apt install -y ./firecracker-cloudstack-agent_<version>_all.deb
   ```

3. **Enable the service**:
   ```bash
    systemctl enable --now firecracker-cloudstack-agent.service
   ```
   Check status:
   ```bash
    systemctl status firecracker-cloudstack-agent.service
   curl -ks https://127.0.0.1:8443/healthz
   ```

---
## Agent Configuration
Main file: `/etc/cloudstack/firecracker-agent.json` (shipped as a conffile). Minimal example:
```json
{
  "bind_host": "0.0.0.0",
  "bind_port": 8443,
  "defaults": {
    "host": {
      "firecracker_bin": "/usr/local/bin/firecracker",
      "kernel_dir": "/var/lib/firecracker/kernel",
      "image_dir": "/var/lib/firecracker/images",
      "conf_dir": "/var/lib/firecracker/conf",
      "run_dir": "/var/run/firecracker",
      "log_dir": "/var/log/firecracker",
      "payload_dir": "/var/lib/firecracker/payload"
    },
    "storage": {
      "driver": "file",
      "volume_dir": "/var/lib/firecracker/volumes"
    },
    "net": {
      "driver": "linux-bridge-vlan",
      "host_bridge": "cloudbr1"
    },
    "console": {
      "bind_host": "0.0.0.0",
      "port_min": 5900,
      "port_max": 5999,
      "geometry": "1024x768x24",
      "xterm_geometry": "132x44",
      "font_family": "Monospace",
      "font_size": 14
    }
  },
  "security": {
    "tls": {
      "enabled": true,
      "cert_file": "/etc/cloudstack/tls-cert/server.crt",
      "key_file": "/etc/cloudstack/tls-cert/server.key",
      "ca_file": "/etc/cloudstack/tls-cert/ca.crt",
      "client_auth": "none"
    }
  },
  "auth": {
    "enabled": true,
    "service": "firecracker-agent"
  },
  "ui": {
    "enabled": true,
    "session_timeout_seconds": 1800
  }
}
```

### Key Sections
- **`defaults.host`** – directories and Firecracker binary path. All paths must exist; the package seeds them in `/var/lib/firecracker`.
- **`security.tls`**
  - `enabled`: turn HTTPS on/off.
  - `cert_file`/`key_file`: server certificate/key. Replace the auto-generated pair as needed.
  - `ca_file`: CA used to sign the server cert and validate client certs when mTLS is enabled.
  - `client_auth`: `none`, `optional`, or `required` (mTLS).
- **`auth`**
  - `enabled`: when true, all `/v1/*` routes require HTTP Basic credentials.
  - `service`: name of the PAM stack (populate `/etc/pam.d/firecracker-agent`). The package depends on `python3-pamela`.
- **`ui`**
  - `enabled`: set to `false` to disable the embedded Vue dashboard entirely. When `true`, the agent mounts static assets under `/ui` and automatically redirects `/` to that route.
  - `session_timeout_seconds`: idle timeout advertised to the UI. After the specified number of seconds the browser clears stored credentials and the user must sign in again. Use `0` to disable automatic expiry (not recommended for shared workstations).
  - The effective values are exposed via `GET /v1/ui/config`, allowing external tooling to inspect how the host is configured.

### Configuration Reference

| Section | Key | Type | Default | Description |
| - | - | - | - | - |
| root | `bind_host` | string | `0.0.0.0` | Interface where the FastAPI listener binds. Use `127.0.0.1` when fronting the agent with another proxy. |
| root | `bind_port` | integer | `8080` | TCP port for the HTTPS/HTTP API. |
| `defaults.host` | `firecracker_bin` | path | — | Absolute path to the Firecracker binary. Required. |
| `defaults.host` | `kernel_dir` | path | `/var/lib/firecracker/kernel` | Directory containing guest kernels (`vmlinux`). |
| `defaults.host` | `image_dir` | path | `/var/lib/firecracker/images` | Directory with root disk images referenced by templates. |
| `defaults.host` | `conf_dir` | path | `/var/lib/firecracker/conf` | Location where rendered Firecracker JSON configs are persisted. |
| `defaults.host` | `run_dir` | path | `/var/run/firecracker` | Runtime sockets, PID files, and recovered network configs. |
| `defaults.host` | `log_dir` | path | `/var/log/firecracker` | Folder for Firecracker stdout/stderr logs. |
| `defaults.host` | `payload_dir` | path | `/var/lib/firecracker/payload` | Storage for raw CloudStack payloads (`create-spec-*.json`). |
| `defaults.storage` | `driver` | enum | `file` | Storage backend: `file`, `lvm`, or `lvmthin`. |
| `defaults.storage` | `volume_dir` | path | `/var/lib/firecracker/volumes` | Base directory for volume files (required for `file` backend). |
| `defaults.storage` | `vg` / `volume_group` | string | — | Name of the LVM volume group used by `lvm`/`lvmthin` drivers (required for LVM backends). |
| `defaults.storage` | `thinpool` | string | — | Thin pool logical volume inside `vg`; required when `driver` is `lvmthin`. |
| `defaults.storage` | `size` | string | — | Optional size hint (e.g., `50G`) applied when templates omit disk size metadata. |
| `defaults.net` | `driver` | enum | `linux-bridge-vlan` | Network backend: `linux-bridge-vlan` or `ovs-vlan`. |
| `defaults.net` | `bridge` | string | — | Optional explicit tap bridge name; falls back to `host_bridge` when unset. |
| `defaults.net` | `host_bridge` | string | `cloudbr1` | Bridge (Linux or OVS) used to attach VM tap interfaces. |
| `defaults.net` | `uplink` | string | — | Optional parent interface/uplink used by the backend. |
| `defaults.console` | `bind_host` | string | `0.0.0.0` | Address where the VNC console bridge binds; use `127.0.0.1` behind SSH tunnels. |
| `defaults.console` | `port_min` | integer | `5900` | Lower bound of the TCP port range reserved for console sessions. |
| `defaults.console` | `port_max` | integer | `5999` | Upper bound of the TCP port range reserved for console sessions. |
| `defaults.console` | `geometry` | string | `1024x768x24` | Virtual framebuffer size and color depth for the Xvfb display used per VM. |
| `defaults.console` | `xterm_geometry` | string | `132x44` | xterm window geometry for console shell helper. |
| `defaults.console` | `font_family` | string | `Monospace` | Font family enforced inside the xterm console helper. |
| `defaults.console` | `font_size` | integer | `14` | Font point size used by the xterm helper. |
| `security.tls` | `enabled` | boolean | `true` | Enables HTTPS for the API/UI. |
| `security.tls` | `cert_file` | path | `/etc/cloudstack/tls-cert/server.crt` | Server certificate presented to clients. |
| `security.tls` | `key_file` | path | `/etc/cloudstack/tls-cert/server.key` | Private key paired with `cert_file`. |
| `security.tls` | `ca_file` | path | `/etc/cloudstack/tls-cert/ca.crt` | CA bundle for validating client certificates (mTLS). |
| `security.tls` | `client_auth` | enum | `none` | TLS client-auth policy: `none`, `optional`, or `required`. |
| `auth` | `enabled` | boolean | `true` | Toggles HTTP Basic authentication. |
| `auth` | `service` | string | `firecracker-agent` | PAM service name used to validate credentials. |
| `ui` | `enabled` | boolean | `true` | Controls whether the Vue dashboard is served under `/ui` and `/` redirects. |
| `ui` | `session_timeout_seconds` | integer | `1800` | Idle timeout advertised to the UI; `0` disables automatic logout. |
| `logging` | `level` | enum | `INFO` | Optional override for the agent logger level (`DEBUG`, `INFO`, etc.). |

### mTLS Configuration Guide
1. **Generate a CA, server, and client certificate**
   ```bash
   sudo install -d -m 0700 /etc/cloudstack/tls-cert
   cd /etc/cloudstack/tls-cert

   # Certificate Authority
   sudo openssl req -x509 -nodes -newkey rsa:4096 -keyout ca.key -out ca.crt \
     -days 3650 -subj "/CN=Firecracker CA"

   # Server certificate signed by the CA
   sudo openssl req -nodes -newkey rsa:4096 -keyout server.key -out server.csr \
     -subj "/CN=$(hostname -f)"
   sudo openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
     -out server.crt -days 825 -sha256 -extensions v3_req \
     -extfile <(printf "[v3_req]\nsubjectAltName=DNS:$(hostname -f),IP:$(hostname -I | awk '{print $1)}'\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth")

   # Client certificate (copy the resulting files to the CloudStack management node)
   sudo openssl req -nodes -newkey rsa:4096 -keyout client.key -out client.csr \
     -subj "/CN=cloudstack"
   sudo openssl x509 -req -in client.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
     -out client.crt -days 825 -sha256 -extensions v3_req \
     -extfile <(printf "[v3_req]\nextendedKeyUsage=clientAuth\nkeyUsage=digitalSignature\nsubjectAltName=DNS:cloudstack\n")
   sudo chown root:root *.crt *.key
   sudo chmod 0640 server.key client.key
   ```
   Keep `ca.key` and the `.srl` files on the agent host only; distribute `ca.crt`, `client.crt`, and `client.key` securely to the CloudStack management server.

2. **Configure the agent to require mTLS**
   - In `/etc/cloudstack/firecracker-agent.json`, set:
     ```json
     "security": {
       "tls": {
         "enabled": true,
         "cert_file": "/etc/cloudstack/tls-cert/server.crt",
         "key_file": "/etc/cloudstack/tls-cert/server.key",
         "ca_file": "/etc/cloudstack/tls-cert/ca.crt",
         "client_auth": "required"
       }
     }
     ```
   - Restart the service so the new certificates and policy are picked up:
     ```bash
     sudo systemctl restart firecracker-cloudstack-agent.service
     journalctl -u firecracker-cloudstack-agent.service -g "TLS enabled"
     ```

3. **Configure the Firecracker client (`firecracker.py`)**
   - Supply the CA and client credentials in the CloudStack host payload (or flattened keys):
     ```json
     {
       "host_url": "https://firecracker-host.example.com",
       "host_port": 8443,
       "client_cert": "/etc/cloudstack/firecracker/client.crt",
       "client_key": "/etc/cloudstack/firecracker/client.key",
       "ca_bundle": "/etc/cloudstack/firecracker/ca.crt"
     }
     ```
   - When using the CLI manually, pass the same values inside the JSON spec file so `firecracker.py` can present the client certificate and trust the agent CA.

### Storage Backends (`defaults.storage`)
- `file` – simple sparse files under `volume_dir`. See `host-agent/firecracker-agent.json-file-example`.
- `lvm` – logical volumes created in `vg`. Optional `size` sets the LV size when images lack metadata. See `host-agent/firecracker-agent.json-lvm-example`.
- `lvmthin` – thin-provisioned volumes inside `vg`/`thinpool`. Optional `size` overrides the provisioned size. See `host-agent/firecracker-agent.json-lvmthin-example`.

All storage drivers accept per-request overrides; values here act as defaults.

### Network Backends (`defaults.net`)
- `linux-bridge-vlan` – attaches tap devices to a Linux bridge and tags VLANs per request. Provide `host_bridge`; optional `uplink` pins the external interface instead of autodetection. See `host-agent/firecracker-agent.json-file-example`.
- `ovs-vlan` – programs Open vSwitch for VLAN tagging. Provide `host_bridge` (integration bridge) and `uplink`; OVS Python bindings must be installed on the host. See `host-agent/firecracker-agent.json-ovs-example`.

After edits, restart the service:
```bash
sudo systemctl restart firecracker-cloudstack-agent.service
```

### Host Filesystem Layout
- `/var/log/firecracker` – rolling log files created per VM (`<vm>.log`) and agent runtime diagnostics.
- `/var/run/firecracker` – transient sockets and PID files used while VMs are running (`<vm>.socket`, `<vm>.pid`).
- `/var/lib/firecracker/images` – guest rootfs images made available to Firecracker (ext4/RAW, typically referenced by template `image`).
- `/var/lib/firecracker/kernel` – uncompressed `vmlinux` kernels referenced by template `kernel`.
- `/var/lib/firecracker/conf` – rendered Firecracker machine configuration JSON files (`<vm>.json`) persisted for troubleshooting.
- `/var/lib/firecracker/volumes` – disk volumes created when the `file` storage backend is selected.
- `/var/lib/firecracker/payload` – raw payloads uploaded by CloudStack (cloud-init data, ISO metadata, temporary artifacts).

### Tmux Access
- Each VM runs inside a detached tmux session named `fc-<vm_name>`; list active sessions with `tmux ls`.
- Attach to the microVM console using `tmux attach -t fc-<vm_name>` and detach without stopping it via `Ctrl-b d`.
- If a session is missing, the agent recreates it when the VM boots; use `tmux kill-session -t fc-<vm_name>` only for advanced troubleshooting.

### VNC Console Bridge
- `POST /v1/vms/{name}/console` spawns an `Xvfb` + `xterm` + `x11vnc` bridge bound to the VM's tmux session and returns `{host, port, password}` ready for the CloudStack console proxy. `DELETE /v1/vms/{name}/console` tears it down.
- The CLI helper now supports `firecracker.py console <payload.json>` to fetch the same tuple programmatically.
- Runtime assets live under `/var/run/firecracker/vnc/` (state JSON, password files). Adjust `defaults.console` in the agent config to tune port ranges, bind address, window geometry, fonts, or read-only mode.
- Ensure the host has `x11vnc`, `xterm`, and `xvfb` installed; the Debian packaging pulls these dependencies.
---
## CloudStack Integration
1. **Install extension on the management server**:
   ```bash
   sudo apt install -y python3-requests
   sudo install -m 0755 firecracker.py \
     /usr/share/cloudstack-management/extensions/firecracker/firecracker.py
   ```
2. **Register in CloudStack UI/API**:
   - Extension: name `Firecracker`, type `Orchestrator`, path `firecracker.py`.
   - Cluster: create External → Firecracker, associate hosts pointing to the HTTPS URL (`https://<host>:8443`).
   - Host config key/values: `url`, `port`, `username`, `password`, `skip_ssl_verification` (set to `false` if trusting the CA, `true` otherwise), plus optional `client_cert`/`client_key` when mTLS is enabled.
3. **Templates** – add Firecracker-specific template details:
   - `kernel`: filename located under the agent's `defaults.host.kernel_dir` (for example `vmlinux-6.1.bin`).
   - `image`: filename stored in `defaults.host.image_dir` (for example `alpine-3.22.ext4`).
   - `boot_args`: optional kernel command line, e.g. `console=ttyS0 reboot=k panic=1 pci=off ip=dhcp`.
   Provide filenames only; the agent resolves them against its configured directories on each host.

---
## Host HOWTOs
- [Network configuration](docs/network.md) – VLAN-aware Linux bridges, OVS setup, persistence tips and validation commands.
- [Storage backends](docs/storage.md) – File, LVM, and LVM-thin workflows, including NFS-backed directories and thin-pool tuning.
- [Security hardening](docs/security.md) – TLS/mTLS generation, PAM authentication, and guidance for internal certificates.

---
## Troubleshooting
- `systemctl status firecracker-cloudstack-agent.service` for service health.
- `journalctl -u firecracker-cloudstack-agent.service -f` shows TLS/auth decisions (`TLS enabled`, `Authentication enabled ...`).
- `curl -ks https://<host>:<port>/healthz` for readiness; supply `-u user:pass` when PAM auth is active.
- To rotate certificates, replace files under `/etc/cloudstack/tls-cert` and restart the service.

---
## Contributing
Issues and patches are welcome. Please:
- Run `python3 -m compileall` on touched modules.
- Add tests or manual validation notes for new features.
- Follow the existing logging style (`logger = logging.getLogger("fc-agent")`).

Licensed to the Apache Software Foundation (ASF) under the Apache License 2.0.
