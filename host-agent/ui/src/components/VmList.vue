<template>
  <div>
    <div class="row" style="margin-bottom: 12px">
      <div class="col-sm-4">
        <input
          v-model.trim="searchTerm"
          type="search"
          class="form-control"
          placeholder="Filter by name, status, VLAN or IP"
        />
      </div>
      <div class="col-sm-8 text-right">
        <button class="btn btn-default" @click="refresh" :disabled="loading">
          <span class="glyphicon glyphicon-refresh" aria-hidden="true"></span>
          Refresh
        </button>
      </div>
    </div>

    <div v-if="errorMessage" class="alert alert-danger" role="alert">
      {{ errorMessage }}
    </div>

    <div v-if="loading" class="alert alert-info" role="alert">
      Loading VM inventory…
    </div>

    <div v-if="!loading && filteredVms.length === 0" class="empty-state">
      <p>No virtual machines discovered on this host.</p>
    </div>

    <div v-for="vm in filteredVms" :key="vm.name" class="vm-card">
      <div class="vm-card-header">
        <div>
          <strong style="font-size: 16px">{{ vm.name }}</strong>
          <span class="badge-status" :class="statusClass(vm.status)">
            {{ vm.status }}
          </span>
          <div class="timestamp">
            CPUs: {{ vm.cpus }} · Memory: {{ formatMemory(vm.memory_mib) }} · Config: {{ vm.config_file }}
          </div>
        </div>
        <div>
          <button class="btn btn-link" type="button" @click="toggle(vm.name)">
            <span
              class="glyphicon"
              :class="isExpanded(vm.name) ? 'glyphicon-chevron-up' : 'glyphicon-chevron-down'"
              aria-hidden="true"
            ></span>
            {{ isExpanded(vm.name) ? "Hide details" : "Show details" }}
          </button>
        </div>
      </div>

      <transition name="fade">
        <div v-if="isExpanded(vm.name)" class="vm-card-body">
          <VmDetails
            :vm-name="vm.name"
            :details="detailsMap[vm.name]"
            :loading="Boolean(detailsLoading[vm.name])"
            :error="detailsError[vm.name]"
            @retry="retryDetails(vm.name)"
          />
        </div>
      </transition>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import VmDetails from "./VmDetails.vue";
import { api } from "../services/apiClient";

const emit = defineEmits(["update-timestamp", "auth-required"]);

const loading = ref(false);
const errorMessage = ref("");
const vms = ref([]);
const searchTerm = ref("");
const expanded = ref([]);
const detailsMap = reactive({});
const detailsLoading = reactive({});
const detailsError = reactive({});
let intervalHandle = null;

const statusClass = (status) => {
  const normalized = (status || "").toLowerCase();
  if (normalized.includes("on")) return "poweron";
  if (normalized.includes("off")) return "poweroff";
  return "unknown";
};

const formatMemory = (mib) => {
  if (!mib) return "-";
  if (mib >= 1024) {
    return `${(mib / 1024).toFixed(1)} GiB`;
  }
  return `${mib} MiB`;
};

const fetchVms = async () => {
  loading.value = true;
  errorMessage.value = "";
  try {
    const { data } = await api.get("/v1/vms");
    const entries = Array.isArray(data?.vms) ? data.vms : [];
    vms.value = entries.map((entry) => ({
      name: entry.name,
      status: entry.status,
      cpus: entry.cpus,
      memory_mib: entry.memory_mib,
      config_file: entry.config_file,
    }));
    emit("update-timestamp", Date.now());
  } catch (error) {
    console.error("Failed to fetch VM list", error);
    if (error?.response?.status === 401) {
      errorMessage.value = "Authentication required.";
      emit("auth-required", "Authentication required.");
    } else {
      errorMessage.value = error?.response?.data?.detail || error?.message || "Failed to fetch VM list.";
    }
    throw error;
  } finally {
    loading.value = false;
  }
};

const refresh = () => {
  const promise = fetchVms();
  promise.catch(() => {
    // handled via auth-required emit or error message
  });
  expanded.value.forEach((vmName) => {
    loadDetails(vmName, true).catch(() => {
      /* errors handled per-call */
    });
  });
  return promise;
};

const toggle = (vmName) => {
  if (isExpanded(vmName)) {
    expanded.value = expanded.value.filter((name) => name !== vmName);
  } else {
    expanded.value = [...expanded.value, vmName];
    if (!detailsMap[vmName] && !detailsLoading[vmName]) {
      loadDetails(vmName).catch(() => {
        /* handled in loadDetails */
      });
    }
  }
};

const isExpanded = (vmName) => expanded.value.includes(vmName);

const loadDetails = async (vmName, force = false) => {
  if (!force && detailsMap[vmName]) {
    return;
  }
  detailsLoading[vmName] = true;
  detailsError[vmName] = "";
  try {
    const { data } = await api.get(`/v1/vms/${encodeURIComponent(vmName)}/details`);
    detailsMap[vmName] = data;
  } catch (error) {
    console.error(`Failed to fetch details for VM ${vmName}`, error);
    if (error?.response?.status === 401) {
      emit("auth-required", "Authentication required.");
      detailsError[vmName] = "Authentication required.";
    } else {
      detailsError[vmName] =
        error?.response?.data?.detail || error?.message || "Failed to load VM details. Click refresh to retry.";
    }
    throw error;
  } finally {
    detailsLoading[vmName] = false;
  }
};

const retryDetails = (vmName) => {
  loadDetails(vmName, true).catch(() => {
    /* handled in loadDetails */
  });
};

const filteredVms = computed(() => {
  if (!searchTerm.value) {
    return vms.value;
  }
  const needle = searchTerm.value.toLowerCase();
  return vms.value.filter((vm) => {
    const inName = vm.name?.toLowerCase().includes(needle);
    const inStatus = vm.status?.toLowerCase().includes(needle);
    const details = detailsMap[vm.name];
    const vlan = (details?.payload?.vlan ?? "").toString().toLowerCase();
    const ip = (details?.payload?.nic?.ip ?? "").toLowerCase();
    return inName || inStatus || vlan.includes(needle) || ip.includes(needle);
  });
});

onMounted(() => {
  fetchVms().catch(() => {
    /* auth handled upstream */
  });
  intervalHandle = window.setInterval(() => {
    fetchVms().catch(() => {
      /* auth handled upstream */
    });
  }, 30000);
});

onBeforeUnmount(() => {
  if (intervalHandle) {
    window.clearInterval(intervalHandle);
  }
});

defineExpose({ refresh });
</script>

<style scoped>
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
