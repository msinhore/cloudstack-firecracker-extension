#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""API routes module for Firecracker Agent."""
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI

from models import SpecRequest
from .handlers import APIHandlers


def register_routes(
    app: FastAPI,
    agent_defaults: Dict[str, Any],
    auth_dependency: Optional[Any] = None,
) -> None:
    """Register all API routes with the FastAPI application."""
    handlers = APIHandlers(agent_defaults)
    deps = [Depends(auth_dependency)] if auth_dependency else []
    protected = {"dependencies": deps} if deps else {}

    # Health and info endpoints
    @app.get("/healthz")
    def healthz():
        return handlers.healthz()

    @app.get("/v1", **protected)
    def v1_index():
        return handlers.v1_index()

    @app.get("/v1/version", **protected)
    def v1_version():
        return handlers.v1_version()

    @app.get("/v1/health", **protected)
    def v1_health_alias():
        return handlers.v1_health_alias()

    @app.get("/v1/config/effective", **protected)
    def v1_config_effective():
        return handlers.v1_config_effective()

    # VM management endpoints
    @app.post("/v1/vms", status_code=201, **protected)
    def create_vm(req: SpecRequest):
        return handlers.api_create(req)

    @app.get("/v1/vms", **protected)
    def v1_list_vms():
        return handlers.v1_list_vms()

    @app.get("/v1/vms/{vm_name}/status", **protected)
    def v1_vm_status_by_name(vm_name: str):
        return handlers.v1_vm_status_by_name(vm_name)

    @app.get("/v1/vms/{vm_name}/details", **protected)
    def v1_vm_details_by_name(vm_name: str):
        return handlers.v1_vm_details_by_name(vm_name)

    @app.post("/v1/vms/{vm_name}/stop", **protected)
    def v1_vm_stop_by_name(vm_name: str):
        return handlers.v1_vm_stop_by_name(vm_name)

    @app.post("/v1/vms/{vm_name}/start", **protected)
    def v1_vm_start_by_name(vm_name: str, req: SpecRequest):
        return handlers.v1_vm_start_by_name(vm_name, req)

    @app.delete("/v1/vms/{vm_name}", **protected)
    def v1_vm_delete_by_name(vm_name: str):
        return handlers.v1_vm_delete_by_name(vm_name)

    @app.post("/v1/vms/{vm_name}/reboot", **protected)
    def v1_vm_reboot_by_name(vm_name: str):
        return handlers.v1_vm_reboot_by_name(vm_name)

    @app.post("/v1/vms/{vm_name}/recover", **protected)
    def v1_vm_recover_by_name(vm_name: str, req: Optional[SpecRequest] = None):
        return handlers.v1_vm_recover_by_name(vm_name, req)

    # System management endpoints
    @app.post("/v1/graceful-shutdown", **protected)
    def v1_graceful_shutdown():
        return handlers.v1_graceful_shutdown()

    @app.post("/v1/save-states", **protected)
    def v1_save_states():
        return handlers.v1_save_states()

    @app.get("/v1/saved-states", **protected)
    def v1_get_saved_states():
        return handlers.v1_get_saved_states()

    @app.post("/v1/recover-all", **protected)
    def v1_recover_all_vms():
        return handlers.v1_recover_all_vms()

    # Network configuration endpoints (retained for recovery tooling)
    @app.get("/v1/network-config/{vm_name}", **protected)
    def v1_get_network_config(vm_name: str):
        return handlers.v1_get_network_config(vm_name)

    @app.post("/v1/network-config/{vm_name}/apply", **protected)
    def v1_apply_network_config(vm_name: str):
        return handlers.v1_apply_network_config(vm_name)

    @app.delete("/v1/network-config/{vm_name}", **protected)
    def v1_delete_network_config(vm_name: str):
        return handlers.v1_delete_network_config(vm_name)
