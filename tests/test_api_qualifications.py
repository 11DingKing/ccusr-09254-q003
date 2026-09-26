"""合同前置条件、资格快照、补证与追溯审批的 API 测试。"""

from __future__ import annotations

import threading
from datetime import datetime

from tests.conftest import SHANGHAI_PLAN, TestSessionLocal

PV = SHANGHAI_PLAN["plan_version"]
TYPES = ["insurance", "nda", "safety_training"]


def _setup_plan_and_contract(client, required_seconds: int = 3600):
    plan = dict(SHANGHAI_PLAN)
    plan["required_seconds"] = required_seconds
    resp = client.post("/api/plans", json=plan)
    assert resp.status_code == 201, resp.text
    resp = client.post(
        f"/api/plans/{PV}/contract",
        json={"contract_version": "C-2024-1", "required_types": TYPES},
    )
    assert resp.status_code == 201, resp.text


def _checkin(eid, student, start, end, activity_type="regular"):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": "A1",
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
    }


def _material(
    client,
    student,
    mtype,
    version,
    valid_from,
    valid_to,
    registered_by="registrar-1",
):
    return client.post(
        "/api/materials",
        json={
            "student_id": student,
            "material_type": mtype,
            "version": version,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "registered_by": registered_by,
            "note": "",
        },
    )


def _register_all_materials(client, student, *, start, end):
    for mtype in TYPES:
        resp = _material(client, student, mtype, 1, start, end)
        assert resp.status_code == 201, resp.text


def test_contract_requires_all_three_material_types(client):
    plan = dict(SHANGHAI_PLAN)
    client.post("/api/plans", json=plan)
    # 缺类型的合同被拒绝（400：企业实训三项前置条件缺一不可）
    resp = client.post(
        f"/api/plans/{PV}/contract",
        json={"contract_version": "C-X", "required_types": ["insurance"]},
    )
    assert resp.status_code == 400, resp.text

    # 合法合同登记成功
    resp = client.post(
        f"/api/plans/{PV}/contract",
        json={"contract_version": "C-2024-1", "required_types": TYPES},
    )
    assert resp.status_code == 201, resp.text

    # 同一培养方案重复登记合同被拒绝（409：合同版本不可变）
    resp = client.post(
        f"/api/plans/{PV}/contract",
        json={"contract_version": "C-2024-2", "required_types": TYPES},
    )
    assert resp.status_code == 409, resp.text


def test_complete_qualification_checkin_counts_directly(client):
    _setup_plan_and_contract(client)
    start, end = "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
    _register_all_materials(client, "S1", start=start, end=end)
    resp = client.post(
        f"/api/plans/{PV}/events",
        json={"events": [_checkin("E-01", "S1", start, end)]},
    )
    assert resp.status_code == 201, resp.text
    quals = resp.json()["qualifications"]
    assert quals[0]["status"] == "qualified"
    assert quals[0]["complete"] is True

    progress = client.get(f"/api/plans/{PV}/students/S1/progress").json()
    assert progress["total_seconds"] == 7200
    assert progress["pending_seconds"] == 0
    checkin = progress["checkins"][0]
    assert checkin["status"] == "CONFIRMED"
    # 事件发生时固定的合同与材料版本
    pinned = checkin["qualification"]
    assert pinned["contract_version"] == "C-2024-1"
    assert {e["material_type"] for e in pinned["entries"]} == set(TYPES)
    assert all(e["version"] == 1 for e in pinned["entries"])


def test_missing_material_keeps_checkin_pending_and_opens_case(client):
    _setup_plan_and_contract(client)
    start, end = "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
    # 只登记保险与保密协议，缺安全培训
    _material(client, "S1", "insurance", 1, start, end)
    _material(client, "S1", "nda", 1, start, end)

    resp = client.post(
        f"/api/plans/{PV}/events",
        json={"events": [_checkin("E-10", "S1", start, end)]},
    )
    quals = resp.json()["qualifications"]
    assert quals[0]["status"] == "pending_supplement"
    assert quals[0]["missing_types"] == ["safety_training"]
    assert quals[0]["case_id"] == "CASE-E-10"

    # 签到不丢弃：进入待定，学时不计入
    progress = client.get(f"/api/plans/{PV}/students/S1/progress").json()
    assert progress["total_seconds"] == 0
    assert progress["pending_seconds"] == 7200
    checkin = progress["checkins"][0]
    assert checkin["status"] == "PENDING"
    assert "qualification_incomplete" in checkin["pending_reasons"]

    cases = client.get(f"/api/plans/{PV}/supplement-cases").json()
    assert len(cases) == 1
    assert cases[0]["case_id"] == "CASE-E-10"
    assert cases[0]["status"] == "open"
    # 案件快照保留事件发生时的缺口证据
    assert cases[0]["snapshot"]["missing_types"] == ["safety_training"]


def test_pinned_snapshot_immutable_on_reimport(client):
    _setup_plan_and_contract(client)
    start, end = "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
    client.post(
        f"/api/plans/{PV}/events",
        json={"events": [_checkin("E-20", "S1", start, end)]},
    )
    # 事后补齐材料
    _register_all_materials(client, "S1", start=start, end=end)
    # 重复导入：事件幂等，固定快照不重算
    resp = client.post(
        f"/api/plans/{PV}/events",
        json={"events": [_checkin("E-20", "S1", start, end)]},
    )
    assert resp.json()["duplicates"] == ["E-20"]
    detail = client.get(
        f"/api/plans/{PV}/checkins/E-20/qualification"
    ).json()
    assert detail["pinned_at_event"]["complete"] is False
    assert detail["current_evaluation"]["complete"] is True


def test_material_expiring_midnight_does_not_cover_overnight_activity(client):
    """跨日活动：材料在活动结束前到期即视为缺口。"""
    _setup_plan_and_contract(client)
    # 22:00 到次日 02:00（上海时间）
    start, end = "2024-03-15T22:00:00+08:00", "2024-03-16T02:00:00+08:00"
    # 保险与 NDA 覆盖全程
    _material(client, "S1", "insurance", 1, start, end)
    _material(client, "S1", "nda", 1, start, end)
    # 安全培训在当地午夜到期（= 2024-03-15T16:00Z），早于签到结束 18:00Z
    _material(
        client,
        "S1",
        "safety_training",
        1,
        "2024-03-01T00:00:00+08:00",
        "2024-03-16T00:00:00+08:00",
    )
    client.post(
        f"/api/plans/{PV}/events",
        json={"events": [_checkin("E-30", "S1", start, end)]},
    )
    progress = client.get(f"/api/plans/{PV}/students/S1/progress").json()
    assert progress["pending_seconds"] == 4 * 3600
    assert progress["total_seconds"] == 0
    detail = client.get(
        f"/api/plans/{PV}/checkins/E-30/qualification"
    ).json()
    entry = {
        e["material_type"]: e
        for e in detail["pinned_at_event"]["entries"]
    }
    assert entry["safety_training"]["covers_interval"] is False
    assert entry["insurance"]["covers_interval"] is True


def test_late_material_does_not_auto_extend_requires_explicit_approval(client):
    """过期/事后材料不得自动延长计入；必须由授权人员显式追溯批准。"""
    _setup_plan_and_contract(client)
    start, end = "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
    client.post(
        f"/api/plans/{PV}/events",
        json={"events": [_checkin("E-40", "S1", start, end)]},
    )
    # 事后登记的材料有效期晚于活动
    late_start, late_end = "2024-03-20T00:00:00+08:00", "2024-12-31T23:59:00+08:00"
    _register_all_materials(client, "S1", start=late_start, end=late_end)

    # 当前重评仍然不覆盖该活动，签到保持待定（不自动延长）
    progress = client.get(f"/api/plans/{PV}/students/S1/progress").json()
    assert progress["total_seconds"] == 0
    assert progress["pending_seconds"] == 7200

    # 未确认缺口时批准被拒绝
    resp = client.post(
        f"/api/plans/{PV}/supplement-cases/CASE-E-40/decision",
        json={
            "approved": True,
            "actor_id": "u1",
            "actor_role": "compliance_officer",
            "reason": "documents arrived late",
        },
    )
    assert resp.status_code == 409

    # 显式承认缺口后追溯计入
    resp = client.post(
        f"/api/plans/{PV}/supplement-cases/CASE-E-40/decision",
        json={
            "approved": True,
            "actor_id": "u1",
            "actor_role": "compliance_officer",
            "reason": "documents arrived late",
            "gap_acknowledged": True,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"

    progress = client.get(f"/api/plans/{PV}/students/S1/progress").json()
    assert progress["total_seconds"] == 7200
    checkin = progress["checkins"][0]
    assert checkin["status"] == "CONFIRMED"
    assert checkin["retro_approval"]["approver_id"] == "u1"
    assert checkin["retro_approval"]["gap_acknowledged"] is True


def test_backdated_covering_materials_allow_retro_approval_without_gap(client):
    """补证材料覆盖原活动区间时，授权人员可直接追溯计入（无缺口）。"""
    _setup_plan_and_contract(client)
    start, end = "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
    client.post(
        f"/api/plans/{PV}/events",
        json={"events": [_checkin("E-45", "S1", start, end)]},
    )
    assert client.get(f"/api/plans/{PV}/students/S1/progress").json()[
        "total_seconds"
    ] == 0

    # 事后补登记，有效期回溯覆盖活动当天
    cover_from, cover_to = "2024-03-01T00:00:00+08:00", "2024-12-31T23:59:00+08:00"
    _register_all_materials(client, "S1", start=cover_from, end=cover_to)

    # 补齐本身不自动计入
    progress = client.get(f"/api/plans/{PV}/students/S1/progress").json()
    assert progress["total_seconds"] == 0
    assert progress["pending_seconds"] == 7200

    # 授权人员确认追溯计入，无需承认缺口
    resp = client.post(
        f"/api/plans/{PV}/supplement-cases/CASE-E-45/decision",
        json={
            "approved": True,
            "actor_id": "u1",
            "actor_role": "compliance_officer",
            "reason": "documents backdated and verified",
        },
    )
    assert resp.status_code == 200, resp.text
    progress = client.get(f"/api/plans/{PV}/students/S1/progress").json()
    assert progress["total_seconds"] == 7200
    assert progress["pending_seconds"] == 0
    checkin = progress["checkins"][0]
    assert checkin["pending_reasons"] == ["retroactively_approved"]


def test_retro_approval_authorized_roles_only(client):
    _setup_plan_and_contract(client)
    start, end = "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
    client.post(
        f"/api/plans/{PV}/events",
        json={"events": [_checkin("E-50", "S1", start, end)]},
    )
    resp = client.post(
        f"/api/plans/{PV}/supplement-cases/CASE-E-50/decision",
        json={
            "approved": True,
            "actor_id": "m1",
            "actor_role": "mentor",
            "reason": "I allow it",
            "gap_acknowledged": True,
        },
    )
    assert resp.status_code == 403


def test_reject_decision_keeps_pending_and_case_is_terminal(client):
    _setup_plan_and_contract(client)
    start, end = "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
    client.post(
        f"/api/plans/{PV}/events",
        json={"events": [_checkin("E-60", "S1", start, end)]},
    )
    first = client.post(
        f"/api/plans/{PV}/supplement-cases/CASE-E-60/decision",
        json={
            "approved": False,
            "actor_id": "u1",
            "actor_role": "admin",
            "reason": "forged document",
        },
    )
    assert first.status_code == 200
    assert first.json()["status"] == "rejected"

    # 学时永不计入
    progress = client.get(f"/api/plans/{PV}/students/S1/progress").json()
    assert progress["total_seconds"] == 0
    assert progress["pending_seconds"] == 7200

    # 终局不可更改
    again = client.post(
        f"/api/plans/{PV}/supplement-cases/CASE-E-60/decision",
        json={
            "approved": True,
            "actor_id": "u1",
            "actor_role": "admin",
            "gap_acknowledged": True,
        },
    )
    assert again.status_code == 409


def test_revoked_material_uncounts_and_overrides_prior_approval(client):
    """撤销解释：撤销固定快照引用的材料后，即使已追溯批准也回到待定。"""
    _setup_plan_and_contract(client)
    start, end = "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
    _register_all_materials(client, "S1", start=start, end=end)
    client.post(
        f"/api/plans/{PV}/events",
        json={"events": [_checkin("E-70", "S1", start, end)]},
    )
    assert client.get(f"/api/plans/{PV}/students/S1/progress").json()[
        "total_seconds"
    ] == 7200

    # 撤销保险
    resp = client.post(
        "/api/materials/S1/insurance/1/revoke",
        json={"revoked_by": "auditor-1", "reason": "policy cancelled"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "revoked"

    progress = client.get(f"/api/plans/{PV}/students/S1/progress").json()
    assert progress["total_seconds"] == 0
    assert progress["pending_seconds"] == 7200
    checkin = progress["checkins"][0]
    assert "material_revoked" in checkin["pending_reasons"]

    # 案件被自动立案；且固定材料被撤销时禁止追溯批准
    cases = client.get(
        f"/api/plans/{PV}/supplement-cases", params={"status": "open"}
    ).json()
    assert any(c["checkin_event_id"] == "E-70" for c in cases)
    case_id = next(c["case_id"] for c in cases if c["checkin_event_id"] == "E-70")
    forbidden = client.post(
        f"/api/plans/{PV}/supplement-cases/{case_id}/decision",
        json={
            "approved": True,
            "actor_id": "u1",
            "actor_role": "admin",
            "gap_acknowledged": True,
        },
    )
    assert forbidden.status_code == 409

    detail = client.get(
        f"/api/plans/{PV}/checkins/E-70/qualification"
    ).json()
    assert detail["replay"]["status"] == "PENDING"
    assert "material_revoked" in detail["replay"]["pending_reasons"]


def test_freeze_preserves_qualification_but_live_replay_reflects_revoke(client):
    """冻结解释：冻结快照保留撤销前的计入状态，实时重放反映撤销。"""
    _setup_plan_and_contract(client)
    start, end = "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
    _register_all_materials(client, "S1", start=start, end=end)
    client.post(
        f"/api/plans/{PV}/events",
        json={"events": [_checkin("E-80", "S1", start, end)]},
    )
    f1 = client.post(f"/api/plans/{PV}/freezes/F-Q1", json={})
    assert f1.status_code == 201
    assert f1.json()["students"][0]["total_seconds"] == 7200

    client.post(
        "/api/materials/S1/nda/1/revoke",
        json={"revoked_by": "auditor-1", "reason": "nda superseded by court order"},
    )

    # 冻结快照不变
    frozen = client.get(f"/api/plans/{PV}/freezes/F-Q1").json()
    assert frozen["students"][0]["total_seconds"] == 7200
    frozen_checkin = frozen["students"][0]["checkins"][0]
    assert frozen_checkin["status"] == "CONFIRMED"
    assert frozen_checkin["qualification"]["contract_version"] == "C-2024-1"

    # 实时快照反映撤销
    live = client.get(f"/api/plans/{PV}/snapshot").json()
    assert live["students"][0]["total_seconds"] == 0
    assert live["students"][0]["pending_seconds"] == 7200

    # 新冻结记录待定状态，diff 解释变化
    client.post(f"/api/plans/{PV}/freezes/F-Q2", json={})
    diff = client.get(
        f"/api/plans/{PV}/freezes/F-Q1/diff/F-Q2"
    ).json()
    fields = diff["student_changes"][0]["fields"]
    assert fields["total_seconds"]["before"] == 7200
    assert fields["total_seconds"]["after"] == 0


def test_concurrent_material_registration_only_one_wins(client):
    from app import services

    _setup_plan_and_contract(client)
    outcomes: list[bool] = []
    lock = threading.Lock()

    def _register():
        session = TestSessionLocal()
        try:
            try:
                services.register_material(
                    session,
                    student_id="S9",
                    material_type="insurance",
                    version=1,
                    valid_from=datetime.fromisoformat(
                        "2024-03-01T00:00:00+08:00"
                    ),
                    valid_to=datetime.fromisoformat(
                        "2024-12-31T00:00:00+08:00"
                    ),
                    registered_by="r",
                )
                ok = True
            except services.MaterialConflictError:
                ok = False
            with lock:
                outcomes.append(ok)
        finally:
            session.close()

    threads = [threading.Thread(target=_register) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(outcomes) == 1
    assert sum(1 for o in outcomes if not o) == 3
    materials = client.get("/api/materials", params={"student_id": "S9"}).json()
    assert len(materials) == 1
    assert materials[0]["version"] == 1


def test_concurrent_case_decision_only_one_wins(client):
    from app import services

    _setup_plan_and_contract(client)
    start, end = "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"
    client.post(
        f"/api/plans/{PV}/events",
        json={"events": [_checkin("E-90", "S1", start, end)]},
    )
    statuses: list[str] = []
    lock = threading.Lock()

    def _decide(approved: bool):
        session = TestSessionLocal()
        try:
            try:
                result = services.decide_supplement_case(
                    session,
                    plan_version=PV,
                    case_id="CASE-E-90",
                    approved=approved,
                    actor_id="officer",
                    actor_role="compliance_officer",
                    reason="concurrent",
                    gap_acknowledged=True,
                )
                status = result["status"]
            except services.CaseError:
                status = "conflict"
            with lock:
                statuses.append(status)
        finally:
            session.close()

    threads = [
        threading.Thread(target=_decide, args=(True,)),
        threading.Thread(target=_decide, args=(True,)),
        threading.Thread(target=_decide, args=(False,)),
        threading.Thread(target=_decide, args=(False,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 只有一个线程成功终局，其余全部冲突
    terminal = [s for s in statuses if s in ("approved", "rejected")]
    assert len(terminal) == 1
    assert statuses.count("conflict") == 3
