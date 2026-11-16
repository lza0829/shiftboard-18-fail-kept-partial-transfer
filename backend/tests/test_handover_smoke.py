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


def _fail_mid_transfer(client, monkeypatch, on_call=2):
    started_keys = _inflight(client, 1)
    hid = _start(client)

    real = svc._transfer_batch
    calls = {"n": 0}

    def flaky(conn, batch_id, from_id, to_id):
        calls["n"] += 1
        if calls["n"] == on_call:
            raise svc.TransferError(f"模拟第 {on_call} 批过户写入失败")
        return real(conn, batch_id, from_id, to_id)

    monkeypatch.setattr(svc, "_transfer_batch", flaky)
    body = client.post(f"/api/handovers/{hid}/complete").json()
    assert body["status"] == "失败"
    return hid, started_keys, body


def test_failed_handover_persists_rollback_proof(client, monkeypatch):
    """每次失败都落持久化回滚证明：归属指纹、失败后交班方在途、锁释放结果。"""
    hid, started_keys, body = _fail_mid_transfer(client, monkeypatch)

    proof = client.get(f"/api/handovers/{hid}/rollback-proof")
    assert proof.status_code == 200, proof.text
    p = proof.json()
    assert p["有回滚证明"] is True
    assert p["handover_status"] == "失败"
    assert p["发起前归属指纹"].startswith("sha256:")
    assert sorted(p["回滚后交班方在途批次"]) == started_keys
    assert p["回滚后交班方在途与发起清单一致"] is True
    assert p["锁释放结果"]["进行中锁"] == "已释放"
    assert p["锁释放结果"]["双方可再次交班"] is True
    assert p["回滚证明"]["回滚方式"].startswith("同一事务整单回滚")

    # 详情接口与对账报告同源：指纹与在途集合一致
    detail = client.get(f"/api/handovers/{hid}").json()
    assert detail["rollback_proof"]["发起前归属指纹"] == p["发起前归属指纹"]
    assert detail["report"]["detail"]["锁释放结果"] == p["锁释放结果"]
    reports = {r["handover_id"]: r for r in client.get("/api/reconcile").json()}
    assert reports[hid]["detail"] == p["回滚证明"]

    # 证明里的指纹确实对应发起时快照，可由 handover_items 重算核对
    with connect() as conn:
        entries = [
            {
                "batch_id": r["batch_id"],
                "biz_key": r["biz_key"],
                "owner_shift_id": r["snapshot_owner_shift_id"],
                "status": r["snapshot_status"],
            }
            for r in conn.execute(
                """
                SELECT hi.batch_id, b.biz_key, hi.snapshot_owner_shift_id, hi.snapshot_status
                FROM handover_items hi JOIN batches b ON b.id = hi.batch_id
                WHERE hi.handover_id = ?
                """,
                (hid,),
            )
        ]
    assert svc._ownership_fingerprint(entries) == p["发起前归属指纹"]


def test_rollback_proof_endpoint_readonly_and_missing(client, monkeypatch):
    """只读接口：无失败单返回“无证明”，且不会改动任何数据；不存在单返回 404。"""
    hid, _, _ = _fail_mid_transfer(client, monkeypatch)
    before = client.get(f"/api/handovers/{hid}").json()
    with connect() as conn:
        batches_before = [
            dict(r) for r in conn.execute("SELECT * FROM batches ORDER BY id")
        ]

    first = client.get(f"/api/handovers/{hid}/rollback-proof").json()
    second = client.get(f"/api/handovers/{hid}/rollback-proof").json()
    assert first == second

    with connect() as conn:
        batches_after = [
            dict(r) for r in conn.execute("SELECT * FROM batches ORDER BY id")
        ]
    assert batches_after == batches_before
    assert client.get(f"/api/handovers/{hid}").json() == before

    ok_id = _start(client)
    assert client.post(f"/api/handovers/{ok_id}/complete").json()["status"] == "已完成"
    no_proof = client.get(f"/api/handovers/{ok_id}/rollback-proof").json()
    assert no_proof["有回滚证明"] is False
    assert no_proof["回滚证明"] is None

    missing = client.get("/api/handovers/99999/rollback-proof")
    assert missing.status_code == 404


def test_failure_before_transfer_reports_no_write_rollback(client):
    """过户前校验失败：归属未动，报告须写明“未发生归属变更，无需回滚”。"""
    hid = _start(client)
    with connect() as conn:
        conn.execute("UPDATE batches SET status = '已结清' WHERE id = 3")
        conn.commit()

    assert client.post(f"/api/handovers/{hid}/complete").json()["status"] == "失败"
    p = client.get(f"/api/handovers/{hid}/rollback-proof").json()
    assert p["回滚证明"]["事务回滚已执行"] is False
    assert p["回滚证明"]["回滚方式"].startswith("未发生归属变更")
    assert p["锁释放结果"]["进行中锁"] == "已释放"
    # 归属确实在原位
    with connect() as conn:
        bad = conn.execute(
            "SELECT COUNT(*) AS c FROM batches WHERE owner_shift_id <> "
            "(SELECT snapshot_owner_shift_id FROM handover_items "
            " WHERE handover_id = ? AND batch_id = batches.id) "
            "AND id IN (SELECT batch_id FROM handover_items WHERE handover_id = ?)",
            (hid, hid),
        ).fetchone()["c"]
    assert bad == 0


def test_failure_report_fallback_still_rolls_back_and_releases_lock(client, monkeypatch):
    """完整失败报告写不进去时，兜底落档仍须零半截归属、释放锁，并给出最小证明。"""
    started_keys = _inflight(client, 1)
    hid = _start(client)

    real_transfer = svc._transfer_batch
    calls = {"n": 0}

    def flaky(conn, batch_id, from_id, to_id):
        calls["n"] += 1
        if calls["n"] == 2:
            raise svc.TransferError("模拟第 2 批过户写入失败")
        return real_transfer(conn, batch_id, from_id, to_id)

    def boom(conn, h, reason, *, rolled_back):
        raise RuntimeError("reconcile insert failed")

    monkeypatch.setattr(svc, "_transfer_batch", flaky)
    monkeypatch.setattr(svc, "_record_failure", boom)

    resp = client.post(f"/api/handovers/{hid}/complete")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "失败"
    assert "模拟第 2 批过户写入失败" in body["reason"]
    # savepoint 撤销了第 1 批归属
    assert _inflight(client, 1) == started_keys
    assert _inflight(client, 2) == []

    assert _shift_status(client, 1) == "值班中"
    assert _shift_status(client, 2) == "空闲"
    assert client.get("/api/dashboard").json()["进行中交班"] == 0

    p = client.get(f"/api/handovers/{hid}/rollback-proof").json()
    assert p["有回滚证明"] is True
    assert "兜底" in p["回滚证明"]["落档说明"]
    assert p["锁释放结果"]["双方可再次交班"] is True
    assert sorted(p["回滚后交班方在途批次"]) == started_keys

    retry = client.post("/api/handovers/start", json={"from_shift_id": 1, "to_shift_id": 2})
    assert retry.status_code == 200, retry.text


