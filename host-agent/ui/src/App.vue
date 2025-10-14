<template>
  <div>
    <header class="fc-navbar">
      <div class="container-fluid navbar-content">
        <div>
          <h1>CloudStack Firecracker Host</h1>
          <p class="timestamp">Last refreshed: {{ lastUpdatedLabel }}</p>
        </div>
        <div class="auth-controls">
          <button v-if="isAuthenticated" class="btn btn-default btn-sm" @click="handleLogout">
            Sign out
          </button>
        </div>
      </div>
    </header>

    <main class="container-fluid" style="max-width: 1200px">
      <VmList ref="vmListRef" @update-timestamp="updateTimestamp" @auth-required="handleAuthRequired" />
    </main>

    <LoginModal
      :visible="showLogin"
      :error="loginError"
      @submit="handleLoginSubmit"
      @cancel="handleLoginCancel"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import VmList from "./components/VmList.vue";
import LoginModal from "./components/LoginModal.vue";
import { clearAuth, loadAuthFromStorage, setBasicAuth } from "./services/apiClient";

const lastUpdated = ref(null);
const showLogin = ref(false);
const loginError = ref("");
const isAuthenticated = ref(false);
const vmListRef = ref(null);

const updateTimestamp = (value) => {
  lastUpdated.value = value;
};

const setAuthState = (state) => {
  isAuthenticated.value = state;
};

const handleAuthRequired = (message) => {
  clearAuth();
  setAuthState(false);
  if (typeof message === "string" && message.trim()) {
    loginError.value = message;
  } else if (!loginError.value) {
    loginError.value = "Authentication is required to access this host.";
  }
  showLogin.value = true;
};

const handleLoginSubmit = async ({ username, password }) => {
  try {
    loginError.value = "";
    setBasicAuth(username, password);
    await vmListRef.value?.refresh();
    setAuthState(true);
    showLogin.value = false;
  } catch (error) {
    clearAuth();
    setAuthState(false);
    if (error?.response?.status === 401) {
      loginError.value = "Invalid credentials. Please try again.";
    } else {
      loginError.value = error?.message || "Failed to authenticate. Please try again.";
    }
    showLogin.value = true;
  }
};

const handleLoginCancel = () => {
  showLogin.value = false;
  loginError.value = "";
};

const handleLogout = () => {
  clearAuth();
  setAuthState(false);
  showLogin.value = true;
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
