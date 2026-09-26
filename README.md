# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。企业实训项目还要求保险、保密协议和安全培训三项前置材料均在有效期内：签到事件导入时会固定合同版本与材料版本快照，缺项签到进入待定（不丢弃、不计入）并自动生成补证案件；材料补齐后仍须授权人员（合规专员/管理员）显式决定是否追溯计入，过期或作废材料不会自动延长计入。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 前置条件与补证 API

- `POST /api/plans/{plan_version}/contract`：登记企业实训合同版本（三项前置材料缺一不可，合同按培养方案唯一、不可变）。
- `POST /api/materials` / `GET /api/materials`：登记/查询学员材料版本（有效期含时区，重复版本冲突返回 409）。
- `POST /api/materials/{student_id}/{material_type}/{version}/revoke`：撤销材料；引用该材料版本的已满足签到自动回到补证待定。
- `GET /api/plans/{plan_version}/checkins/{event_id}/qualification`：返回事件时固定快照、按当前材料的重评结果、重放状态与案件。
- `GET /api/plans/{plan_version}/supplement-cases`：补证案件列表（可按学员/状态过滤）。
- `POST /api/plans/{plan_version}/supplement-cases/{case_id}/decision`：授权人员审批是否追溯计入；材料仍不覆盖区间时须 `gap_acknowledged=true`；案件终局（approved/rejected）不可更改。

## 运行方式

默认数据保存在项目目录的 SQLite 文件中。安装依赖后执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，健康检查地址为 `/health`，业务接口位于 `/api`。

## 测试

```bash
python3 -m pytest -q
```

## 编译检查

```bash
python3 -m compileall -q app tests
```

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询；运行过程中不需要单独的数据库或网络服务。
