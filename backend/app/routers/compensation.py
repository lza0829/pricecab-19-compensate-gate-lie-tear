from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services import compensation_service as svc

router = APIRouter(prefix="/compensation", tags=["compensation"])


class MarkIn(BaseModel):
    target_type: str  # ticket | batch
    target_id: int
    reason: str | None = None


class CompensationLineIn(BaseModel):
    sku_code: str
    new_price: float


class CompensationTicketIn(BaseModel):
    marker_id: int
    effective_at: str
    note: str | None = None
    lines: list[CompensationLineIn] = Field(default_factory=list)


class StrategyIn(BaseModel):
    compensation_strategy: str


@router.get("/strategy")
def get_strategy():
    return svc.get_strategy()


@router.put("/strategy")
def set_strategy(body: StrategyIn):
    return svc.set_strategy(body.compensation_strategy)


@router.get("/markers")
def list_markers():
    return svc.list_markers()


@router.get("/markers/{marker_id}")
def get_marker(marker_id: int):
    return svc.get_marker(marker_id)


@router.post("/markers")
def mark_misapply(body: MarkIn):
    return svc.mark_misapply(body.model_dump())


@router.get("/gates")
def list_gates():
    return svc.list_gates()


@router.get("/gates/integrity")
def gate_integrity():
    return svc.gate_integrity()


@router.post("/tickets")
def create_compensation_ticket(body: CompensationTicketIn):
    return svc.create_compensation_ticket(body.model_dump())
