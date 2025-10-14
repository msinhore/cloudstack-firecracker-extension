<template>
  <div v-if="visible" class="auth-backdrop">
    <div class="auth-dialog panel panel-default">
      <div class="panel-heading">
        <h3 class="panel-title">Authentication Required</h3>
      </div>
      <div class="panel-body">
        <p>Please enter your credentials to access the host dashboard.</p>
        <div v-if="error" class="alert alert-danger" role="alert">
          {{ error }}
        </div>
        <form @submit.prevent="submit">
          <div class="form-group">
            <label for="login-username">Username</label>
            <input
              id="login-username"
              v-model.trim="username"
              type="text"
              class="form-control"
              autocomplete="username"
              required
              autofocus
            />
          </div>
          <div class="form-group">
            <label for="login-password">Password</label>
            <input
              id="login-password"
              v-model="password"
              type="password"
              class="form-control"
              autocomplete="current-password"
              required
            />
          </div>
          <div class="auth-actions">
            <button type="submit" class="btn btn-primary">Sign in</button>
            <button type="button" class="btn btn-default" @click="$emit('cancel')">Cancel</button>
          </div>
        </form>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, watch } from "vue";

const props = defineProps({
  visible: {
    type: Boolean,
    default: false,
  },
  error: {
    type: String,
    default: "",
  },
});

const emit = defineEmits(["submit", "cancel"]);

const username = ref("");
const password = ref("");

watch(
  () => props.visible,
  (newVal) => {
    if (!newVal) {
      username.value = "";
      password.value = "";
    }
  }
);

const submit = () => {
  emit("submit", { username: username.value, password: password.value });
};
</script>
