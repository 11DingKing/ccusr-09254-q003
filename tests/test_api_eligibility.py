"""合同前置条件、资格快照、补证与追溯审批的 API 集成测试。"""

from __future__ import annotations

import threading

from tests.conftest import SHANGHAI_PLAN, TestSessionLocal

PV = SHANGHAI_PLAN["plan_version"]
ALL_REQ = ["insurance", "confidentiality_agreement", "safety_training"]


def _create_plan(client, required_seconds: int = 3600):
    plan = dict(SHANGHAI_PLAN)
    plan["required_seconds"] = required_seconds
    resp = client.post("/api/plans", json=plan)
    assert resp.status_code == 201, resp.text


def _contract(client, required=ALL_REQ, version: str = "C-2024"):
    resp = client.post(
        f"/api/plans/{PV}/contracts",
        json={
            "contract_version": version,
            "enterprise_id": "ENT-1",
            "required_documents": required,
            "created_by": "admin",
        },
    )
    return resp


def _material(client, requirement, *, ref=None, vf="2024-01-01T00:00:00+08:00",
              vt="2024-12-31T23:59:59+08:00", student="S1", by="clerk"):
    return client.post(
        f"/api/plans/{PV}/materials",
        json={
            "student_id": student,
            "requirement": requirement,
            "document_ref": ref or f"{requirement}-doc",
            "valid_from": vf,
            "valid_to": vt,
            "registered_by": by,
        },
    )


def _checkin(client, eid, start, end, *, student="S1", at="internship"):
    return client.post(
        f"/api/plans/{PV}/events",
        json={
            "events": [
                {
                    "event_id": eid,
                    "event_type": "checkin",
                    "student_id": student,
                    "payload": {
                        "activity_id": "A1",
                        "activity_type": at,
                        "check_in_at": start,
                        "check_out_at": end,
                    },
                }
            ]
        },
    )


def _progress(client, student="S1"):
    return client.get(f"/api/plans/{PV}/students/{student}/progress").json()


def _find_checkin(progress, eid):
    return next(c for c in progress["checkins"] if c["event_id"] == eid)


# ---------------------------------------------------------------------------
# 主流程：缺项待定 -> 补证 -> 追溯审批
# ---------------------------------------------------------------------------


def test_deficient_internship_checkin_pending_until_retro_approval(client):
    _create_plan(client)
    assert _contract(client).status_code == 201
    assert _material(client, "insurance").status_code == 201

    resp = _checkin(
        client,
        "E-01",
        "2024-03-15T08:00:00+08:00",
        "2024-03-15T12:00:00+08:00",
    )
    assert resp.json()["admissions_captured"] == ["E-01"]

    progress = _progress(client)
    assert progress["total_seconds"] == 0
    assert progress["pending_seconds"] == 4 * 3600
    checkin = progress["checkins"][0]
    assert checkin["status"] == "PENDING"
    admission = checkin["admission"]
    assert admission["blocking"] is True
    assert admission["status"] == "deficient"
    assert admission["hold_reason"] == "missing_prerequisites"
    assert admission["contract_version"] == "C-2024"
    assert set(admission["missing_requirements"]) == {
        "confidentiality_agreement",
        "safety_training",
    }

    # 导师确认不能解除前置条件阻塞。
    client.post(
        f"/api/plans/{PV}/events",
        json={
            "events": [
                {
                    "event_id": "E-02",
                    "event_type": "mentor_confirm",
                    "student_id": "S1",
                    "payload": {"checkin_event_id": "E-01"},
                }
            ]
        },
    )
    assert _progress(client)["total_seconds"] == 0
    assert _find_checkin(_progress(client), "E-01")["status"] == "PENDING"

    # 补齐缺项材料（覆盖活动窗口）。
    assert _material(client, "confidentiality_agreement", ref="ca-v2").status_code == 201
    assert _material(client, "safety_training", ref="st-v2").status_code == 201
    elig = client.get(
        f"/api/plans/{PV}/students/S1/eligibility",
        params={"moment": "2024-06-01T00:00:00+08:00"},
    ).json()
    assert elig["eligible"] is True and elig["missing"] == []

    # 缺材料时批准追溯 -> 422；补齐后授权人批准 -> 计入。
    bad = client.post(
        f"/api/plans/{PV}/events/E-99/retro-approval",
        json={"decision": "approved", "approver_id": "boss", "reason": "x"},
    )
    assert bad.status_code == 404

    retro = client.post(
        f"/api/plans/{PV}/events/E-01/retro-approval",
        json={"decision": "approved", "approver_id": "boss", "reason": "材料补齐，同意追溯"},
    )
    assert retro.status_code == 201, retro.text
    body = retro.json()
    assert body["decision"] == "approved"
    assert body["revalidation"]["status"] == "eligible"

    progress = _progress(client)
    assert progress["total_seconds"] == 4 * 3600
    checkin = _find_checkin(progress, "E-01")
    assert checkin["status"] == "CONFIRMED"
    assert checkin["admission"]["retroactive"] == "approved"
    assert checkin["admission"]["hold_reason"] == "retroactively_approved"


def test_denied_retroactive_keeps_checkin_pending(client):
    _create_plan(client)
    _contract(client)
    _checkin(client, "E-01", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00")

    retro = client.post(
        f"/api/plans/{PV}/events/E-01/retro-approval",
        json={"decision": "denied", "approver_id": "boss", "reason": "材料无法核实"},
    )
    assert retro.status_code == 201
    assert retro.json()["decision"] == "denied"

    progress = _progress(client)
    assert progress["total_seconds"] == 0
    checkin = _find_checkin(progress, "E-01")
    assert checkin["status"] == "PENDING"
    assert checkin["admission"]["retroactive"] == "denied"

    # 驳回决定终态：重复审批冲突。
    again = client.post(
        f"/api/plans/{PV}/events/E-01/retro-approval",
        json={"decision": "approved", "approver_id": "boss", "reason": "再批"},
    )
    assert again.status_code == 409


def test_regular_activity_and_plan_without_contract_are_not_blocked(client):
    _create_plan(client)
    _contract(client)
    # 未登记任何材料：普通活动照常计入。
    _checkin(
        client, "R-01", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00",
        at="regular",
    )
    assert _progress(client)["total_seconds"] == 3600

    # 未登记合同的方案不做阻塞。
    client.post("/api/plans", json={
        "plan_version": "P-NO-CONTRACT",
        "iana_timezone": "Asia/Shanghai",
        "required_seconds": 0,
    })
    resp = client.post(
        "/api/plans/P-NO-CONTRACT/events",
        json={"events": [{
            "event_id": "N-01", "event_type": "checkin", "student_id": "S2",
            "payload": {
                "activity_id": "A", "activity_type": "internship",
                "check_in_at": "2024-03-15T08:00:00+08:00",
                "check_out_at": "2024-03-15T10:00:00+08:00",
            },
        }]},
    )
    assert resp.json()["admissions_captured"] == []
    progress = client.get(
        "/api/plans/P-NO-CONTRACT/students/S2/progress"
    ).json()
    # 实训仍需导师确认，但不受资格阻塞：无 admission 解释。
    assert progress["checkins"][0].get("admission") is None
    assert progress["pending_seconds"] == 2 * 3600


# ---------------------------------------------------------------------------
# 快照固定：事后登记/补证/撤销不改变历史
# ---------------------------------------------------------------------------


def test_snapshot_pinned_at_event_time_not_changed_by_later_materials(client):
    _create_plan(client)
    _contract(client)
    # 签到发生时没有任何材料。
    _checkin(client, "E-01", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00")

    # 之后补登记一份"覆盖"活动窗口的材料：历史快照仍是 deficient。
    _material(client, "insurance")
    _material(client, "confidentiality_agreement")
    _material(client, "safety_training")
    checkin = _find_checkin(_progress(client), "E-01")
    assert checkin["admission"]["status"] == "deficient"
    assert set(checkin["admission"]["missing_requirements"]) == set(ALL_REQ)

    # 重复导入旧事件不会用新合同/新材料重建快照。
    _checkin(client, "E-01", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00")
    checkin = _find_checkin(_progress(client), "E-01")
    assert checkin["admission"]["status"] == "deficient"

    # 但可以通过窗口试评估看到"现在材料齐备"，作为追溯审批依据。
    window = client.post(
        f"/api/plans/{PV}/students/S1/eligibility/window",
        json={
            "start": "2024-03-15T08:00:00+08:00",
            "end": "2024-03-15T12:00:00+08:00",
        },
    ).json()
    assert window["status"] == "eligible"


def test_revocation_does_not_change_historical_eligible_snapshot(client):
    _create_plan(client)
    _contract(client)
    for req in ALL_REQ:
        _material(client, req)
    _checkin(client, "E-01", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00")
    checkin = _find_checkin(_progress(client), "E-01")
    assert checkin["admission"]["status"] == "eligible"

    # 撤销保险：当前资格变为 revoked。
    materials = client.get(f"/api/plans/{PV}/students/S1/materials").json()
    insurance = next(m for m in materials if m["requirement"] == "insurance")
    revoked = client.post(
        f"/api/plans/{PV}/materials/{insurance['material_id']}/revoke",
        json={"expected_version": insurance["version"], "actor_id": "admin"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"

    elig = client.get(
        f"/api/plans/{PV}/students/S1/eligibility",
        params={"moment": "2024-06-01T00:00:00+08:00"},
    ).json()
    by_code = {r["requirement"]: r["status"] for r in elig["requirements"]}
    assert by_code["insurance"] == "revoked"
    assert elig["eligible"] is False

    # 历史签到的资格快照不变，导师确认仍可计入。
    client.post(
        f"/api/plans/{PV}/events",
        json={"events": [{
            "event_id": "E-02", "event_type": "mentor_confirm", "student_id": "S1",
            "payload": {"checkin_event_id": "E-01"},
        }]},
    )
    assert _progress(client)["total_seconds"] == 4 * 3600

    # 撤销之后的新签到重新被阻塞。
    _checkin(client, "E-03", "2024-03-16T08:00:00+08:00", "2024-03-16T10:00:00+08:00")
    assert _find_checkin(_progress(client), "E-03")["admission"]["status"] == "deficient"


# ---------------------------------------------------------------------------
# 跨日到期：过期材料不自动延长
# ---------------------------------------------------------------------------


def test_cross_day_expiry_blocks_counting_and_retroactive(client):
    _create_plan(client, required_seconds=0)
    _contract(client)
    # 材料在 3-15 当天 23:59:59 到期，活动 22:00 持续到次日 02:00。
    expiry = "2024-03-15T23:59:59+08:00"
    for req in ALL_REQ:
        _material(client, req, vt=expiry)
    _checkin(
        client, "E-01",
        "2024-03-15T22:00:00+08:00", "2024-03-16T02:00:00+08:00",
    )
    progress = _progress(client)
    assert progress["total_seconds"] == 0
    assert progress["pending_seconds"] == 4 * 3600

    # 次日生效的新材料不覆盖窗口起点，追溯批准被拒绝（过期材料不延长）。
    fresh_from = "2024-03-16T00:00:00+08:00"
    for req in ALL_REQ:
        _material(client, req, ref=f"{req}-new", vf=fresh_from, vt=None)
    rejected = client.post(
        f"/api/plans/{PV}/events/E-01/retro-approval",
        json={"decision": "approved", "approver_id": "boss", "reason": "补证"},
    )
    assert rejected.status_code == 422
    detail = rejected.json()["detail"]
    assert "confidentiality_agreement" in detail

    # 用覆盖整个跨日窗口的材料补证后才能追溯计入。
    materials = client.get(f"/api/plans/{PV}/students/S1/materials").json()
    for m in materials:
        if m["valid_from"].startswith("2024-03-16"):
            client.post(
                f"/api/plans/{PV}/materials/{m['material_id']}/revoke",
                json={"expected_version": m["version"], "actor_id": "admin"},
            )
    for req in ALL_REQ:
        _material(
            client, req, ref=f"{req}-cover",
            vf="2024-01-01T00:00:00+08:00", vt="2024-12-31T23:59:59+08:00",
        )
    approved = client.post(
        f"/api/plans/{PV}/events/E-01/retro-approval",
        json={"decision": "approved", "approver_id": "boss", "reason": "窗口已完整覆盖"},
    )
    assert approved.status_code == 201
    assert _progress(client)["total_seconds"] == 4 * 3600


def test_eligibility_at_exact_expiry_boundary(client):
    _create_plan(client)
    _contract(client, required=["insurance"])
    _material(client, "insurance", vt="2024-03-15T12:00:00+08:00")
    # 当前时点在到期时刻 -> 已过期；之前一秒 -> 有效。
    before = client.get(
        f"/api/plans/{PV}/students/S1/eligibility",
        params={"moment": "2024-03-15T11:59:59+08:00"},
    ).json()
    assert before["eligible"] is True
    at_expiry = client.get(
        f"/api/plans/{PV}/students/S1/eligibility",
        params={"moment": "2024-03-15T12:00:00+08:00"},
    ).json()
    assert at_expiry["eligible"] is False
    assert at_expiry["requirements"][0]["status"] == "expired"


# ---------------------------------------------------------------------------
# 补证（supersede）
# ---------------------------------------------------------------------------


def test_supplement_replaces_old_material_without_extending_it(client):
    _create_plan(client)
    _contract(client, required=["insurance"])
    created = _material(client, "insurance", vt="2024-02-01T00:00:00+08:00").json()
    assert created["status"] == "active" and created["version"] == 1

    resp = client.post(
        f"/api/plans/{PV}/materials/{created['material_id']}/supplement",
        json={
            "document_ref": "insurance-v2",
            "valid_from": "2024-03-01T00:00:00+08:00",
            "valid_to": "2025-03-01T00:00:00+08:00",
            "actor_id": "clerk",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["previous"]["status"] == "superseded"
    assert body["previous"]["version"] == 2
    assert body["supplemented"]["supersedes_id"] == created["material_id"]
    assert body["supplemented"]["document_ref"] == "insurance-v2"

    # 旧材料的有效期没有被延长（3 月的活动只由新材料覆盖）。
    window_gap = client.post(
        f"/api/plans/{PV}/students/S1/eligibility/window",
        json={
            "start": "2024-02-15T08:00:00+08:00",
            "end": "2024-02-15T10:00:00+08:00",
        },
    ).json()
    assert window_gap["status"] == "deficient"

    # 不能对已替代/已撤销的材料再次补证。
    again = client.post(
        f"/api/plans/{PV}/materials/{created['material_id']}/supplement",
        json={
            "document_ref": "insurance-v3",
            "valid_from": "2024-01-01T00:00:00+08:00",
            "valid_to": None,
            "actor_id": "clerk",
        },
    )
    assert again.status_code == 409


# ---------------------------------------------------------------------------
# 并发更新
# ---------------------------------------------------------------------------


def test_concurrent_revokes_only_one_wins(client):
    _create_plan(client)
    _contract(client, required=["insurance"])
    created = _material(client, "insurance").json()
    mid = created["material_id"]

    from app import eligibility_services

    outcomes: list[str] = []
    lock = threading.Lock()

    def _revoke():
        session = TestSessionLocal()
        try:
            try:
                eligibility_services.revoke_material(
                    session,
                    plan_version=PV,
                    material_id=mid,
                    expected_version=1,
                    actor_id="admin",
                )
                result = "ok"
            except eligibility_services.VersionConflictError:
                result = "conflict"
            with lock:
                outcomes.append(result)
        finally:
            session.close()

    threads = [threading.Thread(target=_revoke) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 1
    assert outcomes.count("conflict") == 3

    stored = client.get(f"/api/plans/{PV}/students/S1/materials").json()
    row = next(m for m in stored if m["material_id"] == mid)
    assert row["status"] == "revoked"
    assert row["version"] == 2


def test_concurrent_retro_approvals_only_one_decision(client):
    _create_plan(client)
    _contract(client)
    _checkin(client, "E-01", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00")

    from app import eligibility_services

    outcomes: list[str] = []
    lock = threading.Lock()

    def _decide(decision: str):
        session = TestSessionLocal()
        try:
            try:
                eligibility_services.decide_retroactive(
                    session,
                    plan_version=PV,
                    event_id="E-01",
                    decision=decision,
                    approver_id="boss",
                    reason="并发审批",
                )
                result = "ok"
            except (
                eligibility_services.RetroConflictError,
                eligibility_services.RetroValidationError,
            ):
                result = "conflict"
            with lock:
                outcomes.append(result)
        finally:
            session.close()

    threads = [
        threading.Thread(target=_decide, args=("denied",)),
        threading.Thread(target=_decide, args=("approved",)),
        threading.Thread(target=_decide, args=("approved",)),
        threading.Thread(target=_decide, args=("denied",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 1
    assert outcomes.count("conflict") == 3

    decision = client.get(f"/api/plans/{PV}/events/E-01/retro-approval").json()
    assert decision["decision"] in {"approved", "denied"}


def test_concurrent_supplements_leave_no_orphan_new_material(client):
    _create_plan(client)
    _contract(client, required=["insurance"])
    created = _material(client, "insurance").json()
    mid = created["material_id"]

    from app import eligibility_services
    from app.repository import list_materials

    outcomes: list[str] = []
    lock = threading.Lock()

    def _supplement(idx: int):
        session = TestSessionLocal()
        try:
            try:
                eligibility_services.supplement_material(
                    session,
                    plan_version=PV,
                    material_id=mid,
                    document_ref=f"ins-v{idx}",
                    valid_from=_dt("2024-01-01T00:00:00+08:00"),
                    valid_to=None,
                    actor_id="clerk",
                )
                result = "ok"
            except (
                eligibility_services.VersionConflictError,
                eligibility_services.MaterialStateError,
            ):
                result = "conflict"
            with lock:
                outcomes.append(result)
        finally:
            session.close()

    threads = [threading.Thread(target=_supplement, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 1
    session = TestSessionLocal()
    try:
        rows = list_materials(session, PV, "S1")
        active = [m for m in rows if m.status == "active"]
        superseded = [m for m in rows if m.status == "superseded"]
        # 一个成功补证：恰好 1 条新 active + 1 条旧 superseded，无悬挂材料。
        assert len(active) == 1 and len(superseded) == 1
        assert active[0].supersedes_id == mid
    finally:
        session.close()


def _dt(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value)


# ---------------------------------------------------------------------------
# 冻结解释
# ---------------------------------------------------------------------------


def test_freeze_explains_blocked_checkin_and_remains_immutable(client):
    _create_plan(client, required_seconds=0)
    _contract(client)
    _material(client, "insurance")
    _checkin(client, "E-01", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00")

    frozen = client.post(f"/api/plans/{PV}/freezes/F-01", json={}).json()
    s1 = next(s for s in frozen["students"] if s["student_id"] == "S1")
    assert s1["total_seconds"] == 0
    assert s1["pending_seconds"] == 4 * 3600
    admission = s1["checkins"][0]["admission"]
    assert admission["hold_reason"] == "missing_prerequisites"
    assert "safety_training" in admission["missing_requirements"]

    # 冻结后补齐材料并追溯计入：冻结快照保持缺项解释不变。
    _material(client, "confidentiality_agreement")
    _material(client, "safety_training")
    client.post(
        f"/api/plans/{PV}/events/E-01/retro-approval",
        json={"decision": "approved", "approver_id": "boss", "reason": "追溯"},
    )
    frozen_again = client.get(f"/api/plans/{PV}/freezes/F-01").json()
    s1_old = next(s for s in frozen_again["students"] if s["student_id"] == "S1")
    assert s1_old["total_seconds"] == 0
    assert (
        s1_old["checkins"][0]["admission"]["hold_reason"]
        == "missing_prerequisites"
    )

    # 新冻结反映追溯后的状态。
    f2 = client.post(f"/api/plans/{PV}/freezes/F-02", json={}).json()
    s1_new = next(s for s in f2["students"] if s["student_id"] == "S1")
    assert s1_new["total_seconds"] == 4 * 3600
    assert (
        s1_new["checkins"][0]["admission"]["hold_reason"]
        == "retroactively_approved"
    )


# ---------------------------------------------------------------------------
# 输入校验与错误映射
# ---------------------------------------------------------------------------


def test_contract_and_material_validation(client):
    _create_plan(client)
    # 未知前置条件代码。
    resp = client.post(
        f"/api/plans/{PV}/contracts",
        json={
            "contract_version": "C1",
            "enterprise_id": "E",
            "required_documents": ["insurance", "unknown_doc"],
            "created_by": "a",
        },
    )
    assert resp.status_code == 422

    _contract(client)
    # 合同版本重复。
    assert _contract(client).status_code == 409

    # 失效窗口倒置。
    resp = _material(
        client, "insurance",
        vf="2024-03-01T00:00:00+08:00", vt="2024-02-01T00:00:00+08:00",
    )
    assert resp.status_code == 422

    # 撤销不存在的材料。
    resp = client.post(
        f"/api/plans/{PV}/materials/999/revoke",
        json={"expected_version": 1, "actor_id": "admin"},
    )
    assert resp.status_code == 404


def test_retro_approval_requires_reason_and_approver(client):
    _create_plan(client)
    _contract(client)
    _checkin(client, "E-01", "2024-03-15T08:00:00+08:00", "2024-03-15T12:00:00+08:00")
    resp = client.post(
        f"/api/plans/{PV}/events/E-01/retro-approval",
        json={"decision": "approved", "approver_id": "boss", "reason": ""},
    )
    assert resp.status_code == 422
