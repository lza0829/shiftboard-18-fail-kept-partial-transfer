from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services import handover as svc

router = APIRouter(prefix="/handovers", tags=["交班"])


class StartBody(BaseModel):
    from_shift_id: int = Field(..., description="交班班次")
    to_shift_id: int = Field(..., description="接班班次")


@router.get("")
def get_handovers():
    return svc.list_handovers()


@router.get("/{handover_id}")
def get_one(handover_id: int):
    return svc.get_handover(handover_id)


@router.get("/{handover_id}/rollback-proof")
def get_rollback_proof(handover_id: int):
    """按交班单 id 只读查询持久化回滚证明（发起前归属指纹、失败后交班方在途、锁释放结果）。"""
    return svc.get_rollback_proof(handover_id)


@router.post("/start")
def start(body: StartBody):
    return svc.start_handover(body.from_shift_id, body.to_shift_id)


@router.post("/{handover_id}/complete")
def complete(handover_id: int):
    return svc.complete_handover(handover_id)
