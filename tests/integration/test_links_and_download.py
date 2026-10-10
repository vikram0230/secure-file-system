import os
import secrets
import time
import uuid

import httpx
import pytest

from api_service.main import create_app
from api_service.services import signing
from tests.integration.helpers import (
    audit_events,
    corrupt,
    make_link,
    shard_locations,
    uploaded_id,
)


def _with_params(url: str, **overrides) -> str:
    parsed = httpx.URL(url)
    return str(parsed.copy_merge_params(overrides))


async def test_link_then_download_round_trip(client, alice, db_session):
    data = os.urandom(50_000)
    file_id = await uploaded_id(client, alice, data)

    resp = await client.post(f"/files/{file_id}/links", headers=alice, json={"ttl_seconds": 120})
    assert resp.status_code == 201
    link = resp.json()
    assert set(httpx.URL(link["url"]).params) == {"exp", "v", "kid", "sig"}

    download = await client.get(link["url"])  # no API key: the link is the credential
    assert download.status_code == 200
    assert download.content == data
    assert download.headers["content-type"] == "application/octet-stream"
    assert download.headers["content-disposition"] == (
        "attachment; filename=\"report.pdf\"; filename*=UTF-8''report.pdf"
    )
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["cache-control"] == "no-store"
    assert download.headers["referrer-policy"] == "no-referrer"
    assert "sandbox" in download.headers["content-security-policy"]

    [issued] = audit_events(db_session, "link_generated")
    assert issued.ttl_seconds == 120 and str(issued.file_id) == file_id
    assert len(audit_events(db_session, "download_success")) == 1


@pytest.mark.parametrize("ttl", [59, 7 * 24 * 3600 + 1, 0, -5, "60", 60.0, True, None])
async def test_link_ttl_must_be_a_bounded_integer(client, alice, ttl):
    file_id = await uploaded_id(client, alice)
    resp = await client.post(f"/files/{file_id}/links", headers=alice, json={"ttl_seconds": ttl})
    assert resp.status_code == 422


async def test_link_rejects_unknown_fields(client, alice):
    file_id = await uploaded_id(client, alice)
    resp = await client.post(
        f"/files/{file_id}/links", headers=alice, json={"ttl_seconds": 60, "admin": True}
    )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "tamper",
    [
        {"sig": "A" * 43},
        {"exp": "9999999999"},
        {"v": "2"},
        {"kid": "unknown"},
        {"exp": "12abc"},
        {"sig": ""},
    ],
)
async def test_tampered_links_are_denied_and_audited(client, alice, db_session, tamper):
    file_id = await uploaded_id(client, alice)
    url = await make_link(client, alice, file_id)

    resp = await client.get(_with_params(url, **tamper))

    assert resp.status_code == 403
    [denied] = audit_events(db_session, "download_denied")
    assert str(denied.file_id) == file_id


async def test_missing_parameters_are_denied(client, alice):
    file_id = await uploaded_id(client, alice)
    assert (await client.get(f"/download/{file_id}")).status_code == 403


async def test_expired_link_is_denied(client, alice, settings):
    file_id = await uploaded_id(client, alice)
    past = int(time.time()) - 1
    sig = signing.sign(settings.secret_key, settings.secret_key_id, uuid.UUID(file_id), past, 1)
    resp = await client.get(
        f"/download/{file_id}",
        params={"exp": past, "v": 1, "kid": settings.secret_key_id, "sig": sig},
    )
    assert resp.status_code == 403


async def test_denied_request_for_unknown_file_audits_without_file(client, db_session):
    resp = await client.get(f"/download/{uuid.uuid4()}?exp=1&v=1&kid=k1&sig=x")
    assert resp.status_code == 403
    [denied] = audit_events(db_session, "download_denied")
    assert denied.file_id is None

    assert (await client.get("/download/not-a-uuid?exp=1&v=1&kid=k1&sig=x")).status_code == 403


async def test_deleted_file_link_stops_working(client, alice):
    file_id = await uploaded_id(client, alice)
    url = await make_link(client, alice, file_id)
    assert (await client.delete(f"/files/{file_id}", headers=alice)).status_code == 204
    assert (await client.get(url)).status_code == 404


async def test_link_survives_restart(settings, fleet, alice, client):
    data = os.urandom(3_000)
    file_id = await uploaded_id(client, alice, data)
    url = await make_link(client, alice, file_id)

    restarted = create_app(settings.model_copy(), node_transport=fleet)
    async with (
        restarted.router.lifespan_context(restarted),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(restarted), base_url="http://testserver"
        ) as fresh,
    ):
        resp = await fresh.get(url)
    assert resp.status_code == 200
    assert resp.content == data


async def test_links_signed_with_a_retired_key_still_verify(settings, fleet, alice, client):
    data = b"rotation test" * 10
    file_id = await uploaded_id(client, alice, data)
    old_url = await make_link(client, alice, file_id)

    rotated = settings.model_copy(
        update={
            "secret_key": secrets.token_hex(32),
            "secret_key_id": "k2",
            "previous_secret_keys": f"{settings.secret_key_id}:{settings.secret_key}",
        }
    )
    app = create_app(rotated, node_transport=fleet)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://testserver") as c,
    ):
        assert (await c.get(old_url)).content == data
        new_link = await c.post(f"/files/{file_id}/links", headers=alice, json={"ttl_seconds": 60})
        assert httpx.URL(new_link.json()["url"]).params["kid"] == "k2"


async def test_download_tolerates_m_node_outages(client, alice, fleet, db_session):
    data = os.urandom(20_000)
    file_id = await uploaded_id(client, alice, data)
    url = await make_link(client, alice, file_id)
    addresses = [address for _, address, _ in shard_locations(db_session, file_id)]

    fleet.down = set(addresses[:2])  # both happen to be data shards: forces a parity decode
    resp = await client.get(url)
    assert resp.status_code == 200
    assert resp.content == data

    fleet.down = set(addresses[:3])
    assert (await client.get(url)).status_code == 503


async def test_download_skips_corrupt_shards(client, alice, fleet, db_session):
    data = os.urandom(20_000)
    file_id = await uploaded_id(client, alice, data)
    url = await make_link(client, alice, file_id)
    locations = shard_locations(db_session, file_id)

    shard_id, address, _ = locations[0]
    corrupt(fleet.shard_path(address, shard_id))
    fleet.down = {locations[1][1]}

    resp = await client.get(url)
    assert resp.status_code == 200
    assert resp.content == data


async def test_download_rate_limit(settings, fleet, alice, client):
    file_id = await uploaded_id(client, alice)
    url = await make_link(client, alice, file_id)
    limited = create_app(settings.model_copy(update={"download_rate_per_minute": 2}), fleet)
    async with (
        limited.router.lifespan_context(limited),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(limited), base_url="http://testserver"
        ) as c,
    ):
        statuses = [(await c.get(url)).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]
