# Network Configuration HOWTO

This document explains how to prepare an Ubuntu host for the `linux-bridge-vlan` and `ovs-vlan` networking drivers used by the Firecracker CloudStack Agent. In both cases the agent creates a TAP interface per VM and applies VLAN tags provided by CloudStack.

## `defaults.net` keys

| Key | Required | Description |
| - | - | - |
| `driver` | yes | `linux-bridge-vlan` or `ovs-vlan`. |
| `host_bridge` | yes | Linux bridge or OVS integration bridge where TAPs are attached. |
| `bridge` | no | Explicit bridge name when it differs from `host_bridge`. |
| `uplink` | no | Physical uplink interface (for example `eno1`). Strongly recommended for `ovs-vlan`. |

Configure these keys under `defaults.net` in `/etc/cloudstack/firecracker-agent.json`.

---

## linux-bridge-vlan

### 1. Create the bridge

```bash
BRIDGE=cloudbr1
sudo ip link add name "$BRIDGE" type bridge vlan_filtering 1
sudo ip link set "$BRIDGE" up
```

- Enable IPv4/IPv6 forwarding on the host if routing traffic.
- Optionally enslave a physical NIC to the bridge:

```bash
sudo ip link set eno1 master "$BRIDGE"
```

### 2. Persist with Netplan (systemd-networkd)

`/etc/netplan/60-cloudbr1.yaml`:

```yaml
network:
  version: 2
  renderer: networkd
  bridges:
    cloudbr1:
      interfaces: [eno1]
      parameters:
        stp: false
        forward-delay: 0
      dhcp4: no
      addresses: [192.0.2.10/24]
      gateway4: 192.0.2.1
```

Apply with `sudo netplan apply`.

#### Alternative: systemd-networkd units

If you prefer systemd-networkd, create `/etc/systemd/network/10-cloudbr1.netdev`:

```
[NetDev]
Name=cloudbr1
Kind=bridge

[Bridge]
VLANFiltering=yes
DefaultPVID=0
MulticastSnooping=no
```

Then `/etc/systemd/network/10-cloudbr1.network`:

```
[Match]
Name=cloudbr1

[Network]
DHCP=no
```

Finally, enslave the uplink via `/etc/systemd/network/20-eth1.network`:

```
[Match]
Name=eth1

[Network]
Bridge=cloudbr1

[Link]
RequiredForOnline=no
```

Reload networkd:

```bash
sudo systemctl enable --now systemd-networkd
sudo systemctl restart systemd-networkd
bridge vlan show
cat /sys/class/net/cloudbr1/bridge/vlan_filtering
```

You should see `cloudbr1  vlan_filtering 1`, confirming VLAN filtering is active.

### 3. Agent configuration

```json
"defaults": {
  "net": {
    "driver": "linux-bridge-vlan",
    "host_bridge": "cloudbr1",
    "uplink": "eno1"
  }
}
```

The agent uses `ip link` and `bridge vlan` to plug TAP devices, assign VLAN IDs, and remove interfaces once the VM stops.

---

## ovs-vlan

### 1. Install dependencies

```bash
sudo apt install -y openvswitch-switch python3-openvswitch
sudo systemctl enable --now openvswitch-switch
```

### 2. Create the integration bridge

```bash
BRIDGE=cloudbr1
sudo ovs-vsctl add-br "$BRIDGE"
sudo ovs-vsctl add-port "$BRIDGE" eno1
sudo ip link set "$BRIDGE" up
```

- Use `ovs-vsctl set interface eno1 ofport_request=1` if deterministic port numbers are needed.
- Configure VLAN trunking according to your upstream network (the agent allocates one VLAN per VM by default).

### 3. Persist OVS settings

Open vSwitch stores configuration inside `ovsdb`. To ensure the physical NIC is reapplied after reboot:

```bash
sudo ovs-vsctl set bridge cloudbr1 other-config:hwaddr=<HOST_MAC>
sudo ovs-vsctl set Interface cloudbr1 mtu_request=9000  # optional
```

Keep the bridge up after reboots with a small systemd unit (optional):

`/etc/systemd/system/cloudbr1-up.service`

```
[Unit]
Description=Bring up OVS bridge cloudbr1
After=network-online.target openvswitch-switch.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/ip link set cloudbr1 up
ExecStart=/usr/bin/ip link set ens36 up
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

Enable it:

```bash
sudo systemctl enable --now cloudbr1-up.service
```

### 4. Agent configuration

```json
"defaults": {
  "net": {
    "driver": "ovs-vlan",
    "host_bridge": "cloudbr1",
    "uplink": "eno1"
  }
}
```

The `ovs-vlan` backend relies on `ovs-vsctl`/`ovsdbapp` to create TAP ports, assign `tag=<vlan>`, and delete the port when the VM is torn down.

---

## Quick validation

1. Restart the agent: `sudo systemctl restart firecracker-cloudstack-agent`.
2. Request diagnostics: `curl -ks https://HOST:PORT/v1/host/summary`.
3. Create a test VM and confirm the TAP device is attached:

```bash
bridge vlan show
sudo ovs-vsctl show
```

With these steps the host is ready to accept CloudStack workloads using either supported networking backend.
