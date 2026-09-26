"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.eligibility import STATUS_SUPERSEDED
from .core.replay import Event as CoreEvent
from .core.replay import EventType
from .models import (
    Contract,
    Event as EventModel,
    EventAdmission,
    Freeze,
    Plan,
    PrerequisiteMaterial,
    RetroApproval,
)


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
    commit: bool = True,
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
    if commit:
        db.commit()
    return accepted, duplicates


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
# 合同与前置条件
# ---------------------------------------------------------------------------


def get_latest_contract(db: Session, plan_version: str) -> Contract | None:
    stmt = (
        select(Contract)
        .where(Contract.plan_version == plan_version)
        .order_by(Contract.created_at.desc(), Contract.contract_version.desc())
        .limit(1)
    )
    return db.execute(stmt).scalars().first()


def insert_contract(
    db: Session,
    *,
    plan_version: str,
    contract_version: str,
    enterprise_id: str,
    required_documents: list[str],
    created_by: str,
) -> Contract | None:
    stmt = sqlite_insert(Contract).values(
        plan_version=plan_version,
        contract_version=contract_version,
        enterprise_id=enterprise_id,
        required_documents=required_documents,
        created_by=created_by,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "contract_version"]
    ).returning(Contract.contract_version)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is None:
        return None
    return db.get(Contract, (plan_version, contract_version))


def insert_material(
    db: Session,
    *,
    plan_version: str,
    student_id: str,
    requirement: str,
    document_ref: str,
    valid_from: datetime,
    valid_to: datetime | None,
    registered_by: str,
    supersedes_id: int | None = None,
) -> PrerequisiteMaterial:
    row = PrerequisiteMaterial(
        plan_version=plan_version,
        student_id=student_id,
        requirement=requirement,
        document_ref=document_ref,
        valid_from=valid_from,
        valid_to=valid_to,
        status="active",
        version=1,
        supersedes_id=supersedes_id,
        registered_by=registered_by,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def replace_material(
    db: Session,
    *,
    old_id: int,
    expected_version: int,
    document_ref: str,
    valid_from: datetime,
    valid_to: datetime | None,
    actor_id: str,
) -> tuple[PrerequisiteMaterial, PrerequisiteMaterial] | None:
    """补证：在同一事务内插入新材料并把旧材料置为 superseded。

    乐观锁条件下推到 UPDATE：即使并发事务先提交导致版本前移，本语句
    匹配行数为 0，整体回滚（不会留下悬挂的新材料）。
    """
    old = db.get(PrerequisiteMaterial, old_id)
    if old is None:
        db.rollback()
        return None

    new_row = PrerequisiteMaterial(
        plan_version=old.plan_version,
        student_id=old.student_id,
        requirement=old.requirement,
        document_ref=document_ref,
        valid_from=valid_from,
        valid_to=valid_to,
        status="active",
        version=1,
        supersedes_id=old.id,
        registered_by=actor_id,
    )

    stmt = (
        sa_update(PrerequisiteMaterial)
        .where(
            PrerequisiteMaterial.id == old_id,
            PrerequisiteMaterial.version == expected_version,
        )
        .values(
            status=STATUS_SUPERSEDED,
            version=expected_version + 1,
            updated_at=datetime.now(timezone.utc),
        )
    )
    if db.execute(stmt).rowcount != 1:
        db.rollback()
        return None

    db.add(new_row)
    db.commit()
    db.refresh(new_row)
    refreshed_old = db.get(PrerequisiteMaterial, old_id)
    return new_row, refreshed_old


def get_material(db: Session, material_id: int) -> PrerequisiteMaterial | None:
    return db.get(PrerequisiteMaterial, material_id)


def list_materials(
    db: Session,
    plan_version: str,
    student_id: str,
    *,
    requirement: str | None = None,
) -> list[PrerequisiteMaterial]:
    stmt = select(PrerequisiteMaterial).where(
        PrerequisiteMaterial.plan_version == plan_version,
        PrerequisiteMaterial.student_id == student_id,
    )
    if requirement is not None:
        stmt = stmt.where(PrerequisiteMaterial.requirement == requirement)
    stmt = stmt.order_by(PrerequisiteMaterial.id)
    return list(db.execute(stmt).scalars().all())


def update_material_status(
    db: Session,
    material_id: int,
    status: str,
    *,
    expected_version: int,
) -> PrerequisiteMaterial | None:
    """乐观锁更新；版本不匹配（已被并发事务改动）时返回 None。"""
    stmt = (
        sa_update(PrerequisiteMaterial)
        .where(
            PrerequisiteMaterial.id == material_id,
            PrerequisiteMaterial.version == expected_version,
        )
        .values(
            status=status,
            version=expected_version + 1,
            updated_at=datetime.now(timezone.utc),
        )
    )
    if db.execute(stmt).rowcount != 1:
        db.rollback()
        return None
    db.commit()
    return db.get(PrerequisiteMaterial, material_id)


def get_admission(
    db: Session, plan_version: str, event_id: str
) -> EventAdmission | None:
    stmt = select(EventAdmission).where(
        EventAdmission.plan_version == plan_version,
        EventAdmission.event_id == event_id,
    )
    return db.execute(stmt).scalars().first()


def insert_admission_if_absent(
    db: Session,
    *,
    plan_version: str,
    event_id: str,
    student_id: str,
    blocking: bool,
    contract_version: str | None,
    capture: dict[str, Any],
    commit: bool = True,
) -> EventAdmission:
    stmt = sqlite_insert(EventAdmission).values(
        plan_version=plan_version,
        event_id=event_id,
        student_id=student_id,
        blocking=blocking,
        contract_version=contract_version,
        capture=capture,
        retroactive=None,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "event_id"]
    ).returning(EventAdmission.id)
    inserted = db.execute(stmt).scalar_one_or_none()
    if commit:
        db.commit()
    if inserted is not None:
        if commit:
            row = db.get(EventAdmission, inserted)
        else:
            db.flush()
            row = db.get(EventAdmission, inserted)
    else:
        row = get_admission(db, plan_version, event_id)
    assert row is not None
    return row


def load_admissions(
    db: Session, plan_version: str
) -> dict[str, EventAdmission]:
    stmt = select(EventAdmission).where(
        EventAdmission.plan_version == plan_version
    )
    return {row.event_id: row for row in db.execute(stmt).scalars().all()}


def insert_retro_approval(
    db: Session,
    *,
    plan_version: str,
    event_id: str,
    student_id: str,
    decision: str,
    approver_id: str,
    reason: str,
    revalidation: dict[str, Any],
) -> RetroApproval | None:
    """并发情况下只有一个决定能落库（唯一约束）。"""
    stmt = sqlite_insert(RetroApproval).values(
        plan_version=plan_version,
        event_id=event_id,
        student_id=student_id,
        decision=decision,
        approver_id=approver_id,
        reason=reason,
        revalidation=revalidation,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "event_id"]
    ).returning(RetroApproval.id)
    inserted = db.execute(stmt).scalar_one_or_none()
    if inserted is None:
        db.rollback()
        return None
    # 同步标记资格快照常的追溯结论，使重放保持确定。
    admission = get_admission(db, plan_version, event_id)
    if admission is not None:
        admission.retroactive = decision
        admission.updated_at = datetime.now(timezone.utc)
    db.commit()
    return db.get(RetroApproval, inserted)


def get_retro_approval(
    db: Session, plan_version: str, event_id: str
) -> RetroApproval | None:
    stmt = select(RetroApproval).where(
        RetroApproval.plan_version == plan_version,
        RetroApproval.event_id == event_id,
    )
    return db.execute(stmt).scalars().first()
