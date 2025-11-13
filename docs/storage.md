# Storage Configuration HOWTO

This guide covers the three storage drivers supported by the Firecracker CloudStack Agent (`file`, `lvm`, and `lvmthin`) plus common layouts such as local disks and NFS-backed directories.

## `defaults.storage` keys

| Key | Required | Used by | Description |
| - | - | - | - |
| `driver` | yes | all | `file`, `lvm`, or `lvmthin`. |
| `volume_dir` | yes (driver `file`) | file | Base directory where RAW/EXT4 images are created. |
| `vg` / `volume_group` | yes (`lvm`, `lvmthin`) | lvm / lvmthin | LVM volume group that holds the logical volumes. |
| `thinpool` | yes (`lvmthin`) | lvmthin | Thin-pool LV located inside `vg`. |
| `size` | optional | all | Size hint (for example `50G`) when the template does not include disk metadata. |

---

## Driver `file`

Useful for labs or hosts without LVM. Volumes are sparse files created inside `volume_dir`.

### 1. Local directory

```bash
sudo install -d -m 0750 /var/lib/firecracker/volumes
sudo chown firecracker:firecracker /var/lib/firecracker/volumes
```

Configure:

```json
"defaults": {
  "storage": {
    "driver": "file",
    "volume_dir": "/var/lib/firecracker/volumes"
  }
}
```

### 2. NFS directory

Mount the export with options that keep sparse files and locking intact:

```bash
sudo apt install -y nfs-common
sudo mkdir -p /mnt/firecracker-volumes
echo "nfs01:/exports/fc-volumes /mnt/firecracker-volumes nfs defaults,_netdev,vers=4.1 0 0" | sudo tee -a /etc/fstab
sudo mount -a
```

Point `volume_dir` to the mounted path. Validate I/O latency because Firecracker uses synchronous access (`io_engine=Sync`).

---

## Driver `lvm`

Creates one thick-provisioned LV per VM inside a traditional volume group.

### 1. Prepare the VG

```bash
sudo pvcreate /dev/nvme1n1
sudo vgcreate fc-vg /dev/nvme1n1
```

### 2. Configuration

```json
"defaults": {
  "storage": {
    "driver": "lvm",
    "vg": "fc-vg"
  }
}
```

- Ensure the agent’s service account can run `lvcreate`, `lvremove`, and `lvchange`.

### 3. Best practices

- Enable `issue_discards = 1` in `lvm.conf` to reclaim space after deletions (when the device supports TRIM).
- Use `lvdisplay -m` to verify LVs are active after provisioning.

---

## Driver `lvmthin`

Leverages thin pools for fast snapshots; ideal for sharing base templates.

### 1. Create the thin pool

```bash
sudo pvcreate /dev/nvme2n1
sudo vgcreate fc-vg /dev/nvme2n1
sudo lvcreate -T -L 500G -n fc-thinpool fc-vg
```

- Adjust `chunksize` with `-c 256K` if you need a specific performance profile.

### 2. Configuration

```json
"defaults": {
  "storage": {
    "driver": "lvmthin",
    "vg": "fc-vg",
    "thinpool": "fc-thinpool"
  }
}
```

### 3. Internal workflow

1. The agent ensures a base LV (`base-<template>`) exists. If not, it creates a virtual volume (`lvcreate -V SIZE -T vg/thinpool`) and writes the template image into it.
2. For each VM it creates a thin snapshot (`lvcreate -s`) that points to the base LV.
3. After every creation or reuse, the agent forces `lvchange -kn`, `lvchange -ay`, and `udevadm settle` so `/dev/<vg>/<lv>` is available immediately.

### 4. Monitoring

- Use `lvs -a -o+seg_monitor` to check thin-pool health.
- Alert when `lv_thin_pool` usage exceeds ~80% (for example using `lvs --report-format json` + Prometheus).

---

## Base volumes on NFS + LVM

You can expose block devices via iSCSI/NFS (LIO, targetcli, etc.) and build a PV/VG on top. Ensure aligned block sizes and persist the device via UUID before assembling the VG.

---

## Final validation

1. Restart the agent: `sudo systemctl restart firecracker-cloudstack-agent`.
2. Run `firecracker.sh storage prepare <payload.json>` to test the backend.
3. Confirm the device exists:

```bash
ls -l /var/lib/firecracker/volumes   # file driver
sudo lvs -a fc-vg                    # LVM/LVMThin
```

Following these steps prepares the host storage layer for CloudStack workloads.
