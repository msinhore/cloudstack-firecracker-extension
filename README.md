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
   The package pulls `python3-pamela`, `python3-uvicorn`, FastAPI, Open vSwitch bindings and other requirements. Post-install scripts:
   - create `/etc/cloudstack/tls-cert`
   - generate `ca.crt`, `server.crt`, `server.key`
   - install `/etc/systemd/system/firecracker-cloudstack-agent.service`

3. **Enable the service**:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now firecracker-cloudstack-agent.service
   ```
   Check status:
   ```bash
   journalctl -u firecracker-cloudstack-agent.service -f
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
