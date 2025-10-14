<template>
  <div>
    <div v-if="loading" class="alert alert-info" role="alert">
      Loading detailed configuration…
    </div>

    <div v-else-if="error" class="alert alert-warning" role="alert">
      {{ error }}
      <button class="btn btn-xs btn-primary" type="button" @click="$emit('retry')">Retry</button>
    </div>

    <div v-else-if="!details" class="empty-state">
      Select this VM to load detailed information.
    </div>

    <div v-else>
      <section>
        <h3 class="section-title">Compute</h3>
        <table class="details-table">
          <tbody>
            <tr>
              <td>Power state</td>
              <td>{{ details.power_state }}</td>
            </tr>
            <tr>
              <td>vCPUs</td>
              <td>{{ details.vm_config?.cpus ?? "-" }}</td>
            </tr>
            <tr>
              <td>Memory</td>
              <td>{{ memoryLabel }}</td>
            </tr>
            <tr>
              <td>Kernel Image</td>
              <td>{{ details.vm_config?.kernel_image_path || "-" }}</td>
            </tr>
            <tr>
              <td>Boot Args</td>
              <td><code>{{ details.vm_config?.boot_args || "-" }}</code></td>
            </tr>
          </tbody>
        </table>
      </section>

      <section>
        <h3 class="section-title">Storage</h3>
        <table class="details-table">
          <tbody>
            <tr>
              <td>Driver</td>
              <td>{{ details.storage?.driver || "-" }}</td>
            </tr>
            <tr>
              <td>Volume File</td>
              <td>{{ details.storage?.volume_file || "-" }}</td>
            </tr>
            <tr>
              <td>Device Path</td>
              <td>{{ details.storage?.device_path || "-" }}</td>
            </tr>
            <tr>
              <td>Size</td>
              <td>{{ storageSizeLabel }}</td>
            </tr>
            <tr>
              <td>Read Only</td>
              <td>{{ (details.storage?.is_read_only ?? false) ? "Yes" : "No" }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section>
        <h3 class="section-title">Network</h3>
        <table class="details-table">
          <tbody>
            <tr>
              <td>Interfaces</td>
              <td>
                <div v-if="networkInterfaces.length === 0">No interfaces discovered</div>
                <div v-for="iface in networkInterfaces" :key="iface.iface_id" style="margin-bottom: 6px">
                  <strong>{{ iface.iface_id || "eth" }}</strong>
                  · MAC {{ iface.guest_mac || "-" }}
                  <span v-if="iface.host_dev_name">· tap {{ iface.host_dev_name }}</span>
                </div>
              </td>
            </tr>
            <tr>
              <td>Saved NIC</td>
              <td v-if="payloadNic">
                MAC {{ payloadNic.mac }} · IP {{ payloadNic.ip }} · Netmask {{ payloadNic.netmask }}
                <span v-if="payloadNic.gateway">· Gateway {{ payloadNic.gateway }}</span>
                <div v-if="payloadNic.network_name" class="timestamp">
                  Network: {{ payloadNic.network_name }}
                </div>
              </td>
              <td v-else>-</td>
            </tr>
            <tr>
              <td>VLAN</td>
              <td>{{ details.payload?.vlan || "-" }}</td>
            </tr>
            <tr>
              <td>Uplink</td>
              <td>{{ details.payload?.uplink || details.network?.saved_config?.uplink || "-" }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section>
        <h3 class="section-title">Image & Payload</h3>
        <table class="details-table">
          <tbody>
            <tr>
              <td>Origin Image</td>
              <td>{{ details.payload?.image || "-" }}</td>
            </tr>
            <tr>
              <td>Kernel (payload)</td>
              <td>{{ details.payload?.kernel || "-" }}</td>
            </tr>
            <tr>
              <td>Boot Args (payload)</td>
              <td><code>{{ details.payload?.boot_args || "-" }}</code></td>
            </tr>
            <tr>
              <td>Payload Source</td>
              <td>{{ details.payload?.source || "-" }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section>
        <h3 class="section-title">Paths</h3>
        <table class="details-table">
          <tbody>
            <tr>
              <td>Firecracker Config</td>
              <td>{{ details.paths?.config_file || "-" }}</td>
            </tr>
            <tr>
              <td>Log File</td>
              <td>{{ details.paths?.log_file || "-" }}</td>
            </tr>
            <tr>
              <td>Socket File</td>
              <td>{{ details.paths?.socket_file || "-" }}</td>
            </tr>
            <tr>
              <td>PID File</td>
              <td>{{ details.paths?.pid_file || "-" }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section style="margin-top: 20px">
        <button class="btn btn-xs btn-default" type="button" @click="rawVisible = !rawVisible">
          {{ rawVisible ? "Hide raw payload" : "Show raw payload JSON" }}
        </button>
        <div v-if="rawVisible" style="margin-top: 12px">
          <pre style="max-height: 320px; overflow: auto">{{ prettyPayload }}</pre>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from "vue";

const props = defineProps({
  vmName: {
    type: String,
    required: true,
  },
  details: {
    type: Object,
    default: null,
  },
  loading: {
    type: Boolean,
    default: false,
  },
  error: {
    type: String,
    default: "",
  },
});

defineEmits(["retry"]);

const rawVisible = ref(false);

const memoryLabel = computed(() => {
  const mib = props.details?.vm_config?.memory_mib;
  if (!mib) return "-";
  if (mib >= 1024) {
    return `${(mib / 1024).toFixed(1)} GiB`;
  }
  return `${mib} MiB`;
});

const storageSizeLabel = computed(() => {
  const bytes = props.details?.storage?.size_bytes;
  if (!bytes) return "-";
  if (bytes >= 1024 ** 3) {
    return `${(bytes / 1024 ** 3).toFixed(2)} GiB (${bytes.toLocaleString()} bytes)`;
  }
  if (bytes >= 1024 ** 2) {
    return `${(bytes / 1024 ** 2).toFixed(1)} MiB (${bytes.toLocaleString()} bytes)`;
  }
  return `${bytes.toLocaleString()} bytes`;
});

const networkInterfaces = computed(() => props.details?.network?.interfaces || []);
const payloadNic = computed(() => props.details?.payload?.nic || null);
const prettyPayload = computed(() => {
  if (!props.details?.payload?.raw) {
    return "No payload data available.";
  }
  try {
    return JSON.stringify(props.details.payload.raw, null, 2);
  } catch (err) {
    return "Failed to render payload JSON.";
  }
});
</script>
