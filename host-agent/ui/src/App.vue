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
        <div class="container-fluid navbar-content">
          <div>
            <h1>CloudStack Firecracker Host</h1>
            <p class="timestamp">Last refreshed: {{ lastUpdatedLabel }}</p>
          </div>
          <div class="auth-controls">
            <button class="btn btn-default btn-sm" @click="handleLogout">Sign out</button>
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

const updateTimestamp = (value) => {
  lastUpdated.value = value;
};

const setAuthState = (state) => {
  isAuthenticated.value = state;
  if (!state) {
    lastUpdated.value = null;
  }
};

const handleAuthRequired = (message) => {
  clearAuth();
  setAuthState(false);
  loginLoading.value = false;
  if (typeof message === "string" && message.trim()) {
    loginError.value = message;
  } else if (!loginError.value) {
    loginError.value = "Authentication is required to access this host.";
  }
};

const handleLoginSubmit = async ({ username, password }) => {
  loginLoading.value = true;
  loginError.value = "";
  try {
    setBasicAuth(username, password);
    await api.get("/v1/vms");
    setAuthState(true);
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

onMounted(() => {
  const restored = loadAuthFromStorage();
  setAuthState(restored);
  if (!restored) {
    loginError.value = "";
  }
});
</script>
