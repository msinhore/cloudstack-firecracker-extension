import axios from "axios";

const STORAGE_KEY = "fc-host-ui-basic-auth";

const api = axios.create({
  baseURL: "/",
  headers: {
    Accept: "application/json",
  },
});

function setBasicAuth(username, password) {
  if (!username || !password) {
    clearAuth();
    return;
  }
  const token = btoa(`${username}:${password}`);
  const headerValue = `Basic ${token}`;
  api.defaults.headers.common.Authorization = headerValue;
  try {
    sessionStorage.setItem(STORAGE_KEY, headerValue);
  } catch (err) {
    console.warn("Unable to persist auth token in sessionStorage", err);
  }
}

function loadAuthFromStorage() {
  try {
    const stored = sessionStorage.getItem(STORAGE_KEY);
    if (stored) {
      api.defaults.headers.common.Authorization = stored;
      return true;
    }
  } catch (err) {
    console.warn("Unable to restore auth token from sessionStorage", err);
  }
  return false;
}

function clearAuth() {
  delete api.defaults.headers.common.Authorization;
  try {
    sessionStorage.removeItem(STORAGE_KEY);
  } catch (err) {
    console.warn("Unable to clear auth token from sessionStorage", err);
  }
}

export { api, setBasicAuth, loadAuthFromStorage, clearAuth };
