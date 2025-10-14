<template>
  <div>
    <header class="fc-navbar">
      <div class="container-fluid">
        <h1>CloudStack Firecracker Host</h1>
        <p class="timestamp">Last refreshed: {{ lastUpdatedLabel }}</p>
      </div>
    </header>

    <main class="container-fluid" style="max-width: 1200px">
      <VmList @update-timestamp="updateTimestamp" />
    </main>
  </div>
</template>

<script setup>
import { computed, ref } from "vue";
import VmList from "./components/VmList.vue";

const lastUpdated = ref(null);
const updateTimestamp = (value) => {
  lastUpdated.value = value;
};

const lastUpdatedLabel = computed(() => {
  if (!lastUpdated.value) {
    return "never";
  }
  return new Date(lastUpdated.value).toLocaleString();
});
</script>
