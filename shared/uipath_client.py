"""UiPath Orchestrator API client for queue and job management."""

from __future__ import annotations

from typing import Any

import httpx

from cubiczan_resilience import resilient

from shared.config import settings

# Per-request HTTP timeout for all UiPath Orchestrator calls.
HTTP_TIMEOUT = 30.0

# Some UiPath Cloud edge/WAF tiers (notably staging) reject requests carrying a
# non-browser User-Agent with HTTP 403 (Cloudflare "error code: 1010"), before
# they ever reach Identity/Orchestrator. httpx's default UA ("python-httpx/...")
# trips this, so send a browser-like UA on every client — token and OData calls.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class UiPathClient:
    def __init__(self) -> None:
        self.base_url = settings.uipath.cloud_url
        self.org = settings.uipath.org_name
        self.tenant = settings.uipath.tenant_name
        self._token: str | None = None

    @resilient(timeout=HTTP_TIMEOUT, max_attempts=3)
    async def _ensure_token(self) -> str:
        if self._token:
            return self._token
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT, headers={"User-Agent": BROWSER_USER_AGENT}
        ) as client:
            resp = await client.post(
                f"{self.base_url}/identity_/connect/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": settings.uipath.client_id,
                    "client_secret": settings.uipath.client_secret,
                    "scope": "OR.Queues OR.Jobs OR.Folders OR.Execution",
                },
            )
            resp.raise_for_status()
            self._token = resp.json()["access_token"]
        return self._token

    def _api_url(self, path: str) -> str:
        return (
            f"{self.base_url}/{self.org}/{self.tenant}"
            f"/orchestrator_/odata{path}"
        )

    async def _headers(self) -> dict[str, str]:
        token = await self._ensure_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-UIPATH-OrganizationUnitId": settings.uipath.orchestrator_folder,
        }

    @resilient(timeout=HTTP_TIMEOUT, max_attempts=3)
    async def add_queue_item(
        self, queue_name: str, data: dict[str, Any], priority: str = "Normal"
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT, headers={"User-Agent": BROWSER_USER_AGENT}
        ) as client:
            resp = await client.post(
                self._api_url("/Queues/UiPathODataSvc.AddQueueItem"),
                headers=await self._headers(),
                json={
                    "itemData": {
                        "Name": queue_name,
                        "Priority": priority,
                        "SpecificContent": data,
                    }
                },
            )
            resp.raise_for_status()
            return resp.json()

    @resilient(timeout=HTTP_TIMEOUT, max_attempts=3)
    async def get_queue_items(
        self, queue_name: str, status: str = "New", top: int = 50
    ) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT, headers={"User-Agent": BROWSER_USER_AGENT}
        ) as client:
            resp = await client.get(
                self._api_url("/QueueItems"),
                headers=await self._headers(),
                params={
                    "$filter": f"QueueDefinitionId/Name eq '{queue_name}' and Status eq '{status}'",
                    "$top": top,
                    "$orderby": "CreationTime desc",
                },
            )
            resp.raise_for_status()
            return resp.json().get("value", [])

    @resilient(timeout=HTTP_TIMEOUT, max_attempts=3)
    async def start_job(
        self, process_key: str, input_args: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT, headers={"User-Agent": BROWSER_USER_AGENT}
        ) as client:
            body: dict[str, Any] = {
                "startInfo": {
                    "ReleaseKey": process_key,
                    "Strategy": "ModernJobsCount",
                    "JobsCount": 1,
                }
            }
            if input_args:
                body["startInfo"]["InputArguments"] = str(input_args)
            resp = await client.post(
                self._api_url("/Jobs/UiPath.Server.Configuration.OData.StartJobs"),
                headers=await self._headers(),
                json=body,
            )
            resp.raise_for_status()
            return resp.json()

    @resilient(timeout=HTTP_TIMEOUT, max_attempts=3)
    async def get_job_status(self, job_id: int) -> dict[str, Any]:
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT, headers={"User-Agent": BROWSER_USER_AGENT}
        ) as client:
            resp = await client.get(
                self._api_url(f"/Jobs({job_id})"),
                headers=await self._headers(),
            )
            resp.raise_for_status()
            return resp.json()


uipath = UiPathClient()
