"""A deliberately GET-only HTTP client (AUD-632).

This class has no method for anything but a safe read. Adding a mutating
call (`post`, `put`, `patch`, `delete`) here is the one change that would let
a tool trigger a job, spend the owner's provider budget, or write data — so
there is nothing to call by accident, and none to remove by discipline.
"""

from __future__ import annotations

import httpx


class AudrClientError(RuntimeError):
    """Raised when the audr API answers an error response."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"audr API returned {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class AudrClient:
    """Bearer-token client over a single audr instance's read routes."""

    def __init__(self, base_url: str, token: str, *, timeout_seconds: float = 10.0) -> None:
        self._base_url = base_url
        self._token = token
        self._timeout_seconds = timeout_seconds

    async def get(
        self,
        path: str,
        *,
        params: dict[str, str | int | bool] | None = None,
        accept: str = "application/json",
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": accept}
        async with httpx.AsyncClient(
            base_url=self._base_url, timeout=self._timeout_seconds
        ) as http_client:
            response = await http_client.get(path, params=params, headers=headers)
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise AudrClientError(response.status_code, response.text)
        return response
