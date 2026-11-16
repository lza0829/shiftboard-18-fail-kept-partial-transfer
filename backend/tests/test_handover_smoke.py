from pathlib import Path
import os
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
os.environ["DATA_DIR"] = str(ROOT / "backend" / "data_test")
os.environ["FIXTURES_DIR"] = str(ROOT / "fixtures")

db_path = Path(os.environ["DATA_DIR"]) / "app.db"

from app import seed  # noqa: E402
from app.db import connect  # noqa: E402
from app.main import app  # noqa: E402
from app.services import handover as svc  # noqa: E402


@pytest.fixture()
def client():
    if db_path.exists():
        db_path.unlink()
    seed.init_db()
    with TestClient(app) as c:
        yield c


def _inflight(client, shift_id: int) -> list[str]:
    rows = client.get("/api/batches", params={"owner_shift_id": shift_id}).json()
    return sorted(r["biz_key"] for r in rows if r["status"] == "在途")


def _shift_status(client, shift_id: int) -> str:
    shifts = client.get("/api/shifts").json()
    return next(s["status"] for s in shifts if s["id"] == shift_id)


def _start(client, from_id=1, to_id=2) -> int:
    r = client.post(
        "/api/handovers/start", json={"from_shift_id": from_id, "to_shift_id": to_id}
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["项目"] == "交班台"


def test_handover_and_reconcile(client):
    assert _inflight(client, 1)
    started_keys = _inflight(client, 1)

    hid = _start(client)

    # 并行交班应被拒绝
    again = client.post("/api/handovers/start", json={"from_shift_id": 1, "to_shift_id": 3})
    assert again.status_code == 409

    done = client.post(f"/api/handovers/{hid}/complete")
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["status"] == "已完成"
    assert body["batch_count"] == len(started_keys)
    assert body["reason"] == ""

    # 清单内全部批次归接班方，交班方不再有在途
    assert _inflight(client, 1) == []
    assert set(started_keys) <= set(_inflight(client, 2))
    assert _shift_status(client, 1) == "已交班"
    assert _shift_status(client, 2) == "值班中"

    reports = client.get("/api/reconcile").json()
    assert reports
    report = reports[0]
    assert report["一致"] is True
    assert report["结论"] == "成功"
    assert report["handover_id"] == hid
    assert report["before_count"] == len(started_keys)
    assert report["after_from_count"] == 0
    assert report["detail"]["交班方无残留在途"] is True

    # 已完成单不可重复完成 / 重复完成不得再次过户
    again_done = client.post(f"/api/handovers/{hid}/complete")
    assert again_done.status_code == 400
    assert set(_inflight(client, 2)) >= set(started_keys)


def test_failure_before_transfer_rolls_back_and_frees_lock(client):
    started_keys = _inflight(client, 1)
    hid = _start(client)

    # 发起后、完成前，清单内有一个批次被外部结清：不得“跳过仍成功”
    with connect() as conn:
        conn.execute("UPDATE batches SET status = '已结清' WHERE id = 3")
        conn.commit()

    resp = client.post(f"/api/handovers/{hid}/complete")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "失败"
    assert "状态已变为" in body["reason"]

    # 归属全部回到发起前：包括未发生变化的批次，库内逐批核对
    assert _inflight(client, 1) == [k for k in started_keys if k != "B20261115-03"]
    with connect() as conn:
        owners = {
            r["biz_key"]: r["owner_shift_id"]
            for r in conn.execute(
                "SELECT biz_key, owner_shift_id FROM batches WHERE biz_key <> 'B20261113-02'"
            )
        }
        snap = {
            r["biz_key"]: r["snapshot_owner_shift_id"]
            for r in conn.execute(
                """
                SELECT b.biz_key, hi.snapshot_owner_shift_id
                FROM handover_items hi JOIN batches b ON b.id = hi.batch_id
                WHERE hi.handover_id = ?
                """,
                (hid,),
            )
        }
    assert owners == snap

    # 失败单不占进行中锁，双方班次恢复为可再次交班
    assert body["report"]["一致"] is False
    assert body["report"]["结论"] == "失败已回滚"
    assert _shift_status(client, 1) == "值班中"
    assert _shift_status(client, 2) == "空闲"
    assert client.get("/api/dashboard").json()["进行中交班"] == 0

    retry = client.post("/api/handovers/start", json={"from_shift_id": 1, "to_shift_id": 2})
    assert retry.status_code == 200, retry.text


def test_failure_mid_transfer_rolls_back_partial_ownership(client, monkeypatch):
    started_keys = _inflight(client, 1)
    hid = _start(client)

    real = svc._transfer_batch
    calls = {"n": 0}

    def flaky(conn, batch_id, from_id, to_id):
        calls["n"] += 1
        if calls["n"] == 2:
            raise svc.TransferError("模拟第 2 批过户写入失败")
        return real(conn, batch_id, from_id, to_id)

    monkeypatch.setattr(svc, "_transfer_batch", flaky)

    resp = client.post(f"/api/handovers/{hid}/complete")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "失败"
    assert "模拟第 2 批过户写入失败" in body["reason"]
    assert calls["n"] == 2

    # 第 1 批的 UPDATE 必须被 savepoint 撤销：库内归属 == 发起快照
    assert _inflight(client, 1) == started_keys
    assert _inflight(client, 2) == []
    with connect() as conn:
        mismatched = conn.execute(
            """
            SELECT b.biz_key, b.owner_shift_id, hi.snapshot_owner_shift_id
            FROM handover_items hi JOIN batches b ON b.id = hi.batch_id
            WHERE hi.handover_id = ?
              AND (b.owner_shift_id <> hi.snapshot_owner_shift_id
                   OR b.status <> hi.snapshot_status)
            """,
            (hid,),
        ).fetchall()
    assert mismatched == []

    report = body["report"]
    assert report["一致"] is False
    assert report["after_from_count"] == len(started_keys)
    assert sorted(report["detail"]["回滚后交班方在途批次"]) == started_keys
    assert report["detail"]["回滚后交班方在途与发起清单一致"] is True
    assert report["detail"]["归属已回滚到发起快照"] is True
    assert client.get("/api/dashboard").json()["进行中交班"] == 0


def test_success_report_is_written_transactionally(client, monkeypatch):
    """对账报告写失败时，整单（含已过户归属）必须一起回滚，不得出现半截状态。"""
    started_keys = _inflight(client, 1)
    hid = _start(client)

    def boom(*args, **kwargs):
        raise RuntimeError("reconcile insert failed")

    monkeypatch.setattr(svc, "_record_success", boom)

    resp = client.post(f"/api/handovers/{hid}/complete")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "失败"
    assert "reconcile insert failed" in body["reason"]

    assert _inflight(client, 1) == started_keys
    assert _inflight(client, 2) == []
    assert _shift_status(client, 1) == "值班中"
    assert _shift_status(client, 2) == "空闲"
    # 失败报告仍在（走兜底落档路径），且报告数字来自库内
    reports = client.get("/api/reconcile").json()
    assert reports[0]["handover_id"] == hid
    assert reports[0]["一致"] is False


def test_reports_distinguish_success_and_failure(client, monkeypatch):
    ok_id = _start(client)
    assert client.post(f"/api/handovers/{ok_id}/complete").json()["status"] == "已完成"

    second = client.post("/api/handovers/start", json={"from_shift_id": 2, "to_shift_id": 3})
    assert second.status_code == 200, second.text
    fail_id = second.json()["id"]

    real = svc._transfer_batch
    state = {"n": 0}

    def flaky(conn, batch_id, from_id, to_id):
        state["n"] += 1
        if state["n"] == 1:
            raise svc.TransferError("模拟写入冲突")
        return real(conn, batch_id, from_id, to_id)

    monkeypatch.setattr(svc, "_transfer_batch", flaky)
    assert client.post(f"/api/handovers/{fail_id}/complete").json()["status"] == "失败"

    reports = {r["handover_id"]: r for r in client.get("/api/reconcile").json()}
    assert reports[ok_id]["一致"] is True
    assert reports[ok_id]["结论"] == "成功"
    assert reports[fail_id]["一致"] is False
    assert reports[fail_id]["结论"] == "失败已回滚"
    assert reports[fail_id]["handover_status"] == "失败"
    assert reports[fail_id]["detail"]["失败原因"] == "模拟写入冲突"

    # 列表接口能区分失败单
    logs = {h["id"]: h for h in client.get("/api/handovers").json()}
    assert logs[fail_id]["status"] == "失败"
    assert logs[fail_id]["reason"]


def test_start_snapshot_survives_and_blocks_extra_batches(client):
    """发起后交班方新增在途批次：完成必须整单失败，不能只过户清单内部分。"""
    started_keys = _inflight(client, 1)
    hid = _start(client)

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO batches(biz_key, title, owner_shift_id, status, note)
            VALUES ('B20261115-99', '发起后新增', 1, '在途', '')
            """
        )
        conn.commit()

    body = client.post(f"/api/handovers/{hid}/complete").json()
    assert body["status"] == "失败"
    assert "清单外新在途批次" in body["reason"]
    assert _inflight(client, 1) == sorted(started_keys + ["B20261115-99"])
    assert _inflight(client, 2) == []


def test_concurrent_start_only_one_wins(client):
    """并发发起：写锁 + 部分唯一索引保证全仓只有一单进行中。"""
    from concurrent.futures import ThreadPoolExecutor

    def fire(target):
        return client.post(
            "/api/handovers/start", json={"from_shift_id": 1, "to_shift_id": target}
        ).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = list(pool.map(fire, [2, 3, 2, 3]))

    assert codes.count(200) == 1, codes
    assert codes.count(409) == 3, codes
    active = [h for h in client.get("/api/handovers").json() if h["status"] == "进行中"]
    assert len(active) == 1
