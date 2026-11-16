from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from fastapi import HTTPException

from app.db import connect, rows_to_dicts


class TransferError(Exception):
    """批次过户校验失败：整单必须回滚，不允许部分过户后仍标成功。"""


# 失败闭环方案（二选一，本仓固定采用第一种）：
#   同一事务整单回滚 —— 过户与成功落档包在 SAVEPOINT transfer 内，
#   任一步骤异常即 ROLLBACK TO SAVEPOINT transfer 撤销全部归属变更，
#   再在同一事务里落失败单、恢复班次、写回滚证明并提交。
# 因此库里只可能出现两种终态：全部过户的「已完成」，或归属原样 + 「失败」。
# 不存在半截正式归属，也不存在失败单占「进行中」锁。
ROLLBACK_MODE = "同一事务整单回滚（SAVEPOINT transfer）"
ROLLBACK_MODE_NO_WRITE = "未发生归属变更，无需回滚（过户前校验失败）"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _restored_shift_status(prev: str | None, fallback: str) -> str:
    """失败回滚班次状态：恢复发起时快照；快照缺失时用兜底值。"""
    if prev in {"值班中", "空闲", "已交班"}:
        return prev
    return fallback


def _parse_report(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    d = dict(row)
    d["detail"] = json.loads(d.pop("detail_json"))
    d["一致"] = bool(d.pop("consistent"))
    d["结论"] = d.get("outcome") or ("成功" if d["一致"] else "失败已回滚")
    return d


def list_shifts() -> list[dict]:
    with connect() as conn:
        return rows_to_dicts(conn.execute("SELECT * FROM shifts ORDER BY id").fetchall())


def list_batches(owner_shift_id: int | None = None) -> list[dict]:
    with connect() as conn:
        if owner_shift_id is None:
            rows = conn.execute(
                """
                SELECT b.*, s.name AS owner_shift_name
                FROM batches b
                LEFT JOIN shifts s ON s.id = b.owner_shift_id
                ORDER BY b.id
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT b.*, s.name AS owner_shift_name
                FROM batches b
                LEFT JOIN shifts s ON s.id = b.owner_shift_id
                WHERE b.owner_shift_id = ?
                ORDER BY b.id
                """,
                (owner_shift_id,),
            ).fetchall()
        return rows_to_dicts(rows)


def list_handovers() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT h.*,
                   fs.name AS from_shift_name,
                   ts.name AS to_shift_name
            FROM handovers h
            JOIN shifts fs ON fs.id = h.from_shift_id
            JOIN shifts ts ON ts.id = h.to_shift_id
            ORDER BY h.id DESC
            """
        ).fetchall()
        return rows_to_dicts(rows)


def get_handover(handover_id: int) -> dict:
    with connect() as conn:
        h = conn.execute(
            """
            SELECT h.*,
                   fs.name AS from_shift_name,
                   ts.name AS to_shift_name
            FROM handovers h
            JOIN shifts fs ON fs.id = h.from_shift_id
            JOIN shifts ts ON ts.id = h.to_shift_id
            WHERE h.id = ?
            """,
            (handover_id,),
        ).fetchone()
        if not h:
            raise HTTPException(404, "交班单不存在")
        items = conn.execute(
            """
            SELECT b.*,
                   s.name AS owner_shift_name,
                   hi.snapshot_owner_shift_id AS before_owner_shift_id,
                   ps.name AS before_owner_shift_name,
                   hi.snapshot_status AS before_status
            FROM handover_items hi
            JOIN batches b ON b.id = hi.batch_id
            LEFT JOIN shifts s ON s.id = b.owner_shift_id
            LEFT JOIN shifts ps ON ps.id = hi.snapshot_owner_shift_id
            WHERE hi.handover_id = ?
            ORDER BY b.id
            """,
            (handover_id,),
        ).fetchall()
        report = conn.execute(
            "SELECT * FROM reconcile_reports WHERE handover_id = ? ORDER BY id DESC LIMIT 1",
            (handover_id,),
        ).fetchone()
        out = dict(h)
        out["items"] = rows_to_dicts(items)
        out["report"] = _parse_report(report)
        # 失败单的回滚证明与对账报告同源，交班单详情直接可读。
        out["rollback_proof"] = (
            out["report"]["detail"]
            if out["report"] and out["report"]["结论"] == "失败已回滚"
            else None
        )
        return out


def get_rollback_proof(handover_id: int) -> dict:
    """按交班单 id 只读查询持久化回滚证明（证明字段与交班对账报告同源）。"""
    with connect() as conn:
        h = conn.execute(
            "SELECT id, status, reason, started_at, finished_at FROM handovers WHERE id = ?",
            (handover_id,),
        ).fetchone()
        if not h:
            raise HTTPException(404, "交班单不存在")
        report = conn.execute(
            """
            SELECT * FROM reconcile_reports
            WHERE handover_id = ? AND outcome = '失败已回滚'
            ORDER BY id DESC LIMIT 1
            """,
            (handover_id,),
        ).fetchone()
        if not report:
            return {
                "handover_id": handover_id,
                "handover_status": h["status"],
                "有回滚证明": False,
                "回滚证明": None,
            }
        detail = json.loads(report["detail_json"])
        return {
            "handover_id": handover_id,
            "handover_status": h["status"],
            "report_id": report["id"],
            "有回滚证明": True,
            "一致": bool(report["consistent"]),
            "结论": report["outcome"],
            "created_at": report["created_at"],
            # 常用证明字段同时平铺在顶层，读取方无需解包 detail。
            "发起前归属指纹": detail.get("发起前归属指纹"),
            "回滚后交班方在途批次": detail.get("回滚后交班方在途批次"),
            "回滚后交班方在途与发起清单一致": detail.get(
                "回滚后交班方在途与发起清单一致"
            ),
            "锁释放结果": detail.get("锁释放结果"),
            "回滚证明": detail,
        }


def dashboard() -> dict:
    with connect() as conn:
        shifts = rows_to_dicts(conn.execute("SELECT * FROM shifts ORDER BY id").fetchall())
        inflight = conn.execute(
            "SELECT COUNT(*) AS c FROM batches WHERE status = '在途'"
        ).fetchone()["c"]
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM handovers WHERE status = '进行中'"
        ).fetchone()["c"]
        failed = conn.execute(
            "SELECT COUNT(*) AS c FROM handovers WHERE status = '失败'"
        ).fetchone()["c"]
        last = conn.execute(
            "SELECT * FROM handovers ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "班次数": len(shifts),
            "在途批次数": inflight,
            "进行中交班": active,
            "失败交班": failed,
            "最近交班": dict(last) if last else None,
            "班次": shifts,
        }


def _inflight_batches(conn: sqlite3.Connection, shift_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, biz_key FROM batches WHERE owner_shift_id = ? AND status = '在途' ORDER BY id",
        (shift_id,),
    ).fetchall()


def _item_rows(conn: sqlite3.Connection, handover_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT hi.batch_id,
               hi.snapshot_owner_shift_id,
               hi.snapshot_status,
               b.biz_key,
               b.owner_shift_id,
               b.status
        FROM handover_items hi
        LEFT JOIN batches b ON b.id = hi.batch_id
        WHERE hi.handover_id = ?
        ORDER BY hi.batch_id
        """,
        (handover_id,),
    ).fetchall()


def _lock_handover(conn: sqlite3.Connection, handover_id: int) -> sqlite3.Row:
    h = conn.execute("SELECT * FROM handovers WHERE id = ?", (handover_id,)).fetchone()
    if not h:
        raise HTTPException(404, "交班单不存在")
    if h["status"] != "进行中":
        raise HTTPException(400, "只能完成进行中的交班单")
    return h


def _ownership_fingerprint(entries: list[dict]) -> str:
    """发起前归属指纹：对 (批次, 业务键, 归属班次, 状态) 快照做稳定哈希。"""
    canonical = "\n".join(
        "{batch_id}|{biz_key}|{owner}|{status}".format(
            batch_id=e["batch_id"],
            biz_key=e["biz_key"] or "",
            owner="-" if e["owner_shift_id"] is None else e["owner_shift_id"],
            status=e["status"] or "-",
        )
        for e in sorted(entries, key=lambda e: e["batch_id"])
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def start_handover(from_shift_id: int, to_shift_id: int) -> dict:
    """发起交班：写锁内校验+落单+固化归属快照，同一时刻只允许一单进行中。"""
    if from_shift_id == to_shift_id:
        raise HTTPException(400, "交班双方不能是同一班次")
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        fs = conn.execute("SELECT * FROM shifts WHERE id = ?", (from_shift_id,)).fetchone()
        ts = conn.execute("SELECT * FROM shifts WHERE id = ?", (to_shift_id,)).fetchone()
        if not fs or not ts:
            raise HTTPException(404, "班次不存在")
        active = conn.execute(
            "SELECT id FROM handovers WHERE status = '进行中' LIMIT 1"
        ).fetchone()
        if active:
            raise HTTPException(409, f"已有进行中的交班单#{active['id']}，禁止并行交班")
        batches = _inflight_batches(conn, from_shift_id)
        if not batches:
            raise HTTPException(400, "交班方没有在途批次可交接")
        try:
            cur = conn.execute(
                """
                INSERT INTO handovers(
                  from_shift_id, to_shift_id, status, batch_count,
                  from_shift_prev_status, to_shift_prev_status, started_at
                ) VALUES (?, ?, '进行中', ?, ?, ?, ?)
                """,
                (
                    from_shift_id,
                    to_shift_id,
                    len(batches),
                    fs["status"],
                    ts["status"],
                    _now(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "已有进行中的交班单，禁止并行交班") from exc
        hid = cur.lastrowid
        for b in batches:
            conn.execute(
                """
                INSERT INTO handover_items(
                  handover_id, batch_id, snapshot_owner_shift_id, snapshot_status
                ) VALUES (?, ?, ?, '在途')
                """,
                (hid, b["id"], from_shift_id),
            )
        conn.execute("UPDATE shifts SET status = '交班中' WHERE id = ?", (from_shift_id,))
        conn.execute("UPDATE shifts SET status = '接班中' WHERE id = ?", (to_shift_id,))
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_handover(hid)


def _transfer_batch(
    conn: sqlite3.Connection, batch_id: int, from_shift_id: int, to_shift_id: int
) -> None:
    """单批过户：必须恰好命中一条「交班方 + 在途」记录，否则整单失败。"""
    cur = conn.execute(
        """
        UPDATE batches
        SET owner_shift_id = ?
        WHERE id = ? AND owner_shift_id = ? AND status = '在途'
        """,
        (to_shift_id, batch_id, from_shift_id),
    )
    if cur.rowcount != 1:
        raise TransferError(f"批次#{batch_id}不在交班方在途清单内，已中止整单过户")


def _verify_transfer(
    conn: sqlite3.Connection,
    item_ids: list[int],
    from_shift_id: int,
    to_shift_id: int,
) -> tuple[int, int]:
    """落库后复核：清单内每一批都必须已过户，交班方不得残留清单内在途批次。"""
    for batch_id in item_ids:
        row = conn.execute(
            "SELECT owner_shift_id, status FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if row is None:
            raise TransferError(f"批次#{batch_id}已不存在，无法确认过户结果")
        if row["owner_shift_id"] != to_shift_id:
            raise TransferError(f"批次#{batch_id}过户后归属不是接班方")
        if row["status"] != "在途":
            raise TransferError(f"批次#{batch_id}过户后状态异常")
    leftover = conn.execute(
        "SELECT COUNT(*) AS c FROM batches WHERE owner_shift_id = ? AND status = '在途'",
        (from_shift_id,),
    ).fetchone()["c"]
    if leftover:
        raise TransferError(f"交班方仍有 {leftover} 个在途批次未纳入本次清单")
    after_to = conn.execute(
        "SELECT COUNT(*) AS c FROM batches WHERE owner_shift_id = ? AND status = '在途'",
        (to_shift_id,),
    ).fetchone()["c"]
    return leftover, after_to


def _record_success(
    conn: sqlite3.Connection,
    h: sqlite3.Row,
    items: list[sqlite3.Row],
    after_from: int,
    after_to: int,
) -> None:
    now = _now()
    before = len(items)
    conn.execute(
        """
        UPDATE handovers
        SET status = '已完成', finished_at = ?, batch_count = ?, reason = ''
        WHERE id = ?
        """,
        (now, before, h["id"]),
    )
    conn.execute("UPDATE shifts SET status = '已交班' WHERE id = ?", (h["from_shift_id"],))
    conn.execute("UPDATE shifts SET status = '值班中' WHERE id = ?", (h["to_shift_id"],))
    detail = {
        "结论": "成功",
        "失败原因": "",
        "发起清单批次数": before,
        "发起清单批次": [it["biz_key"] for it in items],
        "交班后交班方在途": after_from,
        "交班后接班方在途": after_to,
        "清单已全部过户": True,
        "交班方无残留在途": after_from == 0,
    }
    conn.execute(
        """
        INSERT INTO reconcile_reports(
          handover_id, before_count, after_from_count, after_to_count,
          consistent, outcome, detail_json, created_at
        ) VALUES (?, ?, ?, ?, 1, '成功', ?, ?)
        """,
        (h["id"], before, after_from, after_to, json.dumps(detail, ensure_ascii=False), now),
    )


def _restore_shifts(conn: sqlite3.Connection, h: sqlite3.Row) -> tuple[str, str]:
    """失败回滚：把双方班次恢复到发起时快照，并真正写回库内，释放交班/接班锁。"""
    from_status = _restored_shift_status(h["from_shift_prev_status"], "值班中")
    to_status = _restored_shift_status(h["to_shift_prev_status"], "空闲")
    conn.execute(
        "UPDATE shifts SET status = ? WHERE id = ?", (from_status, h["from_shift_id"])
    )
    conn.execute(
        "UPDATE shifts SET status = ? WHERE id = ?", (to_status, h["to_shift_id"])
    )
    return from_status, to_status


def _lock_release_proof(
    conn: sqlite3.Connection, h: sqlite3.Row, from_status: str, to_status: str
) -> dict:
    """锁释放结果：全部取失败落档后的库内行，不靠内存判断。"""
    active = conn.execute(
        "SELECT COUNT(*) AS c FROM handovers WHERE status = '进行中'"
    ).fetchone()["c"]
    current = conn.execute(
        "SELECT status FROM handovers WHERE id = ?", (h["id"],)
    ).fetchone()
    shift_rows = {
        r["id"]: r["status"]
        for r in conn.execute(
            "SELECT id, status FROM shifts WHERE id IN (?, ?)",
            (h["from_shift_id"], h["to_shift_id"]),
        ).fetchall()
    }
    blocked = from_status in {"交班中", "接班中"} or to_status in {"交班中", "接班中"}
    return {
        "本单状态": current["status"] if current else "不存在",
        "进行中锁": "已释放" if active == 0 else f"仍占用（{active} 单）",
        "全仓进行中交班数": active,
        "交班方班次状态": shift_rows.get(h["from_shift_id"], from_status),
        "接班方班次状态": shift_rows.get(h["to_shift_id"], to_status),
        "双方可再次交班": active == 0 and not blocked,
    }


def _record_failure(
    conn: sqlite3.Connection,
    h: sqlite3.Row,
    reason: str,
    *,
    rolled_back: bool,
) -> None:
    """整单失败：归属已由同一事务回滚，这里落失败单、恢复班次并写回滚证明。

    证明里的数字全部来自回滚后的落库行：发起前归属指纹来自 handover_items 快照，
    失败后交班方在途集合来自 batches 表，锁释放结果来自 handovers/shifts 表，
    与交班单详情、批次表同源，不允许用内存计数冒充落库结果。
    """
    items = _item_rows(conn, h["id"])
    before = len(items)

    snapshot_entries: list[dict] = []
    missing_snapshot = 0
    owner_ok = True
    status_ok = True
    for it in items:
        snap_owner = it["snapshot_owner_shift_id"]
        snap_status = it["snapshot_status"]
        snapshot_entries.append(
            {
                "batch_id": it["batch_id"],
                "biz_key": it["biz_key"],
                "owner_shift_id": snap_owner,
                "status": snap_status,
            }
        )
        if snap_owner is None or snap_status is None:
            missing_snapshot += 1
            continue
        if it["owner_shift_id"] != snap_owner:
            owner_ok = False
        if it["status"] != snap_status:
            status_ok = False

    started_keys = sorted(
        it["biz_key"]
        for it in items
        if it["snapshot_owner_shift_id"] == h["from_shift_id"] and it["biz_key"]
    )
    if not started_keys:
        started_keys = sorted(it["biz_key"] for it in items if it["biz_key"])

    from_inflight = _inflight_batches(conn, h["from_shift_id"])
    from_inflight_keys = sorted(b["biz_key"] for b in from_inflight)
    from_set_matches = from_inflight_keys == started_keys
    after_from = len(from_inflight_keys)
    after_to = conn.execute(
        "SELECT COUNT(*) AS c FROM batches WHERE owner_shift_id = ? AND status = '在途'",
        (h["to_shift_id"],),
    ).fetchone()["c"]

    from_status, to_status = _restore_shifts(conn, h)
    now = _now()
    conn.execute(
        """
        UPDATE handovers
        SET status = '失败', finished_at = ?, batch_count = ?, reason = ?
        WHERE id = ?
        """,
        (now, before, reason, h["id"]),
    )
    lock_proof = _lock_release_proof(conn, h, from_status, to_status)

    detail = {
        "结论": "失败已回滚",
        "失败原因": reason,
        "回滚方式": ROLLBACK_MODE if rolled_back else ROLLBACK_MODE_NO_WRITE,
        "事务回滚已执行": rolled_back,
        "发起清单批次数": before,
        "发起清单批次": started_keys,
        # 发起前归属指纹：交班单发起时固化的归属快照，失败后据此核验零半截。
        "发起前归属指纹": _ownership_fingerprint(snapshot_entries),
        "发起前归属快照": snapshot_entries,
        "缺少发起快照的批次": missing_snapshot,
        "归属已回滚到发起快照": None if missing_snapshot else owner_ok,
        "状态已回到发起快照": None if missing_snapshot else status_ok,
        # 失败后交班方在途集合：取自 batches 表，与批次表同源。
        "回滚后交班方在途批次数": after_from,
        "回滚后交班方在途批次": from_inflight_keys,
        "回滚后交班方在途与发起清单一致": from_set_matches,
        "回滚后接班方在途": after_to,
        "班次状态恢复": {
            h["from_shift_id"]: from_status,
            h["to_shift_id"]: to_status,
        },
        # 锁释放结果：取自 handovers/shifts 落库行。
        "锁释放结果": lock_proof,
    }
    conn.execute(
        """
        INSERT INTO reconcile_reports(
          handover_id, before_count, after_from_count, after_to_count,
          consistent, outcome, detail_json, created_at
        ) VALUES (?, ?, ?, ?, 0, '失败已回滚', ?, ?)
        """,
        (h["id"], before, after_from, after_to, json.dumps(detail, ensure_ascii=False), now),
    )


def _mark_failed(conn: sqlite3.Connection, h: sqlite3.Row, reason: str, before: int) -> None:
    """兜底失败落档：报告写入自身异常时，仍必须释放班次锁并留下最小证明。"""
    from_status, to_status = _restore_shifts(conn, h)
    now = _now()
    conn.execute(
        """
        UPDATE handovers
        SET status = '失败', finished_at = ?, batch_count = ?, reason = ?
        WHERE id = ?
        """,
        (now, before, reason, h["id"]),
    )
    try:
        lock_proof = _lock_release_proof(conn, h, from_status, to_status)
        detail = {
            "结论": "失败已回滚",
            "失败原因": reason,
            "回滚方式": ROLLBACK_MODE,
            "事务回滚已执行": True,
            "发起清单批次数": before,
            "回滚后交班方在途批次": [
                b["biz_key"] for b in _inflight_batches(conn, h["from_shift_id"])
            ],
            "锁释放结果": lock_proof,
            "落档说明": "完整对账报告写入失败，本报告为兜底最小证明",
        }
        conn.execute(
            """
            INSERT INTO reconcile_reports(
              handover_id, before_count, after_from_count, after_to_count,
              consistent, outcome, detail_json, created_at
            ) VALUES (?, ?, ?, ?, 0, '失败已回滚', ?, ?)
            """,
            (
                h["id"],
                before,
                len(detail["回滚后交班方在途批次"]),
                conn.execute(
                    "SELECT COUNT(*) AS c FROM batches WHERE owner_shift_id = ? AND status = '在途'",
                    (h["to_shift_id"],),
                ).fetchone()["c"],
                json.dumps(detail, ensure_ascii=False),
                now,
            ),
        )
    except Exception:  # noqa: BLE001 - 证明落档失败不得阻断失败单与锁释放
        pass


def _fail_with_report(
    conn: sqlite3.Connection,
    h: sqlite3.Row,
    reason: str,
    *,
    rolled_back: bool,
) -> None:
    """写失败报告；若报告本身写不进去，仍必须把单标失败并释放班次锁。"""
    conn.execute("SAVEPOINT fail_record")
    try:
        _record_failure(conn, h, reason, rolled_back=rolled_back)
        conn.execute("RELEASE SAVEPOINT fail_record")
    except Exception:  # noqa: BLE001 - 失败落档自身异常不得吞掉整单释放
        conn.execute("ROLLBACK TO SAVEPOINT fail_record")
        conn.execute("RELEASE SAVEPOINT fail_record")
        _mark_failed(conn, h, reason, h["batch_count"])


def complete_handover(handover_id: int) -> dict:
    """完成交班：全部过户 + 交班单完成 + 对账报告在同一数据库事务内，整单原子。

    原子性方案：`BEGIN IMMEDIATE` 包住整单，过户阶段套 `SAVEPOINT transfer`；
    任一批次过户、落库复核或成功落档失败则 `ROLLBACK TO SAVEPOINT transfer`
    撤销全部归属变更（这是本仓选定的“同一事务整单回滚”闭环方案），
    随后在同一事务内落失败单、恢复双方班次、写回滚证明并提交。
    提交后库里只可能是：全部过户的「已完成」，或归属原样 + 「失败」。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        h = _lock_handover(conn, handover_id)
        items = _item_rows(conn, handover_id)
        before = len(items)

        # 过户前置校验：任何一条不满足都整单失败，禁止“尽量过户、跳过仍成功”。
        problems: list[str] = []
        if before == 0:
            problems.append("交班清单为空，无法完成")
        for it in items:
            if it["owner_shift_id"] is None:
                problems.append(f"批次#{it['batch_id']}不存在")
            elif it["status"] != "在途":
                problems.append(f"批次#{it['biz_key']}状态已变为「{it['status']}」，不能过户")
            elif it["owner_shift_id"] != h["from_shift_id"]:
                problems.append(f"批次#{it['biz_key']}归属已不在交班方")
            elif (
                it["snapshot_owner_shift_id"] is not None
                and it["owner_shift_id"] != it["snapshot_owner_shift_id"]
            ):
                problems.append(f"批次#{it['biz_key']}归属与发起快照不一致")
        item_ids = {it["batch_id"] for it in items}
        current_from = {b["id"] for b in _inflight_batches(conn, h["from_shift_id"])}
        extra = current_from - item_ids
        if extra:
            problems.append(f"交班方存在清单外新在途批次 {len(extra)} 个，整单不能部分过户")

        if problems:
            # 尚未发生任何归属变更，无需回滚；仍落完整证明并释放锁。
            _fail_with_report(conn, h, "；".join(problems), rolled_back=False)
            conn.commit()
            return get_handover(handover_id)

        conn.execute("SAVEPOINT transfer")
        try:
            for it in items:
                _transfer_batch(conn, it["batch_id"], h["from_shift_id"], h["to_shift_id"])
            after_from, after_to = _verify_transfer(
                conn, [it["batch_id"] for it in items], h["from_shift_id"], h["to_shift_id"]
            )
            # 成功落档仍在 savepoint 内：报告写不进去也要连过户一起回滚。
            _record_success(conn, h, items, after_from, after_to)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, TransferError):
                reason = str(exc)
            else:
                reason = f"过户过程中断（{exc.__class__.__name__}）：{exc}"
            # 关键修复：撤销 savepoint 内已写入的部分归属，保证零半截正式归属。
            # 此时既未 RELEASE，失败落档又走独立 savepoint，transfer 必然仍可回滚。
            conn.execute("ROLLBACK TO SAVEPOINT transfer")
            conn.execute("RELEASE SAVEPOINT transfer")
            _fail_with_report(conn, h, reason, rolled_back=True)
        else:
            conn.execute("RELEASE SAVEPOINT transfer")
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_handover(handover_id)


def list_reconcile() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT r.*, h.status AS handover_status,
                   fs.name AS from_shift_name, ts.name AS to_shift_name
            FROM reconcile_reports r
            LEFT JOIN handovers h ON h.id = r.handover_id
            LEFT JOIN shifts fs ON fs.id = h.from_shift_id
            LEFT JOIN shifts ts ON ts.id = h.to_shift_id
            ORDER BY r.id DESC
            """
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            d["一致"] = bool(d.pop("consistent"))
            d["结论"] = d.get("outcome") or ("成功" if d["一致"] else "失败已回滚")
            out.append(d)
        return out
