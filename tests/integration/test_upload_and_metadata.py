import os
import uuid

from sqlalchemy import select, text

from api_service.db.models import File
from tests.integration.helpers import (
    audit_events,
    set_node_status,
    shard_locations,
    upload,
    uploaded_id,
)


async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


async def test_requests_without_valid_key_are_rejected(client, alice):
    for headers in ({}, {"Authorization": "Bearer sfs_wrong"}, {"Authorization": "Basic x"}):
        assert (await client.get("/files", headers=headers)).status_code == 401
        assert (await upload(client, headers)).status_code == 401


async def test_upload_spreads_k_plus_m_shards_across_distinct_nodes(client, alice, db_session):
    data = os.urandom(10_000)
    resp = await upload(client, alice, data)

    assert resp.status_code == 201
    body = resp.json()
    assert body["size_bytes"] == len(data)
    assert body["filename"] == "report.pdf"
    assert body["status"] == "available"
    assert body["availability"] == {
        "state": "healthy",
        "usable_shards": 6,
        "total_shards": 6,
        "required_shards": 4,
    }

    locations = shard_locations(db_session, body["id"])
    assert [index for _, _, index in locations] == list(range(6))
    assert len({address for _, address, _ in locations}) == 6
    assert len(audit_events(db_session, "file_uploaded")) == 1


async def test_upload_validation(client, alice, settings):
    assert (await upload(client, alice, b"")).status_code == 400
    oversized = b"x" * (settings.max_upload_bytes + 1)
    assert (await upload(client, alice, oversized)).status_code == 413
    missing_field = await client.post("/files", headers=alice, files={"other": ("a", b"x")})
    assert missing_field.status_code == 422


async def test_body_far_beyond_limit_rejected_before_parsing(client, alice, settings):
    resp = await client.post(
        "/files",
        headers={**alice, "Content-Type": "multipart/form-data; boundary=x"},
        content=b"x" * (settings.max_upload_bytes + 128 * 1024),
    )
    assert resp.status_code == 413


async def test_small_endpoints_have_a_small_body_limit(client, alice):
    file_id = await uploaded_id(client, alice)
    resp = await client.post(
        f"/files/{file_id}/links",
        headers={**alice, "Content-Type": "application/json"},
        content=b'{"ttl_seconds": 60, "pad": "' + b"x" * 100_000 + b'"}',
    )
    assert resp.status_code == 413


async def test_filename_and_content_type_are_sanitized(client, alice):
    resp = await upload(
        client,
        alice,
        filename="../../etc/pass\u202ewd.txt",  # path components + bidi override
        content_type="text/html; charset=utf-8",
    )
    body = resp.json()
    assert body["filename"] == "passwd.txt"
    assert body["content_type"] == "text/html"

    resp = await upload(client, alice, filename="..", content_type="not a mime type")
    assert resp.json()["filename"] == "file"
    assert resp.json()["content_type"] == "application/octet-stream"


async def test_upload_refused_without_enough_healthy_nodes(client, alice, fleet, db_session):
    set_node_status(db_session, fleet.addresses[:2], "unreachable")  # 5 healthy < 6
    resp = await upload(client, alice)
    assert resp.status_code == 503
    assert db_session.scalar(select(File.id)) is None


async def test_failed_shard_write_rolls_back_cleanly(client, alice, fleet, db_session):
    fleet.fail_writes = set(fleet.addresses)
    resp = await upload(client, alice)

    assert resp.status_code == 502
    db_session.expire_all()
    assert db_session.scalar(select(File.id)) is None
    assert not any(p.exists() and any(p.iterdir()) for p in fleet.dirs.values())


async def test_unreachable_node_during_upload_leaves_row_for_gc(client, alice, fleet, db_session):
    # Every node receives a shard (7 nodes, 6 shards, so pin by taking one down in the DB).
    set_node_status(db_session, [fleet.addresses[6]], "unreachable")
    fleet.down = {fleet.addresses[0]}  # DB still thinks it is healthy

    resp = await upload(client, alice)

    assert resp.status_code == 502
    db_session.expire_all()
    file = db_session.scalar(select(File))
    assert file is not None and file.status == "uploading"
    db_session.commit()
    assert (await client.get("/files", headers=alice)).json()["items"] == []


async def test_list_and_get_are_owner_scoped(client, alice, bob):
    alice_file = await uploaded_id(client, alice)
    bob_file = await uploaded_id(client, bob)

    listed = (await client.get("/files", headers=alice)).json()
    assert [item["id"] for item in listed["items"]] == [alice_file]

    assert (await client.get(f"/files/{alice_file}", headers=alice)).status_code == 200
    for method, path, kwargs in (
        ("GET", f"/files/{bob_file}", {}),
        ("POST", f"/files/{bob_file}/links", {"json": {"ttl_seconds": 60}}),
        ("DELETE", f"/files/{bob_file}", {}),
        ("GET", f"/files/{uuid.uuid4()}", {}),
    ):
        resp = await client.request(method, path, headers=alice, **kwargs)
        assert resp.status_code == 404, (method, path)


async def test_list_pagination(client, alice):
    ids = [await uploaded_id(client, alice, os.urandom(100)) for _ in range(3)]

    first = (await client.get("/files?limit=2", headers=alice)).json()
    second = (await client.get("/files?limit=2&offset=2", headers=alice)).json()

    seen = [i["id"] for i in first["items"]] + [i["id"] for i in second["items"]]
    assert sorted(seen) == sorted(ids)
    assert (await client.get("/files?limit=0", headers=alice)).status_code == 422
    assert (await client.get("/files?limit=101", headers=alice)).status_code == 422


async def test_availability_reflects_node_health(client, alice, db_session):
    file_id = await uploaded_id(client, alice)
    addresses = [address for _, address, _ in shard_locations(db_session, file_id)]

    set_node_status(db_session, addresses[:2], "unreachable")
    degraded = (await client.get(f"/files/{file_id}", headers=alice)).json()["availability"]
    assert degraded == {
        "state": "degraded",
        "usable_shards": 4,
        "total_shards": 6,
        "required_shards": 4,
    }

    set_node_status(db_session, addresses[2:3], "unreachable")
    unavailable = (await client.get(f"/files/{file_id}", headers=alice)).json()["availability"]
    assert unavailable["state"] == "unavailable"


async def test_transfer_limit_sheds_load(client, alice, app):
    import asyncio

    app.state.transfer_slots = asyncio.Semaphore(0)
    resp = await upload(client, alice)
    assert resp.status_code == 503
    assert resp.headers["Retry-After"]


async def test_database_outage_fails_closed(settings, fleet, alice):
    import httpx

    from api_service.main import create_app

    broken = settings.model_copy(
        update={"database_url": "postgresql+psycopg://sfs@127.0.0.1:1/sfs_test"}
    )
    app = create_app(broken, node_transport=fleet)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t") as client,
    ):
        resp = await client.get("/files", headers=alice)
    assert resp.status_code == 503


async def test_delete_revokes_and_removes_shards(client, alice, fleet, db_session):
    file_id = await uploaded_id(client, alice)
    locations = shard_locations(db_session, file_id)

    assert (await client.delete(f"/files/{file_id}", headers=alice)).status_code == 204

    assert (await client.get(f"/files/{file_id}", headers=alice)).status_code == 404
    assert (await client.delete(f"/files/{file_id}", headers=alice)).status_code == 404
    assert shard_locations(db_session, file_id) == []
    assert not any(fleet.shard_path(a, s).exists() for s, a, _ in locations)
    assert len(audit_events(db_session, "file_deleted")) == 1
    db_session.expire_all()
    assert db_session.execute(text("SELECT status FROM files")).scalar() == "deleted"
