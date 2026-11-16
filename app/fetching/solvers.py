"""Solver interface (submit, poll, token) with one adapter per provider.

All four providers speak the same JSON task protocol (``createTask`` / ``getTaskResult`` /
``getBalance`` with ``clientKey``); they differ in the API address and the Turnstile task
type. CapSolver and 2Captcha formats follow their public API documentation; the
Anti-Captcha and CapMonster Cloud adapters use the same protocol and are marked
unverified in the README. The key is never logged and never put into a message.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

PROVIDERS = {
    # provider: (default endpoint, turnstile task type)
    "capsolver": ("https://api.capsolver.com", "AntiTurnstileTaskProxyLess"),
    "two_captcha": ("https://api.2captcha.com", "TurnstileTaskProxyless"),
    "anti_captcha": ("https://api.anti-captcha.com", "TurnstileTaskProxyless"),
    "capmonster_cloud": ("https://api.capmonster.cloud", "TurnstileTask"),
}
AUTH_ERROR_CODES = {
    "ERROR_KEY_DOES_NOT_EXIST",
    "ERROR_KEY_DENIED_ACCESS",
    "ERROR_INVALID_KEY",
    "ERROR_WRONG_USER_KEY",
    "ERROR_ZERO_BALANCE",
    "ERROR_ACCOUNT_SUSPENDED",
    "ERROR_IP_NOT_ALLOWED",
}


class SolverError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code  # solver_auth_failed | solver_unreachable | solver_provider_error
        self.message = message


@dataclass
class Balance:
    amount: float | None
    currency: str | None


class Solver:
    def __init__(self, provider: str, api_key: str, endpoint: str | None = None, timeout: float = 20.0) -> None:
        if provider not in PROVIDERS:
            raise SolverError("solver_provider_error", f"no adapter for {provider}")
        self.provider = provider
        self._key = api_key
        default, self.task_type = PROVIDERS[provider]
        self.endpoint = (endpoint or default).rstrip("/")
        self.timeout = timeout

    def __repr__(self) -> str:  # never show the key
        return f"Solver({self.provider}, {self.endpoint})"

    async def _post(self, path: str, body: dict) -> dict:
        payload = {"clientKey": self._key, **body}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(f"{self.endpoint}{path}", json=payload)
        except (httpx.HTTPError, OSError) as exc:
            raise SolverError("solver_unreachable", f"{self.provider} API unreachable ({type(exc).__name__})") from None
        try:
            data = resp.json()
        except ValueError:
            raise SolverError("solver_provider_error", f"{self.provider} answered HTTP {resp.status_code} without JSON") from None
        if not isinstance(data, dict):
            raise SolverError("solver_provider_error", f"{self.provider} answered an unexpected body")
        if data.get("errorId"):
            code = str(data.get("errorCode") or "")
            desc = str(data.get("errorDescription") or code or "error")
            desc = desc.replace(self._key, "***")[:300]
            if code in AUTH_ERROR_CODES or "key" in code.lower():
                raise SolverError("solver_auth_failed", f"{self.provider}: {desc}")
            raise SolverError("solver_provider_error", f"{self.provider}: {desc}")
        return data

    async def balance(self) -> Balance:
        data = await self._post("/getBalance", {})
        amount = data.get("balance")
        try:
            amount = float(amount) if amount is not None else None
        except (TypeError, ValueError):
            amount = None
        return Balance(amount, "USD" if amount is not None else None)

    async def solve_turnstile(self, website_url: str, website_key: str, max_wait: float = 120.0) -> str:
        task = {"type": self.task_type, "websiteURL": website_url, "websiteKey": website_key or ""}
        created = await self._post("/createTask", {"task": task})
        task_id = created.get("taskId")
        if not task_id:
            # some providers answer synchronously
            token = (created.get("solution") or {}).get("token")
            if token:
                return str(token)
            raise SolverError("solver_provider_error", f"{self.provider}: no taskId")
        deadline = time.monotonic() + max_wait
        await asyncio.sleep(2)
        while time.monotonic() < deadline:
            result = await self._post("/getTaskResult", {"taskId": task_id})
            status = result.get("status")
            if status == "ready":
                token = (result.get("solution") or {}).get("token")
                if not token:
                    raise SolverError("solver_provider_error", f"{self.provider}: ready without a token")
                return str(token)
            await asyncio.sleep(2)
        raise SolverError("solver_provider_error", f"{self.provider}: no solution within {int(max_wait)} s")


def from_profile(solver_cfg: dict | None) -> Solver | None:
    if not solver_cfg or not solver_cfg.get("api_key"):
        return None
    return Solver(solver_cfg["provider"], solver_cfg["api_key"], solver_cfg.get("endpoint"))
