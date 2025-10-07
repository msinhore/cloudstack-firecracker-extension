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

---
## Host Installation (Debian/Ubuntu)
1. **Prepare networking** – ensure a VLAN-aware bridge exists (example `cloudbr1` enslaving `eth1`). Systemd-networkd snippet:
   ```ini
   # /etc/systemd/network/10-cloudbr1.netdev
   [NetDev]
   Name=cloudbr1
   Kind=bridge

   [Bridge]
   VLANFiltering=yes
   DefaultPVID=0
   MulticastSnooping=no

   # /etc/systemd/network/20-eth1.network
   [Match]
   Name=eth1

   [Network]
   Bridge=cloudbr1
   ```
   Reload with `systemctl restart systemd-networkd` and confirm `bridge vlan show`.

2. **Install Firecracker** – place the binary at `/usr/local/bin/firecracker`:
   ```bash
   FC_VERSION=1.13.1
   ARCH=$(uname -m)
   curl -LO https://github.com/firecracker-microvm/firecracker/releases/download/v${FC_VERSION}/firecracker-v${FC_VERSION}-${ARCH}.tgz
   tar -xzf firecracker-v${FC_VERSION}-${ARCH}.tgz
   sudo install -m 0755 firecracker-v${FC_VERSION}-${ARCH}/firecracker /usr/local/bin/firecracker
   ```

3. **Install the agent package**:
   ```bash
   sudo apt install -y ./firecracker-cloudstack-agent_<version>_all.deb
   ```
   The package pulls `python3-pamela`, `python3-uvicorn`, FastAPI, Open vSwitch bindings and other requirements. Post-install scripts:
   - create `/etc/cloudstack/tls-cert`
   - generate `ca.crt`, `server.crt`, `server.key`
   - install `/etc/systemd/system/firecracker-cloudstack-agent.service`

4. **Enable the service**:
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
Main file: `/etc/cloudstack/firecracker-agent.json` (shipped as a conffile). Example:
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
- **`defaults.storage`** – choose `file`, `lvm`, or `lvmthin`. Additional keys (e.g., `vg_name`, `thinpool_name`) follow the examples under `host-agent/`.
- **`defaults.net`** – select `linux-bridge-vlan` or `ovs-vlan`. Provide bridge name and optional uplink settings.
- **`security.tls`**
  - `enabled`: turn HTTPS on/off.
  - `cert_file`/`key_file`: server certificate/key. Replace the auto-generated pair as needed.
  - `ca_file`: CA used to sign the server cert and validate client certs when mTLS is enabled.
  - `client_auth`: `none`, `optional`, or `required` (mTLS).
- **`auth`**
  - `enabled`: when true, all `/v1/*` routes require HTTP Basic credentials.
  - `service`: name of the PAM stack (populate `/etc/pam.d/firecracker-agent`). The package depends on `python3-pamela`.

After edits, restart the service:
```bash
sudo systemctl restart firecracker-cloudstack-agent.service
```

---
## VM Assets (Kernel & RootFS)
- **Kernel** – Firecracker requires an uncompressed `vmlinux`. Quick extraction:
  ```bash
  sudo apt install -y binutils zstd linux-image-$(uname -r)
  sudo curl -fSL https://raw.githubusercontent.com/torvalds/linux/master/scripts/extract-vmlinux \
    -o /usr/local/bin/extract-vmlinux
  sudo chmod +x /usr/local/bin/extract-vmlinux
  sudo mkdir -p /var/lib/firecracker/kernel
  sudo /usr/local/bin/extract-vmlinux /boot/vmlinuz-$(uname -r) \
    | sudo tee /var/lib/firecracker/kernel/vmlinux-$(uname -r).bin >/dev/null
  ```
- **Root filesystem** – build an ext4 image from Alpine Mini RootFS:
  ```bash
  wget https://dl-cdn.alpinelinux.org/alpine/v3.22/releases/x86_64/alpine-minirootfs-3.22.1-x86_64.tar.gz
  mkdir -p /tmp/alpine-rootfs
  sudo tar -xzf alpine-minirootfs-3.22.1-x86_64.tar.gz -C /tmp/alpine-rootfs
  sudo dd if=/dev/zero of=/var/lib/firecracker/images/alpine-3.22.1.ext4 bs=1M count=512
  sudo mkfs.ext4 -F /var/lib/firecracker/images/alpine-3.22.1.ext4 -L rootfs
  sudo mount -o loop /var/lib/firecracker/images/alpine-3.22.1.ext4 /mnt/fcroot
  sudo rsync -aHAX /tmp/alpine-rootfs/ /mnt/fcroot/
  sudo umount /mnt/fcroot
  ```
  Configure networking/SSH inside the chroot as needed (see `docs/` for full recipe). Typical boot args: `console=ttyS0 reboot=k panic=1 pci=off ip=dhcp`.

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
3. **Templates** should reference kernel/image filenames stored on the agent and any custom `boot_args`.

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
