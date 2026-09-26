"""资格评估纯函数测试：边界、跨日到期与撤销。"""

from __future__ import annotations

from datetime import datetime

from app.core.qualifications import (
    MATERIAL_TYPES,
    MaterialVersion,
    evaluate,
)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def test_complete_when_all_three_cover_interval():
    start = _dt("2024-03-15T00:00:00+00:00")
    end = _dt("2024-03-15T04:00:00+00:00")
    materials = [
        MaterialVersion(
            student_id="S1",
            material_type=mtype,
            version=1,
            valid_from=_dt("2024-01-01T00:00:00+00:00"),
            valid_to=_dt("2024-12-31T00:00:00+00:00"),
        )
        for mtype in MATERIAL_TYPES
    ]
    result = evaluate(MATERIAL_TYPES, materials, start, end)
    assert result.complete is True
    assert result.missing_types == ()
    assert all(e.covers_interval for e in result.entries)


def test_valid_to_boundary_equal_to_checkout_covers():
    end = _dt("2024-03-16T00:00:00+08:00")
    start = _dt("2024-03-15T22:00:00+08:00")
    materials = [
        MaterialVersion(
            student_id="S1",
            material_type=mtype,
            version=1,
            valid_from=_dt("2024-03-01T00:00:00+08:00"),
            valid_to=end,
        )
        for mtype in MATERIAL_TYPES
    ]
    result = evaluate(MATERIAL_TYPES, materials, start, end)
    assert result.complete is True


def test_expiring_one_second_before_checkout_does_not_cover():
    start = _dt("2024-03-15T22:00:00+08:00")
    end = _dt("2024-03-16T02:00:00+08:00")
    materials = []
    for mtype in MATERIAL_TYPES:
        materials.append(
            MaterialVersion(
                student_id="S1",
                material_type=mtype,
                version=1,
                valid_from=_dt("2024-03-01T00:00:00+08:00"),
                valid_to=(
                    end if mtype != "safety_training"
                    else _dt("2024-03-16T01:59:59+08:00")
                ),
            )
        )
    result = evaluate(MATERIAL_TYPES, materials, start, end)
    assert result.complete is False
    assert result.missing_types == ("safety_training",)
    by_type = {e.material_type: e for e in result.entries}
    assert by_type["safety_training"].covers_interval is False
    assert by_type["insurance"].covers_interval is True


def test_revoked_material_never_covers_even_within_validity():
    start = _dt("2024-03-15T08:00:00+00:00")
    end = _dt("2024-03-15T10:00:00+00:00")
    materials = [
        MaterialVersion(
            student_id="S1",
            material_type="insurance",
            version=1,
            valid_from=_dt("2024-01-01T00:00:00+00:00"),
            valid_to=_dt("2024-12-31T00:00:00+00:00"),
            revoked=True,
        ),
        MaterialVersion(
            student_id="S1",
            material_type="nda",
            version=1,
            valid_from=_dt("2024-01-01T00:00:00+00:00"),
            valid_to=_dt("2024-12-31T00:00:00+00:00"),
        ),
        MaterialVersion(
            student_id="S1",
            material_type="safety_training",
            version=1,
            valid_from=_dt("2024-01-01T00:00:00+00:00"),
            valid_to=_dt("2024-12-31T00:00:00+00:00"),
        ),
    ]
    result = evaluate(MATERIAL_TYPES, materials, start, end)
    assert result.complete is False
    assert result.missing_types == ("insurance",)
    insurance = next(e for e in result.entries if e.material_type == "insurance")
    assert insurance.present is True
    assert insurance.status == "revoked"
    assert insurance.covers_interval is False


def test_newer_non_covering_version_does_not_shadow_covering_old_one():
    # v2 已过期，v1 仍覆盖区间：应选 v1 而不是简单取最新版本
    start = _dt("2024-03-15T08:00:00+00:00")
    end = _dt("2024-03-15T10:00:00+00:00")
    materials = [
        MaterialVersion(
            student_id="S1",
            material_type=mtype,
            version=1,
            valid_from=_dt("2024-01-01T00:00:00+00:00"),
            valid_to=_dt("2024-12-31T00:00:00+00:00"),
        )
        for mtype in MATERIAL_TYPES
    ] + [
        MaterialVersion(
            student_id="S1",
            material_type="insurance",
            version=2,
            valid_from=_dt("2024-01-01T00:00:00+00:00"),
            valid_to=_dt("2024-02-01T00:00:00+00:00"),
        ),
    ]
    result = evaluate(MATERIAL_TYPES, materials, start, end)
    assert result.complete is True
    insurance = next(e for e in result.entries if e.material_type == "insurance")
    assert insurance.version == 1
