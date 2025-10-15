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
              <h1>CloudStack Firecracker Agent UI</h1>
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
              <div class="host-addresses">
                <template v-if="hostInterfaces.length">
                  <span v-for="iface in hostInterfaces" :key="iface.name" class="host-address-chip">
                    <strong>{{ iface.name }}</strong>
                    <span v-if="iface.mac" class="host-address-mac">· {{ iface.mac }}</span>
                    <span>· {{ iface.labels.join(', ') }}</span>
                  </span>
                </template>
                <span v-else class="host-address muted">No IP address detected</span>
              </div>
            </div>

            <div class="host-stat-grid">
              <div class="host-stat">
                <span class="label">CPUs</span>
                <span class="value">{{ cpuLabel }}</span>
              </div>
              <div class="host-stat">
                <span class="label">Memory</span>
                <span class="value">{{ memoryLabel }}</span>
              </div>
              <div class="host-stat">
                <span class="label">Disk</span>
                <span class="value">{{ diskLabel }}</span>
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
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import LoginPage from "./components/LoginPage.vue";
import VmList from "./components/VmList.vue";
import { api, clearAuth, loadAuthFromStorage, setBasicAuth } from "./services/apiClient";

const lastUpdated = ref(null);
const loginError = ref("");
const loginLoading = ref(false);
const isAuthenticated = ref(false);
const vmListRef = ref(null);
const hostSummary = ref(null);
const uiConfig = ref({
  enabled: true,
  session_timeout_seconds: 0,
});
let sessionExpiryHandle = null;

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
  clearSessionExpiry();
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
    scheduleSessionExpiry();
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
  clearSessionExpiry();
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

const sessionTimeoutSeconds = computed(() => {
  const raw = Number(uiConfig.value?.session_timeout_seconds ?? 0);
  return Number.isFinite(raw) && raw > 0 ? raw : 0;
});

const isBridgeInterface = (name) => {
  if (!name) {
    return false;
  }
  const lower = name.toLowerCase();
  return lower.startsWith("br") || lower.startsWith("virbr") || lower.includes("bridge");
};

const hostInterfaces = computed(() => {
  const summary = hostSummary.value;
  if (!summary) {
    return [];
  }

  const macMap = Object.create(null);
  (summary.mac_addresses || []).forEach((entry) => {
    if (entry?.interface && entry.mac) {
      macMap[entry.interface] = entry.mac;
    }
  });

  const addressMap = new Map();
  (summary.ip_addresses || []).forEach((entry) => {
    const iface = entry?.interface;
    const address = entry?.address;
    const family = entry?.family;
    if (!iface || !address) {
      return;
    }
    if (entry.is_loopback) {
      return;
    }
    if (/^f0-/i.test(iface)) {
      return;
    }
    if (isBridgeInterface(iface)) {
      return;
    }
    if (!addressMap.has(iface)) {
      addressMap.set(iface, []);
    }
    addressMap.get(iface).push({ address, family });
  });

  return Array.from(addressMap.entries())
    .map(([name, addresses]) => {
      const formatted = addresses.map((info) => {
        if (!info.family || info.family === "IPv4") {
          return info.address;
        }
        return `${info.address} (${info.family})`;
      });
      return {
        name,
        mac: macMap[name],
        labels: formatted,
      };
    })
    .filter((entry) => entry.labels.length > 0)
    .sort((a, b) => a.name.localeCompare(b.name));
});

const loadUiConfig = async () => {
  try {
    const { data } = await api.get("/v1/ui/config");
    const payload = (data && data.config) || data || {};
    const timeoutSeconds = Number(payload.session_timeout_seconds ?? 0);
    uiConfig.value = {
      enabled: payload.enabled !== false,
      session_timeout_seconds: Number.isFinite(timeoutSeconds) && timeoutSeconds > 0 ? timeoutSeconds : 0,
    };
  } catch (error) {
    console.warn("Failed to load UI configuration", error);
  }
};

const clearSessionExpiry = () => {
  if (sessionExpiryHandle !== null) {
    window.clearTimeout(sessionExpiryHandle);
    sessionExpiryHandle = null;
  }
};

const handleSessionExpired = () => {
  clearSessionExpiry();
  loginError.value = "Session expired. Please sign in again.";
  clearAuth();
  setAuthState(false);
};

const scheduleSessionExpiry = () => {
  clearSessionExpiry();
  const seconds = sessionTimeoutSeconds.value;
  if (!seconds) {
    return;
  }
  sessionExpiryHandle = window.setTimeout(() => {
    handleSessionExpired();
  }, seconds * 1000);
};
onMounted(() => {
  loadUiConfig().catch(() => {
    /* optional */
  });
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

watch(
  () => isAuthenticated.value,
  (value) => {
    if (value) {
      scheduleSessionExpiry();
      if (!hostSummary.value) {
        fetchHostSummary().catch(() => {
          /* non-fatal */
        });
      }
    } else {
      clearSessionExpiry();
      hostSummary.value = null;
    }
  }
);

watch(
  () => sessionTimeoutSeconds.value,
  (seconds) => {
    if (!seconds) {
      clearSessionExpiry();
      return;
    }
    if (isAuthenticated.value) {
      scheduleSessionExpiry();
    }
  }
);

onBeforeUnmount(() => {
  clearSessionExpiry();
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
  gap: 8px;
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

.host-addresses {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 13px;
  color: rgba(255, 255, 255, 0.85);
}

.host-address-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: rgba(0, 0, 0, 0.24);
  border-radius: 999px;
  padding: 6px 12px;
}

.host-address-chip strong {
  text-transform: lowercase;
}

.host-address-mac {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.7);
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
