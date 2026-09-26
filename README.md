# 实训学时合规与冻结服务

该服务汇聚学员签到、导师确认和请假修正事件，按培养方案与时区重放学时状态，并保存可追溯的学期冻结快照。项目还提供导师分配、证明材料、豁免复核、规则版本、名额、通知和数据留存等领域模块，供后续业务扩展时复用统一的状态与审计约束。

## 企业实训前置条件

企业实训活动要求**保险、保密协议、安全培训**在活动期间持续有效：

- `POST /api/plans/{plan_version}/contracts` 登记合同版本及其前置条件清单；
- `POST /api/plans/{plan_version}/materials` 登记学员材料及有效期，支持 `…/materials/{id}/supplement` 补证与 `…/materials/{id}/revoke` 撤销（乐观锁版本号）；
- 每个**实训签到事件导入时**按当时已登记材料评估并固定资格快照（`event_admissions`），事后登记、补证、撤销材料都不会改写历史结论；
- 缺项签到**进入待定（PENDING）而不是被丢弃**，导师确认也无法解除该阻塞；
- `GET …/students/{id}/eligibility` 与 `POST …/eligibility/window` 分别按当前时点和活动窗口评估，明确区分 `covered / expired / revoked / superseded / not_yet_effective / missing`；
- 材料补齐后由授权人员通过 `POST /api/plans/{plan_version}/events/{event_id}/retro-approval` 决定是否**追溯计入**；审批按活动完整窗口重新校验，过期材料不会自动延长，决定唯一且终态（并发冲突返回 409）。

普通（非实训）活动与未登记合同的方案不受前置条件约束。

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

测试覆盖事件幂等导入、跨时区与跨日学时合并、实习确认、负向修正、冻结快照和差异查询；前置条件模块另覆盖并发更新（撤销/补证/审批的乐观锁与唯一约束）、跨日到期边界、撤销后的历史快照不变性，以及冻结中的缺项/追溯解释；运行过程中不需要单独的数据库或网络服务。
