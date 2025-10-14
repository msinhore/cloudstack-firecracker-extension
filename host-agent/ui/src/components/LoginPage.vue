<template>
  <div class="login-page">
    <div class="login-card">
      <h1 class="login-title">CloudStack Firecracker Host</h1>
      <p class="login-subtitle">Sign in to view the host inventory.</p>

      <div v-if="error" class="login-alert" role="alert">
        {{ error }}
      </div>

      <form class="login-form" @submit.prevent="submit">
        <div class="input-wrapper">
          <span class="input-icon glyphicon glyphicon-user" aria-hidden="true"></span>
          <input
            id="login-username"
            v-model.trim="username"
            class="login-input"
            type="text"
            name="username"
            placeholder="Username"
            autocomplete="username"
            :disabled="loading"
            required
            autofocus
          />
        </div>

        <div class="input-wrapper">
          <span class="input-icon glyphicon glyphicon-lock" aria-hidden="true"></span>
          <input
            id="login-password"
            v-model="password"
            class="login-input"
            :type="passwordVisible ? 'text' : 'password'"
            name="password"
            placeholder="Password"
            autocomplete="current-password"
            :disabled="loading"
            required
          />
          <button
            type="button"
            class="toggle-visibility"
            :aria-label="passwordVisible ? 'Hide password' : 'Show password'"
            :disabled="loading"
            @click="togglePasswordVisibility"
          >
            <span
              class="glyphicon"
              :class="passwordVisible ? 'glyphicon-eye-close' : 'glyphicon-eye-open'"
              aria-hidden="true"
            ></span>
          </button>
        </div>

        <button type="submit" class="login-button" :disabled="loading">
          <span v-if="loading" class="glyphicon glyphicon-refresh spinning" aria-hidden="true"></span>
          {{ loading ? "Signing in..." : "Login" }}
        </button>
      </form>
    </div>
  </div>
</template>

<script setup>
import { ref, watch } from "vue";

const props = defineProps({
  error: {
    type: String,
    default: "",
  },
  loading: {
    type: Boolean,
    default: false,
  },
});

const emit = defineEmits(["submit"]);

const username = ref("");
const password = ref("");
const passwordVisible = ref(false);

watch(
  () => props.error,
  (value) => {
    if (value) {
      password.value = "";
      passwordVisible.value = false;
    }
  }
);

const togglePasswordVisibility = () => {
  passwordVisible.value = !passwordVisible.value;
};

const submit = () => {
  if (props.loading) {
    return;
  }
  emit("submit", { username: username.value, password: password.value });
};
</script>

<style scoped>
.login-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: #ffffff;
  padding: 24px;
}

.login-card {
  width: 100%;
  max-width: 360px;
  background: #ffffff;
  border-radius: 12px;
  box-shadow: 0 14px 35px rgba(31, 48, 76, 0.12);
  padding: 32px 36px;
  text-align: center;
}

.login-title {
  font-size: 20px;
  font-weight: 500;
  margin: 0;
  color: #1a2a42;
}

.login-subtitle {
  margin: 12px 0 24px;
  color: #708198;
  font-size: 14px;
}

.login-alert {
  background-color: #ffe8e8;
  border: 1px solid #f4b4b4;
  border-radius: 8px;
  color: #b51f24;
  padding: 10px 12px;
  margin-bottom: 20px;
  text-align: left;
  font-size: 13px;
}

.login-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.input-wrapper {
  position: relative;
}

.login-input {
  width: 100%;
  height: 44px;
  border: 1px solid #d9dfe7;
  border-radius: 8px;
  padding: 0 42px;
  font-size: 14px;
  color: #1a2a42;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

.login-input::placeholder {
  color: #a4b0c3;
}

.login-input:focus {
  outline: none;
  border-color: #1a73e8;
  box-shadow: 0 0 0 3px rgba(26, 115, 232, 0.15);
}

.login-input:disabled {
  background: #f4f6fb;
  cursor: not-allowed;
}

.input-icon {
  position: absolute;
  top: 50%;
  left: 14px;
  transform: translateY(-50%);
  font-size: 16px;
  color: #a4b0c3;
}

.toggle-visibility {
  position: absolute;
  top: 50%;
  right: 10px;
  transform: translateY(-50%);
  border: none;
  background: transparent;
  padding: 4px;
  color: #7f8a9d;
  cursor: pointer;
}

.toggle-visibility:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.toggle-visibility:focus {
  outline: none;
}

.login-button {
  height: 46px;
  border: none;
  border-radius: 8px;
  background: #1a73e8;
  color: #ffffff;
  font-size: 15px;
  font-weight: 500;
  transition: background 0.2s ease, transform 0.2s ease;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
}

.login-button:not(:disabled):hover {
  background: #1765cc;
}

.login-button:not(:disabled):active {
  transform: translateY(1px);
}

.login-button:disabled {
  background: #9dbcf5;
  cursor: not-allowed;
}

.spinning {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}
</style>
