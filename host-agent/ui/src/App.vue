<template>
  <div class="app-root">
    <LoginPage
      v-if="!isAuthenticated"
      :error="loginError"
      :loading="loginLoading"
      @submit="handleLoginSubmit"
    />

    <div v-else class="app-shell">
      <header class="fc-navbar">
        <div class="container-fluid">
          <div class="navbar-content">
            <div>
              <h1>CloudStack Firecracker Host</h1>
              <p class="timestamp">Last refreshed: {{ lastUpdatedLabel }}</p>
            </div>
            <div class="auth-controls">
              <button class="btn btn-default btn-sm" @click="handleLogout">Sign out</button>
            </div>
          </div>

          <div v-if="hostSummary" class="host-summary">
            <div class="host-meta">
              <div class="host-heading">
                <span class="host-name">{{ hostSummary.hostname || "Unknown host" }}</span>
                <span v-if="hostSummary.fqdn && hostSummary.fqdn !== hostSummary.hostname" class="host-fqdn">
                  ({{ hostSummary.fqdn }})
                </span>
              </div>
              <div v-if="primaryAddress" class="host-address">
                <span class="glyphicon glyphicon-map-marker" aria-hidden="true"></span>
                <span>{{ primaryAddress.address }} · {{ primaryAddress.interface }}</span>
              </div>
              <div v-else class="host-address muted">No IP address detected</div>
            </div>

            <div class="host-stat-grid">
              <div class="host-stat">
                <span class="label">CPUs</span>
                <span class="value">{{ cpuLabel }}</span>
              </div>
              <div class="host-stat">
                <span class="label">Clock</span>
                <span class="value">{{ cpuClockLabel }}</span>
              </div>
              <div class="host-stat">
                <span class="label">Memory</span>
                <span class="value">{{ memoryLabel }}</span>
              </div>
              <div class="host-stat">
                <span class="label">Disk</span>
                <span class="value">{{ diskLabel }}</span>
              </div>
              <div class="host-stat">
                <span class="label">Uptime</span>
                <span class="value">{{ uptimeLabel }}</span>
              </div>
            </div>
          </div>
        </div>
      </header>

      <main class="container-fluid" style="max-width: 1200px">
        <VmList ref="vmListRef" @update-timestamp="updateTimestamp" @auth-required="handleAuthRequired" />
      </main>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import LoginPage from "./components/LoginPage.vue";
import VmList from "./components/VmList.vue";
import { api, clearAuth, loadAuthFromStorage, setBasicAuth } from "./services/apiClient";

const lastUpdated = ref(null);
const loginError = ref("");
const loginLoading = ref(false);
const isAuthenticated = ref(false);
const vmListRef = ref(null);
const hostSummary = ref(null);

const updateTimestamp = (value) => {
  lastUpdated.value = value;
};

const setAuthState = (state) => {
  isAuthenticated.value = state;
  if (!state) {
    lastUpdated.value = null;
    hostSummary.value = null;
  }
};

const handleAuthRequired = (message) => {
  clearAuth();
  setAuthState(false);
  loginLoading.value = false;
  hostSummary.value = null;
  if (typeof message === "string" && message.trim()) {
    loginError.value = message;
  } else if (!loginError.value) {
    loginError.value = "Authentication is required to access this host.";
  }
};

async function fetchHostSummary() {
  try {
    const { data } = await api.get("/v1/host/summary");
    hostSummary.value = data?.host || null;
  } catch (error) {
    console.warn("Failed to fetch host summary", error);
    hostSummary.value = null;
  }
}

const handleLoginSubmit = async ({ username, password }) => {
  loginLoading.value = true;
  loginError.value = "";
  try {
    setBasicAuth(username, password);
    await api.get("/v1/vms");
    setAuthState(true);
    await fetchHostSummary();
  } catch (error) {
    clearAuth();
    setAuthState(false);
    if (error?.response?.status === 401) {
      loginError.value = "Invalid credentials. Please try again.";
    } else {
      loginError.value = error?.message || "Failed to authenticate. Please try again.";
    }
  } finally {
    loginLoading.value = false;
  }
};

const handleLogout = () => {
  clearAuth();
  setAuthState(false);
  loginError.value = "";
};

const lastUpdatedLabel = computed(() => {
  if (!lastUpdated.value) {
    return "never";
  }
  return new Date(lastUpdated.value).toLocaleString();
});

const primaryAddress = computed(() => {
  const addresses = hostSummary.value?.ip_addresses;
  if (!Array.isArray(addresses) || addresses.length === 0) {
    return null;
  }
  const primary = addresses.find((entry) => entry.family === "IPv4" && !entry.is_loopback);
  return primary || addresses[0];
});

const cpuLabel = computed(() => {
  const cpu = hostSummary.value?.cpu;
  if (!cpu) return "-";
  const sockets = cpu.sockets ?? cpu.physical_cores;
  const logical = cpu.logical_cores;
  if (sockets && logical) {
    return `${sockets} sockets / ${logical} threads`;
  }
  if (logical) {
    return `${logical} cores`;
  }
  return "-";
});

const cpuClockLabel = computed(() => {
  const cpu = hostSummary.value?.cpu;
  if (!cpu) return "-";
  const freq = cpu.max_frequency_mhz || cpu.current_frequency_mhz;
  if (!freq) return "-";
  if (freq >= 1000) {
    return `${(freq / 1000).toFixed(1)} GHz`;
  }
  return `${freq} MHz`;
});

const memoryLabel = computed(() => {
  const totalBytes = hostSummary.value?.memory?.total_bytes;
  if (!totalBytes) return "-";
  const gib = totalBytes / 1024 ** 3;
  return `${gib.toFixed(1)} GiB`;
});

const diskLabel = computed(() => {
  const disks = hostSummary.value?.disks;
  if (!Array.isArray(disks) || disks.length === 0) {
    return "-";
  }
  const totalBytes = disks.reduce((sum, disk) => sum + (disk.total_bytes || 0), 0);
  if (!totalBytes) {
    return "-";
  }
  const tib = totalBytes / 1024 ** 4;
  if (tib >= 1) {
    return `${tib.toFixed(2)} TiB`;
  }
  const gib = totalBytes / 1024 ** 3;
  return `${gib.toFixed(1)} GiB`;
});

const uptimeLabel = computed(() => {
  const seconds = hostSummary.value?.uptime_seconds;
  if (!seconds) return "-";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days > 0) {
    return `${days}d ${hours}h`;
  }
  if (hours > 0) {
    return `${hours}h ${minutes}m`;
  }
  return `${minutes} minutes`;
});

onMounted(() => {
  const restored = loadAuthFromStorage();
  setAuthState(restored);
  if (!restored) {
    loginError.value = "";
  } else {
    fetchHostSummary().catch(() => {
      /* non-fatal */
    });
  }
});
</script>

<style scoped>
.host-summary {
  margin-top: 16px;
  padding: 16px;
  border-radius: 10px;
  background: rgba(0, 0, 0, 0.18);
  display: flex;
  flex-wrap: wrap;
  gap: 24px;
}

.host-meta {
  min-width: 240px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.host-heading {
  font-size: 16px;
  font-weight: 600;
}

.host-name {
  margin-right: 6px;
}

.host-fqdn {
  color: rgba(255, 255, 255, 0.65);
  font-size: 13px;
}

.host-address {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: rgba(255, 255, 255, 0.85);
}

.host-address.muted {
  color: rgba(255, 255, 255, 0.6);
}

.host-stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 12px;
  flex: 1;
}

.host-stat {
  background: rgba(0, 0, 0, 0.28);
  border-radius: 10px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.host-stat .label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: rgba(255, 255, 255, 0.6);
}

.host-stat .value {
  font-size: 15px;
  font-weight: 500;
  color: #ffffff;
}

@media (max-width: 768px) {
  .host-summary {
    flex-direction: column;
  }
}
</style>
