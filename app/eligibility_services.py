"""合同、前置材料、资格快照与追溯审批的服务层。

关键约束：
* 签到事件导入时按当时已登记的材料评估并**固定**资格快照，事后登记、
  补证、撤销材料都不会改写历史结论；
* 前置条件缺项的实训签到进入待定（PENDING），不会被丢弃，导师确认也
  无法解除阻塞；
* 材料补齐后须由授权人员做追溯审批，审批按活动完整窗口重新校验材料
  有效期，过期材料不会被自动延长。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from .core import eligibility
from .core.eligibility import (
    Admission,
    MaterialView,
    STATUS_REVOKED,
    STATUS_SUPERSEDED,
    capture_admission,
    ensure_utc,
    evaluate_current,
    evaluate_window,
    validate_requirement_code,
)
from .repository import (
    get_admission,
    get_latest_contract,
    get_material,
    get_plan,
    get_retro_approval,
    insert_admission_if_absent,
    insert_contract,
    insert_material,
    insert_retro_approval,
    list_materials,
    replace_material,
    update_material_status,
)


class PlanNotFoundError(Exception):
    pass


def _require_plan(db: Session, plan_version: str):
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    return plan


class ContractConflictError(Exception):
    pass


class ContractError(ValueError):
    pass


class MaterialNotFoundError(Exception):
    pass


class MaterialStateError(ValueError):
    pass


class VersionConflictError(Exception):
    """乐观锁版本冲突（并发更新）。"""


class AdmissionNotFoundError(Exception):
    pass


class RetroConflictError(Exception):
    """该签到已存在追溯决定。"""


class RetroValidationError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _material_view(row) -> MaterialView:
    return MaterialView(
        id=row.id,
        version=row.version,
        requirement=row.requirement,
        document_ref=row.document_ref,
        valid_from=ensure_utc(row.valid_from),
        valid_to=ensure_utc(row.valid_to) if row.valid_to is not None else None,
        status=row.status,
        supersedes_id=row.supersedes_id,
    )


def _validate_window(valid_from: datetime, valid_to: datetime | None) -> None:
    if valid_from.tzinfo is None or (
        valid_to is not None and valid_to.tzinfo is None
    ):
        raise ContractError("有效期必须包含时区")
    if valid_to is not None and ensure_utc(valid_to) <= ensure_utc(valid_from):
        raise ContractError("有效期结束时间必须晚于开始时间")


def _material_to_dict(row) -> dict[str, Any]:
    return {
        "material_id": row.id,
        "plan_version": row.plan_version,
        "student_id": row.student_id,
        "requirement": row.requirement,
        "document_ref": row.document_ref,
        "valid_from": ensure_utc(row.valid_from).isoformat().replace("+00:00", "Z"),
        "valid_to": (
            ensure_utc(row.valid_to).isoformat().replace("+00:00", "Z")
            if row.valid_to is not None
            else None
        ),
        "status": row.status,
        "version": row.version,
        "supersedes_id": row.supersedes_id,
        "registered_by": row.registered_by,
        "created_at": ensure_utc(row.created_at).isoformat().replace("+00:00", "Z"),
        "updated_at": ensure_utc(row.updated_at).isoformat().replace("+00:00", "Z"),
    }


# ---------------------------------------------------------------------------
# 合同登记
# ---------------------------------------------------------------------------


def contract_to_dict(row) -> dict[str, Any]:
    return {
        "plan_version": row.plan_version,
        "contract_version": row.contract_version,
        "enterprise_id": row.enterprise_id,
        "required_documents": list(row.required_documents),
        "created_by": row.created_by,
        "created_at": ensure_utc(row.created_at).isoformat().replace("+00:00", "Z"),
    }


def register_contract(
    db: Session,
    *,
    plan_version: str,
    contract_version: str,
    enterprise_id: str,
    required_documents: list[str],
    created_by: str,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    if not contract_version.strip() or not enterprise_id.strip() or not created_by.strip():
        raise ContractError("合同版本、企业标识和登记人不能为空")
    if not required_documents:
        raise ContractError("合同至少要约定一项前置条件")
    normalized: list[str] = []
    for code in required_documents:
        code = validate_requirement_code(code.strip())
        if code not in normalized:
            normalized.append(code)

    row = insert_contract(
        db,
        plan_version=plan_version,
        contract_version=contract_version.strip(),
        enterprise_id=enterprise_id.strip(),
        required_documents=normalized,
        created_by=created_by.strip(),
    )
    if row is None:
        raise ContractConflictError(
            f"合同版本 '{contract_version}' 在培养方案 '{plan_version}' 下已存在"
        )
    return contract_to_dict(row)


def get_active_contract(db: Session, plan_version: str):
    """返回当前适用合同；未登记合同时返回 None（该方案不做前置阻塞）。"""
    return get_latest_contract(db, plan_version)


# ---------------------------------------------------------------------------
# 材料登记 / 补证 / 撤销
# ---------------------------------------------------------------------------


def register_material(
    db: Session,
    *,
    plan_version: str,
    student_id: str,
    requirement: str,
    document_ref: str,
    valid_from: datetime,
    valid_to: datetime | None,
    registered_by: str,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    if not student_id.strip() or not document_ref.strip() or not registered_by.strip():
        raise ContractError("学员、材料编号和登记人不能为空")
    requirement = validate_requirement_code(requirement.strip())
    _validate_window(valid_from, valid_to)

    row = insert_material(
        db,
        plan_version=plan_version,
        student_id=student_id.strip(),
        requirement=requirement,
        document_ref=document_ref.strip(),
        valid_from=ensure_utc(valid_from),
        valid_to=ensure_utc(valid_to) if valid_to is not None else None,
        registered_by=registered_by.strip(),
    )
    return _material_to_dict(row)


def _load_material_or_404(db: Session, material_id: int):
    row = get_material(db, material_id)
    if row is None:
        raise MaterialNotFoundError(f"材料 #{material_id} 不存在")
    return row


def supplement_material(
    db: Session,
    *,
    plan_version: str,
    material_id: int,
    document_ref: str,
    valid_from: datetime,
    valid_to: datetime | None,
    actor_id: str,
) -> dict[str, Any]:
    """补证：以新材料替代旧材料（旧材料置为 superseded，不延长其有效期）。"""
    _require_plan(db, plan_version)
    if not document_ref.strip() or not actor_id.strip():
        raise ContractError("材料编号和操作人不能为空")
    _validate_window(valid_from, valid_to)

    old = _load_material_or_404(db, material_id)
    if old.plan_version != plan_version:
        raise MaterialNotFoundError(
            f"材料 #{material_id} 不属于培养方案 '{plan_version}'"
        )
    if old.status == STATUS_SUPERSEDED:
        raise MaterialStateError("材料已被补证替代，请基于最新材料补证")
    if old.status == STATUS_REVOKED:
        raise MaterialStateError("已撤销材料不能补证，请重新登记")

    result = replace_material(
        db,
        old_id=old.id,
        expected_version=old.version,
        document_ref=document_ref.strip(),
        valid_from=ensure_utc(valid_from),
        valid_to=ensure_utc(valid_to) if valid_to is not None else None,
        actor_id=actor_id.strip(),
    )
    if result is None:
        row = _load_material_or_404(db, material_id)
        raise VersionConflictError(
            f"材料 #{material_id} 已被并发更新（提交版本 {old.version}，"
            f"当前版本 {row.version}），补证被拒绝"
        )
    new_row, previous = result
    return {
        "supplemented": _material_to_dict(new_row),
        "previous": _material_to_dict(previous),
    }


def revoke_material(
    db: Session,
    *,
    plan_version: str,
    material_id: int,
    expected_version: int,
    actor_id: str,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    if not actor_id.strip():
        raise ContractError("操作人不能为空")
    row = _load_material_or_404(db, material_id)
    if row.plan_version != plan_version:
        raise MaterialNotFoundError(
            f"材料 #{material_id} 不属于培养方案 '{plan_version}'"
        )
    updated = update_material_status(
        db, material_id, STATUS_REVOKED, expected_version=expected_version
    )
    if updated is None:
        row = _load_material_or_404(db, material_id)
        raise VersionConflictError(
            f"材料 #{material_id} 版本过期（提交 {expected_version}，"
            f"当前 {row.version}），撤销被拒绝"
        )
    return _material_to_dict(updated)


def list_student_materials(
    db: Session, plan_version: str, student_id: str
) -> list[dict[str, Any]]:
    _require_plan(db, plan_version)
    return [
        _material_to_dict(row)
        for row in list_materials(db, plan_version, student_id)
    ]


# ---------------------------------------------------------------------------
# 资格评估
# ---------------------------------------------------------------------------


def _student_material_views(db: Session, plan_version: str, student_id: str):
    return [_material_view(r) for r in list_materials(db, plan_version, student_id)]


def evaluate_student(
    db: Session,
    plan_version: str,
    student_id: str,
    *,
    moment: datetime | None = None,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    contract = get_latest_contract(db, plan_version)
    required = list(contract.required_documents) if contract else []
    result = evaluate_current(
        _student_material_views(db, plan_version, student_id),
        required,
        moment or _now(),
    )
    result["plan_version"] = plan_version
    result["student_id"] = student_id
    result["contract_version"] = contract.contract_version if contract else None
    return result


def evaluate_window_for_student(
    db: Session,
    plan_version: str,
    student_id: str,
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """按给定活动窗口试评估（不落快照）。"""
    _require_plan(db, plan_version)
    contract = get_latest_contract(db, plan_version)
    required = list(contract.required_documents) if contract else []
    result = evaluate_window(
        _student_material_views(db, plan_version, student_id),
        required,
        start,
        end,
    )
    result["plan_version"] = plan_version
    result["student_id"] = student_id
    result["contract_version"] = contract.contract_version if contract else None
    return result


# ---------------------------------------------------------------------------
# 事件发生时的资格快照
# ---------------------------------------------------------------------------


def capture_event_admission(
    db: Session,
    *,
    plan_version: str,
    event_id: str,
    student_id: str,
    activity_type: str,
    check_in_at: datetime,
    check_out_at: datetime,
    commit: bool = True,
) -> dict[str, Any]:
    """为单个签到事件固定资格快照（幂等：重复调用返回既有快照）。

    只有在方案已登记合同且活动为企业实训时才固定快照；普通活动与无合同
    方案完全不受前置条件约束，不产生记录。
    """
    existing = get_admission(db, plan_version, event_id)
    if existing is not None:
        return {
            "event_id": event_id,
            "blocking": existing.blocking,
            "capture": existing.capture,
            "retroactive": existing.retroactive,
            "created": False,
            "skipped": False,
        }

    contract = get_latest_contract(db, plan_version)
    if contract is None or activity_type != "internship":
        return {
            "event_id": event_id,
            "blocking": False,
            "capture": None,
            "retroactive": None,
            "created": False,
            "skipped": True,
        }

    blocking = True
    required = list(contract.required_documents)
    materials = _student_material_views(db, plan_version, student_id)
    capture = capture_admission(
        materials,
        required,
        check_in_at,
        check_out_at,
        blocking=blocking,
        contract_version=contract.contract_version,
        evaluated_at=_now(),
    )
    row = insert_admission_if_absent(
        db,
        plan_version=plan_version,
        event_id=event_id,
        student_id=student_id,
        blocking=blocking,
        contract_version=contract.contract_version,
        capture=capture,
        commit=commit,
    )
    return {
        "event_id": event_id,
        "blocking": row.blocking,
        "capture": row.capture,
        "retroactive": row.retroactive,
        "created": True,
        "skipped": False,
    }


def build_admission_map(db: Session, plan_version: str) -> dict[str, Admission]:
    """供重放使用：event_id -> 固定快照（含最新追溯结论）。"""
    from .repository import load_admissions

    return {
        event_id: Admission(
            blocking=row.blocking,
            status=row.capture.get(
                "status",
                eligibility.ADMISSION_NOT_BLOCKING,
            ),
            contract_version=row.contract_version,
            required=tuple(row.capture.get("required", ())),
            missing=tuple(row.capture.get("missing", ())),
            retroactive=row.retroactive,
            covering=dict(row.capture.get("covering", {})),
        )
        for event_id, row in load_admissions(db, plan_version).items()
    }


# ---------------------------------------------------------------------------
# 追溯审批
# ---------------------------------------------------------------------------


def decide_retroactive(
    db: Session,
    *,
    plan_version: str,
    event_id: str,
    decision: str,
    approver_id: str,
    reason: str,
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    if decision not in ("approved", "denied"):
        raise RetroValidationError("decision 必须是 approved 或 denied")
    if not approver_id.strip():
        raise RetroValidationError("审批人不能为空")
    reason = reason.strip()
    if not reason:
        raise RetroValidationError("追溯审批必须填写理由")

    admission_row = get_admission(db, plan_version, event_id)
    if admission_row is None:
        raise AdmissionNotFoundError(
            f"事件 '{event_id}' 没有资格快照（可能不是签到事件）"
        )
    if not admission_row.blocking:
        raise RetroValidationError("该签到不受前置条件约束，无需追溯审批")
    if not admission_row.capture.get("missing"):
        raise RetroValidationError(
            "该签到发生时前置条件齐备，不属于缺项待定，无需追溯审批"
        )

    existing = get_retro_approval(db, plan_version, event_id)
    if existing is not None:
        raise RetroConflictError(
            f"事件 '{event_id}' 已存在追溯决定: {existing.decision}"
        )

    capture = admission_row.capture
    start = datetime.fromisoformat(capture["window_start"])
    end = datetime.fromisoformat(capture["window_end"])
    required = list(capture.get("required", []))

    # 按当前材料对活动完整窗口重新校验：补证材料覆盖了窗口才能追溯计入，
    # 过期材料绝不自动延长。
    revalidation = evaluate_window(
        _student_material_views(db, plan_version, admission_row.student_id),
        required,
        start,
        end,
    )

    if decision == "approved" and revalidation["missing"]:
        raise RetroValidationError(
            "前置条件仍不完整，不能追溯计入；缺失项: "
            + ", ".join(revalidation["missing"])
        )

    row = insert_retro_approval(
        db,
        plan_version=plan_version,
        event_id=event_id,
        student_id=admission_row.student_id,
        decision=decision,
        approver_id=approver_id.strip(),
        reason=reason,
        revalidation=revalidation,
    )
    if row is None:
        raise RetroConflictError(
            f"事件 '{event_id}' 的追溯决定已被并发请求创建"
        )

    return {
        "plan_version": plan_version,
        "event_id": event_id,
        "student_id": row.student_id,
        "decision": row.decision,
        "approver_id": row.approver_id,
        "reason": row.reason,
        "revalidation": row.revalidation,
        "created_at": ensure_utc(row.created_at).isoformat().replace("+00:00", "Z"),
    }


def get_retroactive_decision(
    db: Session, plan_version: str, event_id: str
) -> dict[str, Any] | None:
    row = get_retro_approval(db, plan_version, event_id)
    if row is None:
        return None
    return {
        "plan_version": row.plan_version,
        "event_id": row.event_id,
        "student_id": row.student_id,
        "decision": row.decision,
        "approver_id": row.approver_id,
        "reason": row.reason,
        "revalidation": row.revalidation,
        "created_at": ensure_utc(row.created_at).isoformat().replace("+00:00", "Z"),
    }
