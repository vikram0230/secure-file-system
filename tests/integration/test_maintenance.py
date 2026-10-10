import os

from sqlalchemy import select, text

from api_service.db.models import File, StorageNode
from api_service.services.maintenance import Maintenance
from api_service.worker import _try_lock
from tests.integration.helpers import (
    corrupt,
    make_link,
    set_node_status,
    shard_locations,
    upload,
    uploaded_id,
)


def _maintenance(app) -> Maintenance:
    return Maintenance(app.state.sessionmaker, app.state.nodes, app.state.settings)


def _node_status(db_session, address: str) -> str:
    db_session.expire_all()
    status = db_session.scalar(select(StorageNode.status).where(StorageNode.address == address))
    db_session.commit()
    return status


async def test_health_check_marks_nodes_down_and_back_up(app, fleet, db_session, settings):
    worker = _maintenance(app)
    target = fleet.addresses[0]
    fleet.down = {target}

    for _ in range(settings.node_failure_threshold - 1):
        await worker.check_node_health()
        assert _node_status(db_session, target) == "healthy"
    await worker.check_node_health()
    assert _node_status(db_session, target) == "unreachable"

    fleet.down = set()
    await worker.check_node_health()
    assert _node_status(db_session, target) == "healthy"


async def test_repair_moves_lost_shard_to_a_spare_node(app, client, alice, fleet, db_session):
    data = os.urandom(30_000)
    file_id = await uploaded_id(client, alice, data)
    before = shard_locations(db_session, file_id)
    lost_shard, lost_address, lost_index = before[0]

    fleet.down = {lost_address}
    set_node_status(db_session, [lost_address], "unreachable", "now() - interval '1 hour'")
    repaired = await _maintenance(app).repair_lost_shards()

    assert repaired == 1
    after = shard_locations(db_session, file_id)
    new_shard, new_address, new_index = after[0]
    assert new_index == lost_index
    assert new_address not in {address for _, address, _ in before}
    assert new_shard != lost_shard
    assert fleet.shard_path(new_address, new_shard).exists()

    # With the original node still down, two more outages are survivable again.
    fleet.down |= {after[1][1], after[2][1]}
    resp = await client.get(await make_link(client, alice, file_id))
    assert resp.status_code == 200
    assert resp.content == data


async def test_repair_waits_out_the_grace_period(app, client, alice, fleet, db_session):
    file_id = await uploaded_id(client, alice)
    address = shard_locations(db_session, file_id)[0][1]
    set_node_status(db_session, [address], "unreachable")  # last seen just now

    worker = Maintenance(
        app.state.sessionmaker,
        app.state.nodes,
        app.state.settings.model_copy(update={"repair_grace_seconds": 3600}),
    )
    assert await worker.repair_lost_shards() == 0


async def test_scrub_finds_and_repairs_corruption(app, client, alice, fleet, db_session):
    data = os.urandom(12_000)
    file_id = await uploaded_id(client, alice, data)
    shard_id, address, index = shard_locations(db_session, file_id)[3]
    corrupt(fleet.shard_path(address, shard_id))

    repaired = await _maintenance(app).scrub()

    assert repaired == 1
    fixed = {i: (s, a) for s, a, i in shard_locations(db_session, file_id)}[index]
    assert fixed[0] != shard_id
    assert not fleet.shard_path(address, shard_id).exists()  # corrupt copy cleaned up


async def test_gc_removes_stale_uploads_once_nodes_return(app, client, alice, fleet, db_session):
    set_node_status(db_session, [fleet.addresses[6]], "unreachable")
    fleet.down = {fleet.addresses[0]}
    assert (await upload(client, alice)).status_code == 502

    db_session.execute(text("UPDATE files SET uploaded_at = now() - interval '1 day'"))
    db_session.commit()
    worker = _maintenance(app)

    await worker.gc_stale_uploads()  # node-0 still down: row must survive
    db_session.expire_all()
    assert db_session.scalar(select(File.id)) is not None
    db_session.commit()

    fleet.down = set()
    await worker.gc_stale_uploads()
    db_session.expire_all()
    assert db_session.scalar(select(File.id)) is None
    assert not any(any(p.iterdir()) for p in fleet.dirs.values() if p.exists())


async def test_gc_finishes_deletes_that_hit_a_down_node(app, client, alice, fleet, db_session):
    file_id = await uploaded_id(client, alice)
    locations = shard_locations(db_session, file_id)
    fleet.down = {locations[0][1]}

    assert (await client.delete(f"/files/{file_id}", headers=alice)).status_code == 204
    assert len(shard_locations(db_session, file_id)) == 1

    fleet.down = set()
    await _maintenance(app).gc_deleted_files()
    assert shard_locations(db_session, file_id) == []
    assert not fleet.shard_path(locations[0][1], locations[0][0]).exists()


async def test_only_one_worker_holds_the_lock(app):
    engine = app.state.sessionmaker.kw["bind"]
    async with engine.connect() as a, engine.connect() as b:
        first = await a.execution_options(isolation_level="AUTOCOMMIT")
        second = await b.execution_options(isolation_level="AUTOCOMMIT")
        assert await _try_lock(first) is True
        assert await _try_lock(second) is False
        await first.execute(text("SELECT pg_advisory_unlock_all()"))
        assert await _try_lock(second) is True
        await second.execute(text("SELECT pg_advisory_unlock_all()"))
