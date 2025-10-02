#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE/2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict

import typer
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse

from api import register_routes
from cli import CLICommands

# Import modular components
from config import ConfigManager
from orchestration import VMLifecycle
from utils.filesystem import set_agent_defaults

# Global variables
logger = logging.getLogger("fc-agent")
logger.setLevel(logging.INFO)
_DEF_HANDLER_SET = False
# Readiness logging interval (seconds) for INFO-level progress during start
READINESS_LOG_INTERVAL = float(os.environ.get("FC_AGENT_READINESS_LOG", "5"))
READINESS_TIMEOUT_DEFAULT = int(os.environ.get("FC_AGENT_READY_TIMEOUT", "10"))
READINESS_ENDPOINTS = ["/version", "/machine-config"]
# FC_AGENT_READY_POLICY accepts: api | socket | pid
READY_POLICY = os.environ.get("FC_AGENT_READY_POLICY", "pid").strip().lower() or "pid"
# Global configuration
AGENT_DEFAULTS: Dict[str, Any] = {}
AGENT_CFG: Dict[str, Any] = {}
IS_API_MODE = True
# Initialize FastAPI app
app = FastAPI(title="Firecracker Agent", version="1.0.0")


def _apply_logging_from_cfg(cfg: Dict[str, Any]) -> None:
    """Apply logging configuration from agent config."""
    global _DEF_HANDLER_SET
    if _DEF_HANDLER_SET:
        return
    log_cfg = cfg.get("logging", {})
    if log_cfg:
        level = log_cfg.get("level", "INFO").upper()
        try:
            logger.setLevel(getattr(logging, level))
        except AttributeError:
            logger.setLevel(logging.INFO)
        # Add console handler if not present
        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        _DEF_HANDLER_SET = True


def root_ok() -> Dict[str, Any]:
    """Root endpoint handler."""
    return {"status": "ok", "message": "Firecracker Agent is running", "version": "1.0.0"}


def v1_config_effective() -> Dict[str, Any]:
    """Get effective configuration."""
    return {"status": "success", "config": AGENT_DEFAULTS}


# FastAPI event handlers
@app.on_event("startup")
async def startup_event():
    """Initialize agent on startup."""
    global AGENT_DEFAULTS, AGENT_CFG
    logger.info("Starting Firecracker Agent...")
    # Load configuration
    config_manager = ConfigManager({})
    AGENT_CFG = config_manager.load_agent_config()
    AGENT_DEFAULTS = AGENT_CFG.get("defaults", {})
    config_manager.agent_defaults = AGENT_DEFAULTS
    set_agent_defaults(AGENT_DEFAULTS)
    
    logger.info("Configuration loaded successfully")
    logger.info("AGENT_CFG keys: %s", list(AGENT_CFG.keys()))
    logger.info("AGENT_DEFAULTS keys: %s", list(AGENT_DEFAULTS.keys()))
    logger.info("AGENT_DEFAULTS: %s", AGENT_DEFAULTS)
    
    # Apply logging configuration
    _apply_logging_from_cfg(AGENT_CFG)
    # Register API routes with loaded configuration
    register_routes(app, AGENT_DEFAULTS)
    # Initialize VM lifecycle for recovery
    vm_lifecycle = VMLifecycle(AGENT_DEFAULTS)
    vm_lifecycle.startup_vm_recovery()
    logger.info("Firecracker Agent started successfully")


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    """Log incoming requests immediately upon receipt."""
    try:
        logger.info("%s %s", request.method, request.url.path)
    except Exception:
        pass
    return await call_next(request)


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown."""
    logger.info("Shutting down Firecracker Agent (VMs left running)…")
    # Do not stop running VMs on agent restart; networking recovery will run on startup
    logger.info("Firecracker Agent shut down")


# Root endpoint
@app.get("/", include_in_schema=False)
def root():
    return root_ok()


# Middleware for error handling
@app.middleware("http")
async def error_handling_middleware(request: Request, call_next):
    """Global error handling middleware."""
    try:
        response = await call_next(request)
        return response
    except Exception as e:
        logger.exception("Unhandled error in request: %s", e)
        return JSONResponse(status_code=500, content={"error": "Internal server error", "detail": str(e)})


# Exception handlers
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle request validation errors."""
    logger.error("Validation error: %s", exc)
    return JSONResponse(status_code=422, content={"error": "Validation error", "detail": exc.errors()})


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Handle HTTP exceptions."""
    logger.error("HTTP error: %s", exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle general exceptions."""
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(status_code=500, content={"error": "Internal server error"})


# Register API routes
# register_routes(app, AGENT_DEFAULTS)  # Moved to startup_event
# CLI interface
cli = typer.Typer()


@cli.command()
def prepare(spec_file: Path):
    """Prepare storage (volume) only."""
    cli_commands = CLICommands(AGENT_DEFAULTS)
    cli_commands.prepare(spec_file)


@cli.command()
def create(spec_file: Path, timeout: int = 30):
    """Create and start a VM."""
    cli_commands = CLICommands(AGENT_DEFAULTS)
    cli_commands.create(spec_file, timeout)


@cli.command()
def start(spec_file: Path, timeout: int = 30):
    """Start an existing VM."""
    cli_commands = CLICommands(AGENT_DEFAULTS)
    cli_commands.start(spec_file, timeout)


@cli.command()
def stop(spec_file: Path, timeout: int = 30):
    """Stop a running VM."""
    cli_commands = CLICommands(AGENT_DEFAULTS)
    cli_commands.stop(spec_file, timeout)


@cli.command()
def reboot(spec_file: Path, timeout: int = 30):
    """Reboot a VM."""
    cli_commands = CLICommands(AGENT_DEFAULTS)
    cli_commands.reboot(spec_file, timeout)


@cli.command()
def delete(spec_file: Path):
    """Delete a VM."""
    cli_commands = CLICommands(AGENT_DEFAULTS)
    cli_commands.delete(spec_file)


@cli.command()
def vm_status(spec_file: Path):
    """Get VM status."""
    cli_commands = CLICommands(AGENT_DEFAULTS)
    cli_commands.vm_status(spec_file)


@cli.command()
def net_prepare_cmd(spec_file: Path):
    """Prepare network for VM."""
    cli_commands = CLICommands(AGENT_DEFAULTS)
    cli_commands.net_prepare_cmd(spec_file)


@cli.command()
def net_teardown_cmd(spec_file: Path):
    """Teardown network for VM."""
    cli_commands = CLICommands(AGENT_DEFAULTS)
    cli_commands.net_teardown_cmd(spec_file)


@cli.command()
def write_config_cmd(spec_file: Path):
    """Write VM configuration."""
    cli_commands = CLICommands(AGENT_DEFAULTS)
    cli_commands.write_config_cmd(spec_file)


@cli.command()
def recover(spec_file: Path):
    """Recover networking for an existing VM."""
    cli_commands = CLICommands(AGENT_DEFAULTS)
    cli_commands.recover(spec_file)


def main():
    """Main entry point."""
    global AGENT_CFG, AGENT_DEFAULTS
    # Load configuration first
    config_manager = ConfigManager({})
    AGENT_CFG = config_manager.load_agent_config()
    AGENT_DEFAULTS = AGENT_CFG.get("defaults", {})
    config_manager.agent_defaults = AGENT_DEFAULTS
    set_agent_defaults(AGENT_DEFAULTS)
    # Run API by default; set FC_AGENT_MODE=cli to use the local CLI instead
    mode = os.environ.get("FC_AGENT_MODE", "api").lower()
    if mode == "cli":
        cli()
    else:
        try:
            cfg = AGENT_CFG
            uvicorn.run(app, host=cfg["bind_host"], port=cfg["bind_port"], reload=False)
        except ModuleNotFoundError:
            print(
                "uvicorn is not installed. Install it or run CLI mode:"
                "\n  FC_AGENT_MODE=cli python3 firecracker-agent.py\n",
                flush=True,
            )


if __name__ == "__main__":
    main()
