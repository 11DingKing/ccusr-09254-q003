"""服务端业务模块。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from . import services
from .db import get_db
from .schemas import (
    CaseDecisionIn,
    CaseOut,
    ContractIn,
    ContractOut,
    DiffOut,
    EventBatchIn,
    FreezeIn,
    ImportResult,
    MaterialIn,
    MaterialOut,
    PlanIn,
    PlanOut,
    QualificationEvaluationOut,
    RevokeIn,
    SnapshotOut,
    StudentProgressOut,
)

router = APIRouter(prefix="/api")


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
def create_plan(body: PlanIn, db: Session = Depends(get_db)) -> Any:
    return services.ensure_plan(
        db,
        plan_version=body.plan_version,
        iana_timezone=body.iana_timezone,
        required_seconds=body.required_seconds,
    )


@router.get("/plans/{plan_version}", response_model=PlanOut)
def read_plan(plan_version: str, db: Session = Depends(get_db)) -> Any:
    plan = services.get_plan_plain(db, plan_version)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


@router.post(
    "/plans/{plan_version}/events",
    response_model=ImportResult,
    status_code=status.HTTP_201_CREATED,
)
def post_events(
    plan_version: str, body: EventBatchIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.import_events(
            db,
            plan_version=plan_version,
            events=[e.model_dump() for e in body.events],
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/snapshot",
    response_model=SnapshotOut,
)
def get_snapshot(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        snap = services.current_snapshot(db, plan_version)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/students/{student_id}/progress",
    response_model=StudentProgressOut,
)
def get_progress(
    plan_version: str, student_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        result = services.student_progress(db, plan_version, student_id)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.post(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
    status_code=status.HTTP_201_CREATED,
)
def post_freeze(
    plan_version: str,
    freeze_id: str,
    body: FreezeIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        snap, _ = services.freeze_semester(
            db, plan_version=plan_version, freeze_id=freeze_id
        )
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
)
def get_freeze(
    plan_version: str, freeze_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        snap = services.get_frozen_snapshot(db, plan_version, freeze_id)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.FreezeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/explain/{student_id}",
    response_model=StudentProgressOut,
)
def explain_freeze_student(
    plan_version: str,
    freeze_id: str,
    student_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        result = services.explain_frozen_student(
            db, plan_version, freeze_id, student_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/diff/{other_freeze_id}",
    response_model=DiffOut,
)
def get_diff(
    plan_version: str,
    freeze_id: str,
    other_freeze_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.diff_freezes(
            db, plan_version, freeze_id, other_freeze_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 合同、材料、资格评估与补证/追溯审批
# ---------------------------------------------------------------------------


@router.post(
    "/plans/{plan_version}/contract",
    response_model=ContractOut,
    status_code=status.HTTP_201_CREATED,
)
def post_contract(
    plan_version: str, body: ContractIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.register_contract(
            db,
            plan_version=plan_version,
            contract_version=body.contract_version,
            required_types=[t for t in body.required_types],
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.ContractConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except services.ContractError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/plans/{plan_version}/contract", response_model=ContractOut)
def get_contract(plan_version: str, db: Session = Depends(get_db)) -> Any:
    plan = services.get_contract_plain(db, plan_version)
    if plan is None:
        raise HTTPException(status_code=404, detail="contract not registered")
    return plan


@router.post(
    "/materials",
    response_model=MaterialOut,
    status_code=status.HTTP_201_CREATED,
)
def post_material(
    body: MaterialIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.register_material(
            db,
            student_id=body.student_id,
            material_type=body.material_type,
            version=body.version,
            valid_from=body.valid_from,
            valid_to=body.valid_to,
            registered_by=body.registered_by,
            note=body.note,
        )
    except services.ContractError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except services.MaterialConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/materials", response_model=list[MaterialOut])
def get_materials(
    student_id: str | None = None, db: Session = Depends(get_db)
) -> Any:
    return services.list_materials_plain(db, student_id=student_id)


@router.post(
    "/materials/{student_id}/{material_type}/{version}/revoke",
    response_model=MaterialOut,
)
def revoke_material_route(
    student_id: str,
    material_type: str,
    version: int,
    body: RevokeIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.revoke_material(
            db,
            student_id=student_id,
            material_type=material_type,
            version=version,
            revoked_by=body.revoked_by,
            reason=body.reason,
        )
    except services.QualificationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/checkins/{event_id}/qualification",
    response_model=QualificationEvaluationOut,
)
def get_checkin_qualification_route(
    plan_version: str, event_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.evaluate_checkin_qualification(db, plan_version, event_id)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.QualificationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/supplement-cases",
    response_model=list[CaseOut],
)
def list_cases_route(
    plan_version: str,
    student_id: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.list_supplement_cases(
            db, plan_version, student_id=student_id, status=status
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/supplement-cases/{case_id}",
    response_model=CaseOut,
)
def get_case_route(
    plan_version: str, case_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.get_supplement_case(db, plan_version, case_id)
    except services.CaseError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/plans/{plan_version}/supplement-cases/{case_id}/decision",
    response_model=CaseOut,
)
def decide_case_route(
    plan_version: str,
    case_id: str,
    body: CaseDecisionIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.decide_supplement_case(
            db,
            plan_version=plan_version,
            case_id=case_id,
            approved=body.approved,
            actor_id=body.actor_id,
            actor_role=body.actor_role,
            reason=body.reason,
            gap_acknowledged=body.gap_acknowledged,
        )
    except services.CaseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
