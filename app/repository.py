"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.qualifications import MaterialStatus, MaterialVersion
from .core.replay import Event as CoreEvent
from .core.replay import EventType
from .models import (
    CheckinQualification,
    Contract,
    Event as EventModel,
    Freeze,
    Material,
    Plan,
    SupplementCase,
)


def _as_utc(value: datetime) -> datetime:
    """SQLite 不保留时区信息，读回时按 UTC 解释。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def get_plan(db: Session, plan_version: str) -> Plan | None:
    return db.get(Plan, plan_version)


def upsert_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> Plan:
    stmt = sqlite_insert(Plan).values(
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version"],
        set_={
            "iana_timezone": iana_timezone,
            "required_seconds": required_seconds,
        },
    )
    db.execute(stmt)
    db.commit()
    plan = db.get(Plan, plan_version)
    assert plan is not None
    return plan


def _to_core_event(row: EventModel) -> CoreEvent:
    return CoreEvent(
        event_id=row.event_id,
        plan_version=row.plan_version,
        event_type=EventType(row.event_type),
        student_id=row.student_id,
        payload=dict(row.payload),
        created_at=row.created_at,
    )


def insert_events(
    db: Session,
    *,
    plan_version: str,
    events: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """执行确定性的业务处理。"""
    accepted: list[str] = []
    duplicates: list[str] = []
    for e in events:
        stmt = sqlite_insert(EventModel).values(
            event_id=e["event_id"],
            plan_version=plan_version,
            student_id=e["student_id"],
            event_type=e["event_type"],
            payload=e["payload"],
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["event_id", "plan_version"]
        ).returning(EventModel.id)
        inserted_id = db.execute(stmt).scalar_one_or_none()
        if inserted_id is not None:
            accepted.append(e["event_id"])
        else:
            duplicates.append(e["event_id"])
    db.commit()
    return accepted, duplicates


def get_event(db: Session, plan_version: str, event_id: str) -> EventModel | None:
    stmt = (
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .where(EventModel.event_id == event_id)
    )
    return db.execute(stmt).scalars().first()


def load_events(db: Session, plan_version: str) -> list[CoreEvent]:
    stmt = select(EventModel).where(EventModel.plan_version == plan_version)
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def load_events_up_to(
    db: Session, plan_version: str, max_event_id: str
) -> list[CoreEvent]:
    """执行确定性的业务处理。"""
    stmt = (
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .where(EventModel.event_id <= max_event_id)
    )
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def max_event_id(db: Session, plan_version: str) -> str | None:
    stmt = (
        select(EventModel.event_id)
        .where(EventModel.plan_version == plan_version)
        .order_by(EventModel.event_id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def get_freeze(
    db: Session, plan_version: str, freeze_id: str
) -> Freeze | None:
    return db.get(Freeze, (plan_version, freeze_id))


def insert_freeze(
    db: Session,
    *,
    plan_version: str,
    freeze_id: str,
    snapshot: dict[str, Any],
    event_cutoff_id: str | None,
) -> Freeze | None:
    """执行确定性的业务处理。"""
    stmt = sqlite_insert(Freeze).values(
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snapshot,
        event_cutoff_id=event_cutoff_id,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "freeze_id"]
    ).returning(Freeze.plan_version)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return db.get(Freeze, (plan_version, freeze_id))
    return None


# ---------------------------------------------------------------------------
# 合同与资格
# ---------------------------------------------------------------------------


def get_contract(db: Session, plan_version: str) -> Contract | None:
    stmt = select(Contract).where(Contract.plan_version == plan_version)
    return db.execute(stmt).scalars().first()


def insert_contract(
    db: Session,
    *,
    plan_version: str,
    contract_version: str,
    required_types: list[str],
) -> Contract | None:
    """登记合同；同一培养方案已有合同时拒绝（版本不可变）。"""
    stmt = sqlite_insert(Contract).values(
        plan_version=plan_version,
        contract_version=contract_version,
        required_types=required_types,
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["plan_version"]).returning(
        Contract.plan_version
    )
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is None:
        return None
    return get_contract(db, plan_version)


def list_materials(
    db: Session, *, student_id: str | None = None
) -> list[Material]:
    stmt = select(Material)
    if student_id is not None:
        stmt = stmt.where(Material.student_id == student_id)
    rows = db.execute(stmt.order_by(Material.id)).scalars().all()
    return list(rows)


def insert_material(
    db: Session,
    *,
    student_id: str,
    material_type: str,
    version: int,
    valid_from: datetime,
    valid_to: datetime,
    registered_by: str,
    note: str,
) -> Material | None:
    stmt = sqlite_insert(Material).values(
        student_id=student_id,
        material_type=material_type,
        version=version,
        valid_from=valid_from.astimezone(timezone.utc),
        valid_to=valid_to.astimezone(timezone.utc),
        status="active",
        registered_by=registered_by,
        note=note,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["student_id", "material_type", "version"]
    ).returning(Material.id)
    inserted_id = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted_id is None:
        return None
    return db.get(Material, inserted_id)


def get_material(
    db: Session, *, student_id: str, material_type: str, version: int
) -> Material | None:
    stmt = (
        select(Material)
        .where(Material.student_id == student_id)
        .where(Material.material_type == material_type)
        .where(Material.version == version)
    )
    return db.execute(stmt).scalars().first()


def set_material_status(
    db: Session,
    *,
    student_id: str,
    material_type: str,
    version: int,
    status: str,
) -> Material | None:
    """状态机式更新；仅在当前状态不同且行存在时生效。"""
    row = get_material(
        db,
        student_id=student_id,
        material_type=material_type,
        version=version,
    )
    if row is None or row.status == status:
        db.rollback()
        return row
    row.status = status
    db.commit()
    db.refresh(row)
    return row


def material_to_core(row: Material) -> MaterialVersion:
    return MaterialVersion(
        student_id=row.student_id,
        material_type=row.material_type,
        version=row.version,
        valid_from=_as_utc(row.valid_from),
        valid_to=_as_utc(row.valid_to),
        revoked=(row.status == MaterialStatus.REVOKED.value),
    )


def insert_checkin_qualification(
    db: Session,
    *,
    plan_version: str,
    checkin_event_id: str,
    contract_version: str,
    snapshot: dict[str, Any],
    complete: bool,
    missing_types: list[str],
) -> CheckinQualification | None:
    stmt = sqlite_insert(CheckinQualification).values(
        plan_version=plan_version,
        checkin_event_id=checkin_event_id,
        contract_version=contract_version,
        snapshot=snapshot,
        complete=complete,
        missing_types=missing_types,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "checkin_event_id"]
    ).returning(CheckinQualification.checkin_event_id)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is None:
        return None
    return db.get(CheckinQualification, (plan_version, checkin_event_id))


def get_checkin_qualification(
    db: Session, plan_version: str, checkin_event_id: str
) -> CheckinQualification | None:
    return db.get(CheckinQualification, (plan_version, checkin_event_id))


def list_checkin_qualifications(
    db: Session, plan_version: str
) -> list[CheckinQualification]:
    stmt = select(CheckinQualification).where(
        CheckinQualification.plan_version == plan_version
    )
    return list(db.execute(stmt).scalars().all())


def list_pinned_for_student(
    db: Session, student_id: str
) -> list[CheckinQualification]:
    """跨培养方案查找学员签到固定的资格快照（撤销联动用）。"""
    stmt = (
        select(CheckinQualification)
        .join(
            EventModel,
            (EventModel.plan_version == CheckinQualification.plan_version)
            & (EventModel.event_id == CheckinQualification.checkin_event_id),
        )
        .where(EventModel.student_id == student_id)
    )
    return list(db.execute(stmt).scalars().all())


def insert_supplement_case(
    db: Session,
    *,
    plan_version: str,
    case_id: str,
    checkin_event_id: str,
    student_id: str,
    snapshot: dict[str, Any],
) -> SupplementCase | None:
    stmt = sqlite_insert(SupplementCase).values(
        plan_version=plan_version,
        case_id=case_id,
        checkin_event_id=checkin_event_id,
        student_id=student_id,
        status="open",
        snapshot=snapshot,
        decision=None,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "case_id"]
    ).returning(SupplementCase.case_id)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is None:
        return None
    return db.get(SupplementCase, (plan_version, case_id))


def get_case(
    db: Session, plan_version: str, case_id: str
) -> SupplementCase | None:
    return db.get(SupplementCase, (plan_version, case_id))


def get_case_for_checkin(
    db: Session, plan_version: str, checkin_event_id: str
) -> SupplementCase | None:
    stmt = (
        select(SupplementCase)
        .where(SupplementCase.plan_version == plan_version)
        .where(SupplementCase.checkin_event_id == checkin_event_id)
    )
    return db.execute(stmt).scalars().first()


def list_cases(
    db: Session,
    plan_version: str,
    *,
    student_id: str | None = None,
    status: str | None = None,
) -> list[SupplementCase]:
    stmt = select(SupplementCase).where(SupplementCase.plan_version == plan_version)
    if student_id is not None:
        stmt = stmt.where(SupplementCase.student_id == student_id)
    if status is not None:
        stmt = stmt.where(SupplementCase.status == status)
    return list(db.execute(stmt.order_by(SupplementCase.case_id)).scalars().all())


def resolve_case(
    db: Session,
    *,
    plan_version: str,
    case_id: str,
    status: str,
    decision: dict[str, Any],
) -> SupplementCase | None:
    """追加式终局：仅 open 案件可被授权人员决定。

    使用带状态条件的 UPDATE 实现并发安全：两个并发决定只有一个生效，
    终局后重放为 None。
    """
    stmt = (
        update(SupplementCase)
        .where(SupplementCase.plan_version == plan_version)
        .where(SupplementCase.case_id == case_id)
        .where(SupplementCase.status == "open")
        .values(status=status, decision=decision)
        .returning(SupplementCase.case_id)
    )
    affected = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if affected is None:
        return None
    return get_case(db, plan_version, case_id)
