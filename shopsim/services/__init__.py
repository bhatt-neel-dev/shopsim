"""The three shop services."""
from __future__ import annotations

import importlib

import httpx


def create(name: str, transports: dict[str, httpx.AsyncBaseTransport] | None = None):
    from shopsim.common.service import create_service_app

    module = importlib.import_module(f"shopsim.services.{name}")
    return create_service_app(name, module, transports)
