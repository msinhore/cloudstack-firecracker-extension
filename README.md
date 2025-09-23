# Firecracker for Apache CloudStack

Lightweight integration that lets CloudStack orchestrate Firecracker microVMs through an external hypervisor extension and a host-side agent. The extension runs on the management server, while each Firecracker host runs a FastAPI agent that performs storage, networking, and Firecracker orchestration.

---
## Table of Contents
1. [Architecture](#architecture)
2. [CloudStack Extension Setup](#cloudstack-extension-setup)
   - [Management Server Requirements](#management-server-requirements)
   - [Installing the Extension](#installing-the-extension)
   - [CloudStack Host & Template Settings](#cloudstack-host--template-settings)
3. [Host Agent Setup](#host-agent-setup)
   - [Host Requirements](#host-requirements)
   - [Installing the Debian Package](#installing-the-debian-package)
   - [Configuration File](#configuration-file)
4. [Storage Backends](#storage-backends)
5. [Networking Backends](#networking-backends)
6. [Lifecycle Operations](#lifecycle-operations)
   - [REST API](#rest-api)
   - [CLI Usage](#cli-usage)
7. [Recovery Workflow](#recovery-workflow)
8. [Troubleshooting](#troubleshooting)
9. [Debian Packaging Notes](#debian-packaging-notes)
10. [Support](#support)

---
## Architecture

```
CloudStack (management server)
 └─ firecracker.py (extension)
     └─ HTTP/JSON → Firecracker Agent (FastAPI)
         ├─ Storage backends (file, LVM, LVM thin)
         ├─ Networking backends (Linux bridge VLAN, OVS VLAN)
         └─ Firecracker VM process (tmux + config file)
```

1. CloudStack invokes `firecracker.py <op> <payload.json> [timeout]`.
2. The client validates the payload, reads host URL/port, and forwards the JSON to the agent.
3. The agent prepares storage, sets up networking, writes Firecracker config, injects SSH keys, and runs Firecracker.
4. The agent responds with JSON and persists metadata (volume, network, VM state) for recovery.

---
## CloudStack Extension Setup

### Management Server Requirements
- Python 3.8+
- `python3-requests`

Install on Debian/Ubuntu:
```bash
apt update
apt install -y python3-requests
```

### Installing the Extension

Copy the script and mark it executable:
```bash
install -m 0755 firecracker.py \
  /usr/share/cloudstack-management/extensions/firecracker/firecracker.py
```

### CloudStack Host & Template Settings

**Host (External Hypervisor → Host)**
- `url`: `http://<firecracker-host>` (required)
- `port`: `8080` (defaults to 8080 if omitted)

Networking, storage paths, and credentials remain on the agent host.

**Template (External Hypervisor → Template)**
- `image`: name of the rootfs file in the agent `image_dir`
- `kernel`: name of the kernel image in the agent `kernel_dir`
- `boot_args`: optional kernel arguments

Flattened fields (`vm_name`, `vm_cpus`, `vm_ram`, `vm_vlans`, etc.) are accepted and forwarded unchanged to the agent.

---
## Host Agent Setup
### Host Requirements
- Ubuntu 22.04 or 24.04 (other modern distributions should work)
- Kernel modules: `tun`, `bridge`, `br_netfilter`
- Firecracker binary (e.g., `/usr/local/bin/firecracker`)
- Required utilities: `iproute2`, `bridge-utils`, `tmux`, `jq`, `curl`
- Python packages: `python3-fastapi`, `python3-uvicorn`, `python3-typer`, `python3-psutil`, `python3-libtmux`, `python3-pyroute2`, `python3-pydantic`, `python3-starlette`, `python3-ovsdbapp` (for OVS support)

Example installation:
```bash
apt update
apt install -y \
  jq curl iproute2 bridge-utils tmux lvm2 thin-provisioning-tools \
  python3-fastapi python3-uvicorn python3-typer python3-psutil \
  python3-libtmux python3-pyroute2 python3-pydantic python3-starlette \
  python3-ovsdbapp
```

Create directories:
```bash
mkdir -p /var/lib/firecracker/{images,kernel,conf,volumes,payload}
mkdir -p /var/log/firecracker /var/run/firecracker
```

Optional: disable bridge netfiltering
```bash
sysctl -w net.bridge.bridge-nf-call-iptables=0
sysctl -w net.bridge.bridge-nf-call-ip6tables=0
sysctl -w net.bridge.bridge-nf-call-arptables=0
```

Ensure a VLAN-aware bridge (for example `cloudbr1`) exists with the uplink enslaved.

### Installing the Debian Package

```bash
sudo dpkg -i firecracker-cloudstack-agent_<version>.deb
sudo systemctl daemon-reload
sudo systemctl enable --now firecracker-cloudstack-agent.service
```

Key files:
- `/usr/lib/firecracker-cloudstack-agent/` – Python modules
- `/usr/bin/firecracker-cloudstack-agent` – CLI wrapper
- `/etc/cloudstack/firecracker-agent.json` – configuration (copy from the examples if needed)
- `/etc/systemd/system/firecracker-cloudstack-agent.service`

Check status:
```bash
systemctl status firecracker-cloudstack-agent.service
ss -ltnp | grep 8080
```

### Configuration File
`/etc/cloudstack/firecracker-agent.json`:
```json
{
  "bind_host": "0.0.0.0",
  "bind_port": 8080,
  "defaults": {
    "host": {
      "firecracker_bin": "/usr/local/bin/firecracker",
      "conf_dir": "/var/lib/firecracker/conf",
      "run_dir": "/var/run/firecracker",
      "log_dir": "/var/log/firecracker",
      "image_dir": "/var/lib/firecracker/images",
      "kernel_dir": "/var/lib/firecracker/kernel"
    },
    "storage": {
      "driver": "file",
      "volume_dir": "/var/lib/firecracker/volumes"
    },
    "net": {
      "driver": "linux-bridge-vlan",
      "host_bridge": "cloudbr1",
      "uplink": "eth1"
    }
  }
}
```

Examples for LVM and LVM-thin live under `host-agent/`.

---
## Storage Backends

| Driver       | Description                                 | Requirements                     | Notes                                              |
|--------------|---------------------------------------------|----------------------------------|----------------------------------------------------|
| `file`       | Copy image to `volume_dir` and use raw file | None                             | Default; supports SSH key injection                |
| `lvm`        | Provision LV per VM and copy image          | `lvm2`, an existing VG           | Uses `lvcreate`, `mkfs`, `dd`                      |
| `lvmthin`    | Thin-provision snapshot from base LV        | Thin pool (`lvcreate -T` setup)  | Creates base LV on-demand, snapshots per VM        |

Select the driver via agent defaults or per-request `spec.storage.driver`. The agent automatically calls the right backend factory.

---
## Networking Backends

| Driver               | Description                                     | Requirements              | Behavior |
|----------------------|-------------------------------------------------|---------------------------|----------|
| `linux-bridge-vlan`  | TAP per NIC, Linux bridge VLAN filtering        | `bridge-utils`, `pyroute2`| Configures TAP PVID/untagged, uplink tagged        |
| `ovs-vlan`           | TAP per NIC, Open vSwitch VLAN tagging          | `python3-ovsdbapp`, OVS db| Access ports for TAPs, trunks VLANs on uplink      |

The agent extracts VLAN IDs from `cloudstack.vm.details.nics[].broadcastUri` (`vlan://<id>`). If no VLAN is supplied, networking fails.

Saved network configuration is written to `run_dir` (e.g., `/var/run/firecracker/network-config-<vm>.json`) and reused for recovery.

---
## Lifecycle Operations

### REST API
| Method | Path                              | Description                                   |
|--------|-----------------------------------|-----------------------------------------------|
| POST   | `/v1/vms`                         | Create + start VM from CloudStack spec        |
| POST   | `/v1/vms/{vm}/start`              | Start using existing config/disk              |
| POST   | `/v1/vms/{vm}/stop`               | Stop Firecracker process gracefully           |
| POST   | `/v1/vms/{vm}/reboot`             | Stop then start                               |
| DELETE | `/v1/vms/{vm}`                    | Stop and remove VM resources                  |
| POST   | `/v1/vms/{vm}/recover`            | Reapply networking / restart process          |
| GET    | `/v1/vms/{vm}/status`             | `poweron` / `poweroff` / `unknown`            |
| GET    | `/v1/network-config/{vm}`         | View saved network configuration              |
| POST   | `/v1/network-config/{vm}/apply`   | Reapply saved configuration                   |
| DELETE | `/v1/network-config/{vm}`         | Delete stored network config                  |
| POST   | `/v1/save-states`                 | Persist running VM state for restart recovery |
| GET    | `/v1/saved-states`                | Inspect persisted states                      |
| POST   | `/v1/recover-all`                 | Recover networking for all running VMs        |
| POST   | `/v1/graceful-shutdown`           | Stop all running VMs                          |
| GET    | `/v1` / `/healthz` / `/v1/version`| Info & health endpoints                       |

OpenAPI schema: `http://<host>:8080/openapi.json`.

### CLI Usage
Run locally on the host (API or CLI mode available):
```bash
python3 firecracker-agent.py --mode cli create  spec.json
python3 firecracker-agent.py --mode cli start   spec.json
python3 firecracker-agent.py --mode cli stop    spec.json
python3 firecracker-agent.py --mode cli reboot  spec.json
python3 firecracker-agent.py --mode cli delete  spec.json
python3 firecracker-agent.py --mode cli recover spec.json
python3 firecracker-agent.py --mode cli vm-status spec.json
```

### Extension CLI
```bash
python3 firecracker.py create  create-spec.json 1800
python3 firecracker.py start   create-spec.json 1800
python3 firecracker.py stop    create-spec.json 1800
python3 firecracker.py reboot  create-spec.json 1800
python3 firecracker.py delete  create-spec.json 1800
python3 firecracker.py status  create-spec.json 1800
python3 firecracker.py recover create-spec.json 1800
```

---
## Recovery Workflow (Not supported yet by CloudStack)

1. Agent startup scans `conf_dir` and `vm-states.json` to detect lingering VMs.
2. If the agent was restarted (daemon restart), it reapplies saved network config. If the host was rebooted, it restarts Firecracker for VMs marked `poweron`.
3. `POST /v1/vms/{vm}/recover`:
   - Try saved network-config JSON.
   - If missing or stale, use the provided spec (payload). The agent re-prepares networking and re-saves config.
   - As a last resort, reconstruct spec from the stored Firecracker config.
4. CLI `recover` mirrors the REST path for on-host operations.

Keep volumes, conf files, and network metadata intact for recovery to succeed. Deleting a VM via the API/CLI removes the saved network config automatically.

---
## Troubleshooting

**Processes & tmux**
```bash
ps -ef | grep firecracker
tmux ls
```

**Logs**
```bash
tail -n 200 /var/log/firecracker/<vm>.log
journalctl -u firecracker-cloudstack-agent.service
```

**Networking**
```bash
brctl show
bridge vlan show
bridge vlan show dev <tap>
bridge vlan show dev <uplink>
```

**Agent API**
```bash
curl -s http://localhost:8080/v1/vms | jq
curl -s http://localhost:8080/v1/network-config/<vm> | jq
```

**Firecracker socket**
```bash
curl -sS --unix-socket /var/run/firecracker/<vm>.socket \
     -H 'Accept: application/json' http://localhost/machine-config
```

---
## Debian Packaging Notes

### Build Dependencies
```bash
apt update
apt install -y build-essential debhelper devscripts
```

### Building
```bash
cd host-agent/pkg
# Preferred (uses debhelper rules)
dpkg-buildpackage -us -uc
```

Generated packages land in the repo root (`../firecracker-cloudstack-agent_<ver>_<arch>.deb`). Install the package on each Firecracker host as described earlier.
