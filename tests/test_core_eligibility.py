"""资格评估纯函数测试：窗口覆盖、跨日到期、状态解释。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.eligibility import (
    MaterialView,
    STATUS_ACTIVE,
    STATUS_REVOKED,
    STATUS_SUPERSEDED,
    admission_from_capture,
    capture_admission,
    evaluate_current,
    evaluate_window,
)

REQUIRED = ["insurance", "confidentiality_agreement", "safety_training"]
SH_TZ = "+08:00"


def _material(
    mid: int,
    requirement: str,
    valid_from: str,
    valid_to: str | None,
    *,
    status: str = STATUS_ACTIVE,
) -> MaterialView:
    return MaterialView(
        id=mid,
        version=1,
        requirement=requirement,
        document_ref=f"doc-{mid}",
        valid_from=datetime.fromisoformat(valid_from),
        valid_to=datetime.fromisoformat(valid_to) if valid_to else None,
        status=status,
    )


def _all_three(valid_from: str, valid_to: str | None) -> list[MaterialView]:
    return [
        _material(1, "insurance", valid_from, valid_to),
        _material(2, "confidentiality_agreement", valid_from, valid_to),
        _material(3, "safety_training", valid_from, valid_to),
    ]


def test_window_fully_covered_is_eligible():
    materials = _all_three("2024-01-01T00:00:00+08:00", "2024-12-31T23:59:59+08:00")
    result = evaluate_window(
        materials,
        REQUIRED,
        datetime.fromisoformat("2024-03-15T08:00:00+08:00"),
        datetime.fromisoformat("2024-03-15T12:00:00+08:00"),
    )
    assert result["status"] == "eligible"
    assert result["missing"] == []
    assert set(result["covering"]) == set(REQUIRED)


def test_material_expiring_during_cross_day_activity_does_not_cover():
    # 材料在活动跨日的次日午夜前到期：不覆盖整个窗口，过期不自动延长。
    materials = _all_three("2024-01-01T00:00:00+08:00", "2024-03-15T23:59:59+08:00")
    result = evaluate_window(
        materials,
        REQUIRED,
        datetime.fromisoformat("2024-03-15T22:00:00+08:00"),
        datetime.fromisoformat("2024-03-16T02:00:00+08:00"),
    )
    assert result["status"] == "deficient"
    assert set(result["missing"]) == set(REQUIRED)


def test_valid_to_equal_window_end_still_covers():
    end = "2024-03-15T12:00:00+08:00"
    materials = _all_three("2024-01-01T00:00:00+08:00", end)
    result = evaluate_window(
        materials,
        REQUIRED,
        datetime.fromisoformat("2024-03-15T08:00:00+08:00"),
        datetime.fromisoformat(end),
    )
    assert result["missing"] == []


def test_valid_one_second_before_window_end_does_not_cover():
    materials = _all_three("2024-01-01T00:00:00+08:00", "2024-03-15T11:59:59+08:00")
    result = evaluate_window(
        materials,
        REQUIRED,
        datetime.fromisoformat("2024-03-15T08:00:00+08:00"),
        datetime.fromisoformat("2024-03-15T12:00:00+08:00"),
    )
    assert set(result["missing"]) == set(REQUIRED)


def test_not_yet_effective_material_does_not_cover():
    materials = _all_three("2024-03-16T00:00:00+08:00", None)
    result = evaluate_window(
        materials,
        REQUIRED,
        datetime.fromisoformat("2024-03-15T08:00:00+08:00"),
        datetime.fromisoformat("2024-03-15T12:00:00+08:00"),
    )
    assert set(result["missing"]) == set(REQUIRED)


def test_revoked_material_does_not_cover():
    materials = [
        _material(1, "insurance", "2024-01-01T00:00:00+08:00", None, status=STATUS_REVOKED),
        _material(2, "confidentiality_agreement", "2024-01-01T00:00:00+08:00", None),
        _material(3, "safety_training", "2024-01-01T00:00:00+08:00", None),
    ]
    result = evaluate_window(
        materials,
        REQUIRED,
        datetime.fromisoformat("2024-03-15T08:00:00+08:00"),
        datetime.fromisoformat("2024-03-15T12:00:00+08:00"),
    )
    assert result["missing"] == ["insurance"]


def test_capture_pins_material_versions_at_event_time():
    materials = _all_three("2024-01-01T00:00:00+08:00", "2024-12-31T23:59:59+08:00")
    capture = capture_admission(
        materials,
        REQUIRED,
        datetime.fromisoformat("2024-03-15T08:00:00+08:00"),
        datetime.fromisoformat("2024-03-15T12:00:00+08:00"),
        blocking=True,
        contract_version="C-1",
        evaluated_at=datetime(2024, 3, 15, 0, 1, tzinfo=timezone.utc),
    )
    assert capture["status"] == "eligible"
    assert capture["blocking"] is True
    assert capture["contract_version"] == "C-1"
    # 固定了当时的全部材料版本；事后撤销/补证不改变这份快照。
    assert len(capture["materials"]) == 3
    pinned = capture["covering"]["insurance"]
    assert pinned["material_id"] == 1 and pinned["version"] == 1

    # 重放入口直接消费快照。
    admission = admission_from_capture(capture)
    assert admission.missing == () and admission.retroactive is None


def test_capture_for_non_blocking_activity_is_marked_not_blocking():
    materials: list[MaterialView] = []
    capture = capture_admission(
        materials,
        REQUIRED,
        datetime.fromisoformat("2024-03-15T08:00:00+08:00"),
        datetime.fromisoformat("2024-03-15T12:00:00+08:00"),
        blocking=False,
        contract_version="C-1",
        evaluated_at=datetime(2024, 3, 15, tzinfo=timezone.utc),
    )
    assert capture["status"] == "not_blocking"
    assert capture["missing"] == REQUIRED


def test_current_eligibility_explains_each_requirement_state():
    materials = [
        # 覆盖中
        _material(1, "insurance", "2024-01-01T00:00:00+08:00", "2024-12-31T23:59:59+08:00"),
        # 已过期
        _material(2, "confidentiality_agreement", "2024-01-01T00:00:00+08:00", "2024-02-01T00:00:00+08:00"),
        # 已撤销
        _material(3, "safety_training", "2024-01-01T00:00:00+08:00", None, status=STATUS_REVOKED),
    ]
    result = evaluate_current(
        materials, REQUIRED, datetime.fromisoformat("2024-06-01T00:00:00+08:00")
    )
    by_code = {row["requirement"]: row["status"] for row in result["requirements"]}
    assert by_code == {
        "insurance": "covered",
        "confidentiality_agreement": "expired",
        "safety_training": "revoked",
    }
    assert result["eligible"] is False
    assert set(result["missing"]) == {"confidentiality_agreement", "safety_training"}


def test_current_eligibility_distinguishes_not_yet_effective():
    materials = [
        _material(1, "insurance", "2024-01-01T00:00:00+08:00", None),
        _material(2, "confidentiality_agreement", "2024-12-01T00:00:00+08:00", None),
        _material(3, "safety_training", "2024-01-01T00:00:00+08:00", None),
    ]
    result = evaluate_current(
        materials, REQUIRED, datetime.fromisoformat("2024-06-01T00:00:00+08:00")
    )
    by_code = {row["requirement"]: row["status"] for row in result["requirements"]}
    assert by_code["confidentiality_agreement"] == "not_yet_effective"
    assert result["eligible"] is False


def test_superseded_material_is_reported_as_superseded():
    materials = [
        _material(1, "insurance", "2024-01-01T00:00:00+08:00", None, status=STATUS_SUPERSEDED),
    ]
    result = evaluate_current(
        materials, ["insurance"], datetime.fromisoformat("2024-06-01T00:00:00+08:00")
    )
    assert result["requirements"][0]["status"] == "superseded"
    assert result["eligible"] is False


def test_window_rejects_inverted_interval():
    with pytest.raises(ValueError):
        evaluate_window(
            [],
            REQUIRED,
            datetime.fromisoformat("2024-03-15T12:00:00+08:00"),
            datetime.fromisoformat("2024-03-15T08:00:00+08:00"),
        )
