import pytest

from penr.traffic.index import (
    get_exchange,
    get_exchange_bodies,
    init_traffic_db,
    list_exchanges,
    meta_by_ids,
    recent_exchange_ids,
    record_exchange,
)


@pytest.mark.asyncio
async def test_traffic_roundtrip(tmp_path, monkeypatch):
    from penr.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("ANTIFOX_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()

    await init_traffic_db()
    eid = await record_exchange(
        method="GET",
        url="https://example.com/",
        status=200,
        resp_body="ok",
        task_id="1",
    )
    assert eid > 0
    ids = await recent_exchange_ids(limit=1, task_id="1")
    assert ids == [eid]
    row = await get_exchange(eid)
    assert row is not None
    assert row["url"] == "https://example.com/"
    assert row["host"] == "example.com"


@pytest.mark.asyncio
async def test_traffic_pagination_and_host_filter(tmp_path, monkeypatch):
    from penr.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("ANTIFOX_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()

    await init_traffic_db()
    await record_exchange(method="GET", url="https://a.example/x", status=200, task_id="9")
    await record_exchange(method="POST", url="https://b.example/y", status=404, task_id="9")
    await record_exchange(method="GET", url="https://a.example/z", status=500, task_id="8")

    page0 = await list_exchanges(host="a.example", page=0, size=10)
    assert page0["total"] == 2
    assert all(e["host"] == "a.example" for e in page0["exchanges"])
    assert page0["status_buckets"].get(2) == 1
    assert page0["status_buckets"].get(5) == 1

    by_task = await list_exchanges(task_id="9", page=0, size=10)
    assert by_task["total"] == 2

    bodies = await get_exchange_bodies(by_task["exchanges"][0]["id"])
    assert bodies is not None
    assert "req" in bodies and "resp" in bodies

    metas = await meta_by_ids([e["id"] for e in by_task["exchanges"]])
    assert len(metas) == 2
