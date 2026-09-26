"""企业实训前置资格（保险、保密协议、安全培训）的纯领域逻辑。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Iterable, Sequence

from .clock import to_utc

#: 企业实训项目要求的前置材料类型
MATERIAL_TYPES: tuple[str, ...] = ("insurance", "nda", "safety_training")


class MaterialType(StrEnum):
    INSURANCE = "insurance"
    NDA = "nda"
    SAFETY_TRAINING = "safety_training"


class MaterialStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


@dataclass(frozen=True)
class MaterialVersion:
    """一份登记材料的某个不可变版本。"""

    student_id: str
    material_type: str
    version: int
    valid_from: datetime
    valid_to: datetime
    revoked: bool = False

    def normalized(self) -> "MaterialVersion":
        return MaterialVersion(
            student_id=self.student_id,
            material_type=self.material_type,
            version=self.version,
            valid_from=to_utc(self.valid_from),
            valid_to=to_utc(self.valid_to),
            revoked=self.revoked,
        )

    def covers(self, start_utc: datetime, end_utc: datetime) -> bool:
        """材料是否完整覆盖活动区间；撤销的材料不具备覆盖效力。"""
        if self.revoked:
            return False
        return self.valid_from <= start_utc and self.valid_to >= end_utc

    def valid_at(self, moment_utc: datetime) -> bool:
        if self.revoked:
            return False
        return self.valid_from <= moment_utc <= self.valid_to


@dataclass(frozen=True)
class QualificationEntry:
    material_type: str
    present: bool
    version: int | None
    status: str
    valid_from: datetime | None
    valid_to: datetime | None
    covers_interval: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "material_type": self.material_type,
            "present": self.present,
            "version": self.version,
            "status": self.status,
            "valid_from": _iso(self.valid_from),
            "valid_to": _iso(self.valid_to),
            "covers_interval": self.covers_interval,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QualificationEntry":
        return cls(
            material_type=data["material_type"],
            present=data["present"],
            version=data.get("version"),
            status=data["status"],
            valid_from=_parse(data.get("valid_from")),
            valid_to=_parse(data.get("valid_to")),
            covers_interval=data["covers_interval"],
        )


@dataclass(frozen=True)
class QualificationEvaluation:
    required_types: tuple[str, ...]
    entries: tuple[QualificationEntry, ...]
    complete: bool
    missing_types: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_types": list(self.required_types),
            "entries": [e.to_dict() for e in self.entries],
            "complete": self.complete,
            "missing_types": list(self.missing_types),
        }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return to_utc(value).isoformat().replace("+00:00", "Z")


def _parse(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def evaluate(
    required_types: Sequence[str],
    materials: Iterable[MaterialVersion],
    start_utc: datetime,
    end_utc: datetime,
) -> QualificationEvaluation:
    """按合同要求评估材料集合是否完整覆盖活动区间。

    同一类型存在多个版本时，选取覆盖区间的最高版本；没有覆盖版本时
    退回最高版本（用于解释“过期/缺口”），但不计为满足。
    """
    start_utc = to_utc(start_utc)
    end_utc = to_utc(end_utc)
    versions = [m.normalized() for m in materials]
    by_type: dict[str, list[MaterialVersion]] = {}
    for m in versions:
        by_type.setdefault(m.material_type, []).append(m)

    entries: list[QualificationEntry] = []
    missing: list[str] = []
    complete = True
    for material_type in required_types:
        candidates = sorted(
            by_type.get(material_type, []), key=lambda m: m.version, reverse=True
        )
        active = [m for m in candidates if not m.revoked]
        chosen = next((m for m in active if m.covers(start_utc, end_utc)), None)
        if chosen is not None:
            entries.append(
                QualificationEntry(
                    material_type=material_type,
                    present=True,
                    version=chosen.version,
                    status=MaterialStatus.ACTIVE.value,
                    valid_from=chosen.valid_from,
                    valid_to=chosen.valid_to,
                    covers_interval=True,
                )
            )
            continue
        complete = False
        missing.append(material_type)
        latest = candidates[0] if candidates else None
        if latest is None:
            entries.append(
                QualificationEntry(
                    material_type=material_type,
                    present=False,
                    version=None,
                    status="missing",
                    valid_from=None,
                    valid_to=None,
                    covers_interval=False,
                )
            )
        else:
            entries.append(
                QualificationEntry(
                    material_type=material_type,
                    present=True,
                    version=latest.version,
                    status=(
                        MaterialStatus.REVOKED.value
                        if latest.revoked
                        else MaterialStatus.ACTIVE.value
                    ),
                    valid_from=latest.valid_from,
                    valid_to=latest.valid_to,
                    covers_interval=False,
                )
            )

    return QualificationEvaluation(
        required_types=tuple(required_types),
        entries=tuple(entries),
        complete=complete,
        missing_types=tuple(missing),
    )


# ---------------------------------------------------------------------------
# 签到事件发生时固定的资格快照
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PinnedQualification:
    """事件导入时固定的合同版本与前置材料版本，事后不可变。"""

    contract_version: str
    required_types: tuple[str, ...]
    complete: bool
    missing_types: tuple[str, ...]
    entries: tuple[QualificationEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "required_types": list(self.required_types),
            "complete": self.complete,
            "missing_types": list(self.missing_types),
            "entries": [e.to_dict() for e in self.entries],
        }

    @classmethod
    def from_evaluation(
        cls, contract_version: str, evaluation: QualificationEvaluation
    ) -> "PinnedQualification":
        return cls(
            contract_version=contract_version,
            required_types=evaluation.required_types,
            complete=evaluation.complete,
            missing_types=evaluation.missing_types,
            entries=evaluation.entries,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PinnedQualification":
        return cls(
            contract_version=data["contract_version"],
            required_types=tuple(data.get("required_types", ())),
            complete=data["complete"],
            missing_types=tuple(data.get("missing_types", ())),
            entries=tuple(QualificationEntry.from_dict(e) for e in data["entries"]),
        )


@dataclass(frozen=True)
class RetroApproval:
    """授权人员对缺项签到的追溯计入决定。"""

    case_id: str
    checkin_event_id: str
    approved: bool
    approver_id: str
    approver_role: str
    reason: str
    decided_at: datetime
    gap_acknowledged: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "checkin_event_id": self.checkin_event_id,
            "approved": self.approved,
            "approver_id": self.approver_id,
            "approver_role": self.approver_role,
            "reason": self.reason,
            "decided_at": _iso(self.decided_at),
            "gap_acknowledged": self.gap_acknowledged,
        }


# ---------------------------------------------------------------------------
# 重放时传入的资格上下文（由数据库行组装，核心逻辑保持纯函数）
# ---------------------------------------------------------------------------


@dataclass
class QualifierInput:
    #: 受合同约束的签到事件 id -> 固定快照
    pinned: dict[str, PinnedQualification] = field(default_factory=dict)
    #: 签到事件 id -> 按当前材料重评是否完整覆盖活动区间
    current_complete: dict[str, bool] = field(default_factory=dict)
    #: 签到事件 id -> 追溯决定
    approvals: dict[str, RetroApproval] = field(default_factory=dict)


INTERNSHIP = "internship"


def resolve_checkin(
    *,
    activity_type: str,
    mentor_confirmed: bool,
    pinned: PinnedQualification | None,
    current_complete: bool | None,
    approval: RetroApproval | None,
) -> tuple[bool, list[str]]:
    """决定一条签到是否计入，以及待定原因。

    返回 (是否计入, 原因列表)。原因列表非空即表示待定。

    * 事件时材料齐全、事后材料被撤销且无新版本覆盖 → ``material_revoked``，
      追溯批准也不能放行（作废材料不得借批准“复活”）。
    * 事件时缺项 → ``qualification_incomplete``，材料补齐不自动延长，
      须授权人员显式追溯批准（缺口未补齐时须显式承认缺口）。
    """
    if pinned is None:
        # 未登记合同的培养方案保持历史语义
        if activity_type == INTERNSHIP and not mentor_confirmed:
            return False, ["internship_awaiting_mentor"]
        return True, []

    reasons: list[str] = []
    if pinned.complete:
        if current_complete is False:
            reasons.append("material_revoked")
    else:
        reasons.append("qualification_incomplete")
    if activity_type == INTERNSHIP and not mentor_confirmed:
        reasons.append("internship_awaiting_mentor")

    if not reasons:
        return True, []
    if (
        approval is not None
        and approval.approved
        and "material_revoked" not in reasons
    ):
        return True, ["retroactively_approved"]
    return False, reasons
