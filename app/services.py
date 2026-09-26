"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from .core.clock import to_utc
from .core.qualifications import (
    MATERIAL_TYPES,
    MaterialStatus,
    PinnedQualification,
    QualifierInput,
    RetroApproval,
    evaluate,
)
from .core.replay import EventType
from .core.snapshot import Snapshot, build_snapshot, diff_snapshots, explain_student
from . import repository
from .repository import (
    get_freeze,
    get_plan,
    insert_events,
    insert_freeze,
    load_events,
    load_events_up_to,
    max_event_id,
    upsert_plan,
)


class PlanNotFoundError(Exception):
    pass


class FreezeConflictError(Exception):
    pass


class FreezeNotFoundError(Exception):
    pass


class ContractError(ValueError):
    pass


class ContractConflictError(ContractError):
    pass


class MaterialConflictError(ValueError):
    pass


class QualificationNotFoundError(Exception):
    pass


class CaseError(ValueError):
    pass


#: 有权确认追溯计入的角色
AUTHORIZED_APPROVER_ROLES = frozenset({"compliance_officer", "admin"})


def get_plan_plain(db: Session, plan_version: str) -> dict[str, Any] | None:
    plan = get_plan(db, plan_version)
    if plan is None:
        return None
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def ensure_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> dict[str, Any]:
    plan = upsert_plan(
        db,
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def _require_plan(db: Session, plan_version: str):
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    return plan


def import_events(
    db: Session, *, plan_version: str, events: list[dict[str, Any]]
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    accepted, duplicates = insert_events(
        db, plan_version=plan_version, events=events
    )
    pinned_results = _pin_accepted_checkins(db, plan_version, events, accepted)
    return {
        "accepted": len(accepted),
        "duplicates": duplicates,
        "rejected": [],
        "qualifications": pinned_results,
    }


def _pin_accepted_checkins(
    db: Session,
    plan_version: str,
    events: list[dict[str, Any]],
    accepted_ids: list[str],
) -> list[dict[str, Any]]:
    """为新导入的签到固定资格快照；缺项不丢弃，进入待定并立案。"""
    contract = repository.get_contract(db, plan_version)
    results: list[dict[str, Any]] = []
    if contract is None:
        return results
    required_types = [str(t) for t in contract.required_types]
    accepted_set = set(accepted_ids)

    for event in events:
        if event["event_id"] not in accepted_set:
            continue
        if event["event_type"] != EventType.CHECKIN.value:
            continue
        # 重复导入同一事件不重新固定快照
        if repository.get_checkin_qualification(
            db, plan_version, event["event_id"]
        ):
            continue

        student_id = event["student_id"]
        payload = event["payload"]
        start_utc = to_utc(datetime.fromisoformat(payload["check_in_at"]))
        end_utc = to_utc(datetime.fromisoformat(payload["check_out_at"]))
        materials = [
            repository.material_to_core(m)
            for m in repository.list_materials(db, student_id=student_id)
        ]
        evaluation = evaluate(
            required_types, materials, start_utc, end_utc
        )
        pinned = PinnedQualification.from_evaluation(
            contract.contract_version, evaluation
        )
        repository.insert_checkin_qualification(
            db,
            plan_version=plan_version,
            checkin_event_id=event["event_id"],
            contract_version=contract.contract_version,
            snapshot=pinned.to_dict(),
            complete=evaluation.complete,
            missing_types=list(evaluation.missing_types),
        )
        entry: dict[str, Any] = {
            "event_id": event["event_id"],
            "student_id": student_id,
            "complete": evaluation.complete,
            "missing_types": list(evaluation.missing_types),
        }
        if not evaluation.complete:
            case_id = f"CASE-{event['event_id']}"
            if repository.get_case_for_checkin(
                db, plan_version, event["event_id"]
            ) is None:
                repository.insert_supplement_case(
                    db,
                    plan_version=plan_version,
                    case_id=case_id,
                    checkin_event_id=event["event_id"],
                    student_id=student_id,
                    snapshot=pinned.to_dict(),
                )
            entry["case_id"] = case_id
            entry["status"] = "pending_supplement"
        else:
            entry["status"] = "qualified"
        results.append(entry)
    return results


def _build_qualifier(db: Session, plan_version: str):
    """从数据库组装重放所需的资格上下文。"""
    pinned_rows = repository.list_checkin_qualifications(db, plan_version)
    pinned: dict[str, PinnedQualification] = {}
    current_complete: dict[str, bool] = {}
    student_cache: dict[str, list] = {}

    for row in pinned_rows:
        pinned_obj = PinnedQualification.from_dict(row.snapshot)
        pinned[row.checkin_event_id] = pinned_obj
        # 仅对事件时齐全的签到判断事后撤销/失效；缺项签到走补证流程。
        if pinned_obj.complete:
            found = _checkin_interval(db, plan_version, row.checkin_event_id)
            if found is None:
                continue
            event_row, start_utc, end_utc = found
            if event_row.student_id not in student_cache:
                student_cache[event_row.student_id] = [
                    repository.material_to_core(m)
                    for m in repository.list_materials(
                        db, student_id=event_row.student_id
                    )
                ]
            live = evaluate(
                pinned_obj.required_types,
                student_cache[event_row.student_id],
                start_utc,
                end_utc,
            )
            current_complete[row.checkin_event_id] = live.complete

    approvals: dict[str, RetroApproval] = {}
    for case in repository.list_cases(db, plan_version):
        if case.status != "approved" or not case.decision:
            continue
        decision = dict(case.decision)
        approvals[case.checkin_event_id] = RetroApproval(
            case_id=case.case_id,
            checkin_event_id=case.checkin_event_id,
            approved=True,
            approver_id=decision["approver_id"],
            approver_role=decision["approver_role"],
            reason=decision.get("reason", ""),
            decided_at=datetime.fromisoformat(
                decision["decided_at"].replace("Z", "+00:00")
            ),
            gap_acknowledged=bool(decision.get("gap_acknowledged", False)),
        )

    return QualifierInput(
        pinned=pinned,
        current_complete=current_complete,
        approvals=approvals,
    )


def current_snapshot(db: Session, plan_version: str) -> Snapshot:
    plan = _require_plan(db, plan_version)
    events = load_events(db, plan_version)
    qualifier = _build_qualifier(db, plan_version)
    return build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        qualifier=qualifier,
    )


def student_progress(
    db: Session, plan_version: str, student_id: str
) -> dict[str, Any] | None:
    snap = current_snapshot(db, plan_version)
    return explain_student(snap, student_id)


def freeze_semester(
    db: Session, *, plan_version: str, freeze_id: str
) -> tuple[Snapshot, bool]:
    """执行确定性的业务处理。"""
    plan = _require_plan(db, plan_version)
    existing = get_freeze(db, plan_version, freeze_id)
    if existing is not None:
        return Snapshot.from_dict(existing.snapshot), False

    cutoff = max_event_id(db, plan_version)
    events = load_events(db, plan_version)
    qualifier = _build_qualifier(db, plan_version)
    snap = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        freeze_id=freeze_id,
        event_cutoff_id=cutoff,
        qualifier=qualifier,
    )
    row = insert_freeze(
        db,
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snap.to_dict(),
        event_cutoff_id=cutoff,
    )
    if row is None:
        existing = get_freeze(db, plan_version, freeze_id)
        assert existing is not None
        return Snapshot.from_dict(existing.snapshot), False
    return snap, True


def get_frozen_snapshot(
    db: Session, plan_version: str, freeze_id: str
) -> Snapshot:
    _require_plan(db, plan_version)
    row = get_freeze(db, plan_version, freeze_id)
    if row is None:
        raise FreezeNotFoundError(
            f"freeze '{freeze_id}' for plan '{plan_version}' does not exist"
        )
    return Snapshot.from_dict(row.snapshot)


def explain_frozen_student(
    db: Session, plan_version: str, freeze_id: str, student_id: str
) -> dict[str, Any] | None:
    snap = get_frozen_snapshot(db, plan_version, freeze_id)
    return explain_student(snap, student_id)


def diff_freezes(
    db: Session, plan_version: str, old_freeze_id: str, new_freeze_id: str
) -> dict[str, Any]:
    old = get_frozen_snapshot(db, plan_version, old_freeze_id)
    new = get_frozen_snapshot(db, plan_version, new_freeze_id)
    return diff_snapshots(old, new)


# ---------------------------------------------------------------------------
# 合同登记
# ---------------------------------------------------------------------------


def register_contract(
    db: Session,
    *,
    plan_version: str,
    contract_version: str,
    required_types: list[str],
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    normalized = _normalize_required_types(required_types)
    row = repository.insert_contract(
        db,
        plan_version=plan_version,
        contract_version=contract_version,
        required_types=normalized,
    )
    if row is None:
        existing = repository.get_contract(db, plan_version)
        assert existing is not None
        raise ContractConflictError(
            f"contract for plan '{plan_version}' already registered "
            f"as '{existing.contract_version}'"
        )
    return _contract_dict(row)


def _normalize_required_types(required_types: list[str]) -> list[str]:
    if not required_types:
        raise ContractError("required_types must not be empty")
    seen: list[str] = []
    for raw in required_types:
        value = str(raw).strip().lower()
        if value not in MATERIAL_TYPES:
            raise ContractError(f"unknown material type '{raw}'")
        if value not in seen:
            seen.append(value)
        if len(seen) == len(MATERIAL_TYPES):
            break
    # 企业实训三项前置条件缺一不可
    missing = [t for t in MATERIAL_TYPES if t not in seen]
    if missing:
        raise ContractError(
            f"enterprise internship contract must require all of "
            f"{list(MATERIAL_TYPES)}; missing {missing}"
        )
    return MATERIAL_TYPES  # 固定规范顺序


def get_contract_plain(db: Session, plan_version: str) -> dict[str, Any] | None:
    row = repository.get_contract(db, plan_version)
    if row is None:
        return None
    return _contract_dict(row)


def _contract_dict(row) -> dict[str, Any]:
    return {
        "plan_version": row.plan_version,
        "contract_version": row.contract_version,
        "required_types": list(row.required_types),
        "created_at": repository._as_utc(row.created_at)
        .isoformat()
        .replace("+00:00", "Z"),
    }


# ---------------------------------------------------------------------------
# 材料登记
# ---------------------------------------------------------------------------


def register_material(
    db: Session,
    *,
    student_id: str,
    material_type: str,
    version: int,
    valid_from: datetime,
    valid_to: datetime,
    registered_by: str,
    note: str = "",
) -> dict[str, Any]:
    material_type = material_type.strip().lower()
    if material_type not in MATERIAL_TYPES:
        raise ContractError(f"unknown material type '{material_type}'")
    valid_from = to_utc(valid_from)
    valid_to = to_utc(valid_to)
    if valid_to <= valid_from:
        raise ContractError("valid_to must be after valid_from")
    row = repository.insert_material(
        db,
        student_id=student_id,
        material_type=material_type,
        version=version,
        valid_from=valid_from,
        valid_to=valid_to,
        registered_by=registered_by,
        note=note,
    )
    if row is None:
        raise MaterialConflictError(
            f"material '{material_type}' version {version} for student "
            f"'{student_id}' already registered"
        )
    return _material_dict(row)


def revoke_material(
    db: Session,
    *,
    student_id: str,
    material_type: str,
    version: int,
    revoked_by: str,
    reason: str = "",
) -> dict[str, Any]:
    material_type = material_type.strip().lower()
    row = repository.set_material_status(
        db,
        student_id=student_id,
        material_type=material_type,
        version=version,
        status=MaterialStatus.REVOKED.value,
    )
    if row is None:
        raise QualificationNotFoundError("material not found")

    # 撤销固定快照引用了该材料版本的已满足签到：自动转入补证待定并立案，
    # 撤销不会静默抹掉历史，也不自动延长/自动恢复计入。
    opened_cases = _open_revocation_cases(
        db,
        student_id=student_id,
        material_type=material_type,
        version=version,
    )
    result = _material_dict(row)
    result["opened_case_ids"] = opened_cases
    return result


def _open_revocation_cases(
    db: Session,
    *,
    student_id: str,
    material_type: str,
    version: int,
) -> list[str]:
    opened: list[str] = []
    for pinned_row in repository.list_pinned_for_student(db, student_id):
        pinned = PinnedQualification.from_dict(pinned_row.snapshot)
        referenced = any(
            e.material_type == material_type and e.version == version
            for e in pinned.entries
        )
        if not referenced:
            continue
        existing = repository.get_case_for_checkin(
            db, pinned_row.plan_version, pinned_row.checkin_event_id
        )
        if existing is not None:
            continue
        case_id = f"CASE-{pinned_row.checkin_event_id}"
        repository.insert_supplement_case(
            db,
            plan_version=pinned_row.plan_version,
            case_id=case_id,
            checkin_event_id=pinned_row.checkin_event_id,
            student_id=student_id,
            snapshot=pinned_row.snapshot,
        )
        opened.append(case_id)
    return opened


def list_materials_plain(
    db: Session, *, student_id: str | None = None
) -> list[dict[str, Any]]:
    rows = repository.list_materials(db, student_id=student_id)
    return [_material_dict(r) for r in rows]


def _material_dict(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "student_id": row.student_id,
        "material_type": row.material_type,
        "version": row.version,
        "valid_from": repository._as_utc(row.valid_from)
        .isoformat()
        .replace("+00:00", "Z"),
        "valid_to": repository._as_utc(row.valid_to)
        .isoformat()
        .replace("+00:00", "Z"),
        "status": row.status,
        "registered_by": row.registered_by,
        "note": row.note,
    }


# ---------------------------------------------------------------------------
# 资格评估
# ---------------------------------------------------------------------------


def _checkin_interval(db: Session, plan_version: str, event_id: str):
    row = repository.get_event(db, plan_version, event_id)
    if row is None or row.event_type != EventType.CHECKIN.value:
        return None
    payload = dict(row.payload)
    start_utc = to_utc(datetime.fromisoformat(payload["check_in_at"]))
    end_utc = to_utc(datetime.fromisoformat(payload["check_out_at"]))
    return row, start_utc, end_utc


def evaluate_checkin_qualification(
    db: Session, plan_version: str, event_id: str
) -> dict[str, Any]:
    """返回签到的固定快照、按当前材料重评的结果与案件状态（解释用）。"""
    _require_plan(db, plan_version)
    found = _checkin_interval(db, plan_version, event_id)
    if found is None:
        raise QualificationNotFoundError("checkin event not found")
    row, start_utc, end_utc = found
    contract = repository.get_contract(db, plan_version)

    pinned_row = repository.get_checkin_qualification(db, plan_version, event_id)
    pinned = (
        PinnedQualification.from_dict(pinned_row.snapshot)
        if pinned_row is not None
        else None
    )

    required_types = (
        [str(t) for t in contract.required_types]
        if contract is not None
        else list(MATERIAL_TYPES)
    )
    materials = [
        repository.material_to_core(m)
        for m in repository.list_materials(db, student_id=row.student_id)
    ]
    live = evaluate(required_types, materials, start_utc, end_utc)

    # 当前重放状态（含撤销与追溯决定）
    snap = current_snapshot(db, plan_version)
    replay_status = None
    for student in snap.students:
        if student["student_id"] != row.student_id:
            continue
        for checkin in student["checkins"]:
            if checkin["event_id"] == event_id:
                replay_status = {
                    "status": checkin["status"],
                    "counts": checkin["counts"],
                    "pending_reasons": checkin["pending_reasons"],
                    "retro_approval": checkin["retro_approval"],
                }

    case = repository.get_case_for_checkin(db, plan_version, event_id)
    return {
        "plan_version": plan_version,
        "event_id": event_id,
        "student_id": row.student_id,
        "contract_registered": contract is not None,
        "pinned_at_event": pinned.to_dict() if pinned is not None else None,
        "current_evaluation": live.to_dict(),
        "replay": replay_status,
        "case": _case_dict(case) if case is not None else None,
    }


# ---------------------------------------------------------------------------
# 补证案件与追溯审批
# ---------------------------------------------------------------------------


def list_supplement_cases(
    db: Session,
    plan_version: str,
    *,
    student_id: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    _require_plan(db, plan_version)
    rows = repository.list_cases(
        db, plan_version, student_id=student_id, status=status
    )
    return [_case_dict(r) for r in rows]


def get_supplement_case(
    db: Session, plan_version: str, case_id: str
) -> dict[str, Any]:
    row = repository.get_case(db, plan_version, case_id)
    if row is None:
        raise CaseError("supplement case not found")
    return _case_dict(row)


def decide_supplement_case(
    db: Session,
    *,
    plan_version: str,
    case_id: str,
    approved: bool,
    actor_id: str,
    actor_role: str,
    reason: str = "",
    gap_acknowledged: bool = False,
) -> dict[str, Any]:
    """授权人员决定缺项签到是否追溯计入。

    材料补齐本身不会自动延长计入；批准是显式行为，且当前材料仍不覆盖
    活动区间时必须显式确认缺口（gap_acknowledged）。案件终局不可改。
    """
    case = repository.get_case(db, plan_version, case_id)
    if case is None:
        raise CaseError("supplement case not found")
    if case.status != "open":
        raise CaseError(f"case already {case.status}")
    if actor_role not in AUTHORIZED_APPROVER_ROLES:
        raise PermissionError(
            f"role '{actor_role}' is not authorized to decide supplement cases"
        )

    found = _checkin_interval(db, plan_version, case.checkin_event_id)
    assert found is not None
    event_row, start_utc, end_utc = found
    contract = repository.get_contract(db, plan_version)
    required_types = (
        [str(t) for t in contract.required_types]
        if contract is not None
        else list(MATERIAL_TYPES)
    )
    materials = [
        repository.material_to_core(m)
        for m in repository.list_materials(db, student_id=case.student_id)
    ]
    live = evaluate(required_types, materials, start_utc, end_utc)

    # 事件时材料齐全、事后被撤销且无新版本重新覆盖：禁止追溯计入，
    # 作废材料不得靠批准“复活”（过期材料不得自动延长的硬约束）。
    pinned = PinnedQualification.from_dict(case.snapshot)
    if approved and pinned.complete and not live.complete:
        raise CaseError(
            "cannot retroactively approve: materials valid at the event "
            "have been revoked or expired; register replacements first"
        )
    # 事件时缺项、当前仍不覆盖：必须由授权人员显式承认缺口。
    if approved and not live.complete and not gap_acknowledged:
        raise CaseError(
            "current materials still do not cover the activity interval; "
            "gap_acknowledged=true is required to approve"
        )

    decision = {
        "approved": approved,
        "approver_id": actor_id,
        "approver_role": actor_role,
        "reason": reason,
        "decided_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "gap_acknowledged": gap_acknowledged,
        "current_evaluation": live.to_dict(),
    }
    new_status = "approved" if approved else "rejected"
    updated = repository.resolve_case(
        db,
        plan_version=plan_version,
        case_id=case_id,
        status=new_status,
        decision=decision,
    )
    if updated is None:
        raise CaseError("case already decided concurrently")
    return _case_dict(updated)


def _case_dict(row) -> dict[str, Any]:
    return {
        "plan_version": row.plan_version,
        "case_id": row.case_id,
        "checkin_event_id": row.checkin_event_id,
        "student_id": row.student_id,
        "status": row.status,
        "snapshot": row.snapshot,
        "decision": row.decision,
        "created_at": repository._as_utc(row.created_at)
        .isoformat()
        .replace("+00:00", "Z"),
        "updated_at": repository._as_utc(row.updated_at)
        .isoformat()
        .replace("+00:00", "Z"),
    }
