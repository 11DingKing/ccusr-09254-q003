"""前置条件（保险、保密协议、安全培训）资格评估纯函数。

评估结果在签到事件导入时一次性固化，之后材料的登记、撤销、补证都不会
改变历史快照；是否追溯计入由独立的审批决定驱动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

# 企业实训合同可要求的前置条件代码。
REQUIREMENT_CODES: tuple[str, ...] = (
    "insurance",
    "confidentiality_agreement",
    "safety_training",
)

REQUIREMENT_LABELS: Mapping[str, str] = {
    "insurance": "保险",
    "confidentiality_agreement": "保密协议",
    "safety_training": "安全培训",
}

# 材料生命周期状态。
STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"
STATUS_SUPERSEDED = "superseded"
VALID_STATUSES = frozenset(
    {STATUS_ACTIVE, STATUS_REVOKED, STATUS_SUPERSEDED}
)

# 资格评估结论。
ADMISSION_ELIGIBLE = "eligible"
ADMISSION_DEFICIENT = "deficient"
ADMISSION_NOT_BLOCKING = "not_blocking"

# 追溯审批结论。
RETRO_APPROVED = "approved"
RETRO_DENIED = "denied"


def ensure_utc(value: datetime) -> datetime:
    """把时间统一为 UTC；naive 时间按 UTC 解释。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def validate_requirement_code(code: str) -> str:
    if code not in REQUIREMENT_CODES:
        raise ValueError(
            f"未知前置条件代码 '{code}'，可选值: {', '.join(REQUIREMENT_CODES)}"
        )
    return code


@dataclass(frozen=True)
class MaterialView:
    """评估时使用的材料只读视图（固定版本与有效期）。"""

    id: int
    version: int
    requirement: str
    document_ref: str
    valid_from: datetime
    valid_to: datetime | None
    status: str
    supersedes_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "material_id": self.id,
            "version": self.version,
            "requirement": self.requirement,
            "document_ref": self.document_ref,
            "valid_from": _iso(self.valid_from),
            "valid_to": _iso(self.valid_to),
            "status": self.status,
            "supersedes_id": self.supersedes_id,
        }


@dataclass(frozen=True)
class Admission:
    """签到事件的准入结论，随重放注入。"""

    blocking: bool
    status: str
    contract_version: str | None = None
    required: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    retroactive: str | None = None  # approved / denied / None
    covering: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocking": self.blocking,
            "status": self.status,
            "contract_version": self.contract_version,
            "required": list(self.required),
            "missing": list(self.missing),
            "retroactive": self.retroactive,
            "covering": {code: dict(mat) for code, mat in self.covering.items()},
        }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return ensure_utc(value).isoformat().replace("+00:00", "Z")


def _covers_window(
    material: MaterialView, start_utc: datetime, end_utc: datetime
) -> bool:
    """材料的有效期必须完整覆盖整个活动区间。

    跨日活动在材料到期日当天午夜之后结束时，材料不再覆盖——过期材料
    *不会* 被自动延长。
    """
    if material.status != STATUS_ACTIVE:
        return False
    if ensure_utc(material.valid_from) > start_utc:
        return False
    if material.valid_to is not None and ensure_utc(material.valid_to) < end_utc:
        return False
    return True


def _covers_at(material: MaterialView, moment_utc: datetime) -> bool:
    if material.status != STATUS_ACTIVE:
        return False
    if ensure_utc(material.valid_from) > moment_utc:
        return False
    if material.valid_to is not None and ensure_utc(material.valid_to) <= moment_utc:
        return False
    return True


def _pick_covering(
    materials: Sequence[MaterialView],
    code: str,
    start_utc: datetime,
    end_utc: datetime,
) -> MaterialView | None:
    candidates = [
        m
        for m in materials
        if m.requirement == code and _covers_window(m, start_utc, end_utc)
    ]
    if not candidates:
        return None
    # 编号最大即最新登记的一份，保证选择确定。
    return max(candidates, key=lambda m: (m.id, m.version))


def evaluate_window(
    materials: Iterable[MaterialView],
    required_codes: Sequence[str],
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """按活动窗口评估前置条件是否齐备。"""
    start_utc = ensure_utc(start)
    end_utc = ensure_utc(end)
    if end_utc <= start_utc:
        raise ValueError("活动结束时间必须晚于开始时间")

    material_list = list(materials)
    missing: list[str] = []
    covering: dict[str, dict[str, Any]] = {}
    for code in required_codes:
        chosen = _pick_covering(material_list, code, start_utc, end_utc)
        if chosen is None:
            missing.append(code)
        else:
            covering[code] = chosen.to_dict()

    return {
        "status": ADMISSION_DEFICIENT if missing else ADMISSION_ELIGIBLE,
        "missing": missing,
        "covering": covering,
        "window_start": _iso(start_utc),
        "window_end": _iso(end_utc),
    }


def capture_admission(
    materials: Iterable[MaterialView],
    required_codes: Sequence[str],
    start: datetime,
    end: datetime,
    *,
    blocking: bool,
    contract_version: str | None,
    evaluated_at: datetime,
) -> dict[str, Any]:
    """生成事件发生时固定的资格快照（含被固定的材料版本）。"""
    material_list = list(materials)
    result = evaluate_window(material_list, required_codes, start, end)
    return {
        "blocking": blocking,
        "status": (
            result["status"] if blocking else ADMISSION_NOT_BLOCKING
        ),
        "contract_version": contract_version,
        "required": list(required_codes),
        "missing": result["missing"],
        "covering": result["covering"],
        "window_start": result["window_start"],
        "window_end": result["window_end"],
        "evaluated_at": _iso(ensure_utc(evaluated_at)),
        "materials": [m.to_dict() for m in sorted(material_list, key=lambda m: m.id)],
    }


def admission_from_capture(
    capture: Mapping[str, Any], retroactive: str | None = None
) -> Admission:
    return Admission(
        blocking=bool(capture.get("blocking")),
        status=str(capture.get("status", ADMISSION_NOT_BLOCKING)),
        contract_version=capture.get("contract_version"),
        required=tuple(capture.get("required", ())),
        missing=tuple(capture.get("missing", ())),
        retroactive=retroactive,
        covering=dict(capture.get("covering", {})),
    )


def evaluate_current(
    materials: Iterable[MaterialView],
    required_codes: Sequence[str],
    moment: datetime,
) -> dict[str, Any]:
    """评估某一时点（默认当前）每份前置材料的状态，供资格查询使用。"""
    moment_utc = ensure_utc(moment)
    material_list = list(materials)
    rows: list[dict[str, Any]] = []
    missing: list[str] = []

    for code in required_codes:
        scoped = [m for m in material_list if m.requirement == code]
        active = [m for m in scoped if m.status == STATUS_ACTIVE]
        covering = [m for m in active if _covers_at(m, moment_utc)]
        if covering:
            chosen = max(covering, key=lambda m: (m.id, m.version))
            rows.append(
                {"requirement": code, "status": "covered", "material": chosen.to_dict()}
            )
            continue

        missing.append(code)
        if not scoped:
            detail_status = "missing"
            chosen = None
        elif active:
            # 最新一份有效登记材料当前不在覆盖区间内。
            chosen = max(active, key=lambda m: (m.id, m.version))
            if chosen.valid_to is not None and ensure_utc(chosen.valid_to) <= moment_utc:
                detail_status = "expired"
            else:
                detail_status = "not_yet_effective"
        else:
            chosen = max(scoped, key=lambda m: (m.id, m.version))
            detail_status = chosen.status  # revoked / superseded
        rows.append(
            {"requirement": code, "status": detail_status, "material": chosen.to_dict() if chosen else None}
        )

    return {
        "evaluated_at": _iso(moment_utc),
        "requirements": rows,
        "missing": missing,
        "eligible": not missing,
    }
