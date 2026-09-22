"""HTTP client for the storage-node fleet (private network only)."""

import hashlib

import httpx


class NodeError(Exception):
    """A node was unreachable, timed out, or answered with an unexpected status."""


class ShardMissingError(NodeError):
    """The node is up but does not hold the requested shard."""


class ShardCorruptError(NodeError):
    """The node is up but the shard failed its at-rest checksum."""


class NodeClient:
    def __init__(
        self,
        token: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            transport=transport,
            headers={"X-Node-Token": token},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def put_shard(self, address: str, shard_id: str, data: bytes) -> None:
        response = await self._request(
            "PUT",
            address,
            f"/shards/{shard_id}",
            content=data,
            headers={"X-Checksum-Sha256": hashlib.sha256(data).hexdigest()},
        )
        if response.status_code != 201:
            raise NodeError(f"{address}: PUT {shard_id} -> {response.status_code}")

    async def get_shard(self, address: str, shard_id: str) -> bytes:
        response = await self._request("GET", address, f"/shards/{shard_id}")
        if response.status_code == 404:
            raise ShardMissingError(f"{address}: {shard_id} not found")
        if response.headers.get("X-Shard-Status") == "corrupt":
            raise ShardCorruptError(f"{address}: {shard_id} corrupt at rest")
        if response.status_code != 200:
            raise NodeError(f"{address}: GET {shard_id} -> {response.status_code}")
        return response.content

    async def delete_shard(self, address: str, shard_id: str) -> None:
        """Idempotent: a shard that is already gone counts as deleted."""
        response = await self._request("DELETE", address, f"/shards/{shard_id}")
        if response.status_code not in (204, 404):
            raise NodeError(f"{address}: DELETE {shard_id} -> {response.status_code}")

    async def is_healthy(self, address: str) -> bool:
        try:
            response = await self._request("GET", address, "/healthz")
        except NodeError:
            return False
        return response.status_code == 200

    async def _request(self, method: str, address: str, path: str, **kwargs) -> httpx.Response:
        try:
            return await self._client.request(method, f"http://{address}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise NodeError(f"{address}: {method} {path} failed: {exc!r}") from exc
