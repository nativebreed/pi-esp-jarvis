"""Talks to the small helper service that runs on the Pi itself (outside Docker)."""
from __future__ import annotations

import logging

import httpx

from . import config

log = logging.getLogger("pihelper")


class PiHelper:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=15)
        self._actions: dict | None = None

    @property
    def configured(self) -> bool:
        return bool(config.PI_HELPER_TOKEN)

    async def _call(self, method: str, path: str, **kw):
        r = await self._http.request(method, f"{config.PI_HELPER_URL}{path}",
                                     headers={"X-Token": config.PI_HELPER_TOKEN}, **kw)
        r.raise_for_status()
        return r.json()

    async def status(self) -> dict:
        return await self._call("GET", "/status")

    async def catalog(self, refresh: bool = False) -> dict:
        if self._actions is None or refresh:
            self._actions = await self._call("GET", "/actions")
        return self._actions

    async def run(self, name: str, arg: str = "") -> dict:
        return await self._call("POST", "/run", json={"name": name, "arg": arg})
