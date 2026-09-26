"""合同、前置材料、资格评估与追溯审批 API。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from . import eligibility_services as elig
from .db import get_db
from .schemas import (
    ContractIn,
    ContractOut,
    EligibilityOut,
    MaterialIn,
    MaterialOut,
    RetroIn,
    RetroOut,
    RevokeIn,
    SupplementIn,
    SupplementOut,
    WindowEligibilityIn,
    WindowEligibilityOut,
)

router = APIRouter(prefix="/api/plans/{plan_version}")


def _raise_domain_error(exc: Exception) -> HTTPException:
    if isinstance(exc, elig.PlanNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (elig.MaterialNotFoundError, elig.AdmissionNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(
        exc,
        (
            elig.ContractConflictError,
            elig.VersionConflictError,
            elig.RetroConflictError,
            elig.MaterialStateError,
        ),
    ):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (elig.ContractError, elig.RetroValidationError, ValueError)):
        return HTTPException(status_code=422, detail=str(exc))
    raise exc


# ---------------------------------------------------------------------------
# 合同
# ---------------------------------------------------------------------------


@router.post(
    "/contracts",
    response_model=ContractOut,
    status_code=status.HTTP_201_CREATED,
)
def post_contract(
    plan_version: str, body: ContractIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return elig.register_contract(
            db,
            plan_version=plan_version,
            contract_version=body.contract_version,
            enterprise_id=body.enterprise_id,
            required_documents=list(body.required_documents),
            created_by=body.created_by,
        )
    except Exception as exc:  # noqa: BLE001 - 统一映射为 HTTP 错误
        raise _raise_domain_error(exc) from exc


@router.get("/contracts/latest", response_model=ContractOut)
def get_latest_contract_route(
    plan_version: str, db: Session = Depends(get_db)
) -> Any:
    try:
        contract = elig.get_active_contract(db, plan_version)
    except elig.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if contract is None:
        raise HTTPException(status_code=404, detail="该培养方案尚未登记合同")
    return elig.contract_to_dict(contract)


# ---------------------------------------------------------------------------
# 材料登记 / 补证 / 撤销
# ---------------------------------------------------------------------------


@router.post(
    "/materials",
    response_model=MaterialOut,
    status_code=status.HTTP_201_CREATED,
)
def post_material(
    plan_version: str, body: MaterialIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return elig.register_material(
            db,
            plan_version=plan_version,
            student_id=body.student_id,
            requirement=body.requirement,
            document_ref=body.document_ref,
            valid_from=body.valid_from,
            valid_to=body.valid_to,
            registered_by=body.registered_by,
        )
    except Exception as exc:  # noqa: BLE001
        raise _raise_domain_error(exc) from exc


@router.get("/students/{student_id}/materials", response_model=list[MaterialOut])
def list_materials_route(
    plan_version: str, student_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return elig.list_student_materials(db, plan_version, student_id)
    except elig.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/materials/{material_id}/supplement",
    response_model=SupplementOut,
    status_code=status.HTTP_201_CREATED,
)
def post_supplement(
    plan_version: str,
    material_id: int,
    body: SupplementIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return elig.supplement_material(
            db,
            plan_version=plan_version,
            material_id=material_id,
            document_ref=body.document_ref,
            valid_from=body.valid_from,
            valid_to=body.valid_to,
            actor_id=body.actor_id,
        )
    except Exception as exc:  # noqa: BLE001
        raise _raise_domain_error(exc) from exc


@router.post("/materials/{material_id}/revoke", response_model=MaterialOut)
def post_revoke(
    plan_version: str,
    material_id: int,
    body: RevokeIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return elig.revoke_material(
            db,
            plan_version=plan_version,
            material_id=material_id,
            expected_version=body.expected_version,
            actor_id=body.actor_id,
        )
    except Exception as exc:  # noqa: BLE001
        raise _raise_domain_error(exc) from exc


# ---------------------------------------------------------------------------
# 资格评估
# ---------------------------------------------------------------------------


@router.get(
    "/students/{student_id}/eligibility",
    response_model=EligibilityOut,
)
def get_eligibility(
    plan_version: str,
    student_id: str,
    moment: datetime | None = Query(default=None),
    db: Session = Depends(get_db),
) -> Any:
    try:
        return elig.evaluate_student(
            db, plan_version, student_id, moment=moment
        )
    except elig.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/students/{student_id}/eligibility/window",
    response_model=WindowEligibilityOut,
)
def post_eligibility_window(
    plan_version: str,
    student_id: str,
    body: WindowEligibilityIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return elig.evaluate_window_for_student(
            db,
            plan_version,
            student_id,
            start=body.start,
            end=body.end,
        )
    except elig.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 追溯审批
# ---------------------------------------------------------------------------


@router.post(
    "/events/{event_id}/retro-approval",
    response_model=RetroOut,
    status_code=status.HTTP_201_CREATED,
)
def post_retro_approval(
    plan_version: str,
    event_id: str,
    body: RetroIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return elig.decide_retroactive(
            db,
            plan_version=plan_version,
            event_id=event_id,
            decision=body.decision,
            approver_id=body.approver_id,
            reason=body.reason,
        )
    except Exception as exc:  # noqa: BLE001
        raise _raise_domain_error(exc) from exc


@router.get("/events/{event_id}/retro-approval", response_model=RetroOut)
def get_retro_approval_route(
    plan_version: str, event_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        result = elig.get_retroactive_decision(db, plan_version, event_id)
    except elig.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="该事件尚无追溯决定")
    return result
