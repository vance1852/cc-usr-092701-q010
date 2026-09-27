# 服务接口

所有时间使用带时区的 ISO 8601 格式。服务持久化 UTC 时间，按诊所配置的时区解释运营日期。JSON 请求大小上限为 1 MB；无效请求返回稳定的错误码和 HTTP 状态，不向调用方透出数据库异常。

## 登录与诊所隔离

`POST /auth/token` 接受 `staff_id` 和 `password`，返回有效期不超过一天的 Bearer 凭据。除登录与健康检查外，请求必须同时提供 `Authorization: Bearer …` 和 `X-Clinic-ID`。认证失败不区分账号不存在、停用或密码错误；诊所边界之外的数据返回不存在，避免泄露另一诊所的记录。

`POST /auth/logout` 撤销当前凭据。修改员工密码会撤销该员工的全部活动凭据。初始负责人通过命令行创建；没有可直接注册负责人的 HTTP 路由。

## 患者、评估与诊疗计划

- `POST /patients` 建立诊所内患者档案；外部编号在诊所范围内唯一。
- `GET /patients/{patient_id}` 返回最小档案，不返回联系方式密文。
- `POST /patients/{patient_id}/merge` 以两个版本号和书面原因将重复档案标记为合并，并指向保留档案。
- `POST /patients/{patient_id}/assessments` 新建评估草稿；`POST /assessments/{assessment_id}/sign` 由临床岗位签署。
- `POST /patients/{patient_id}/consents` 创建更高版本的授权；`POST /consents/{consent_id}/withdraw` 撤回授权。
- `POST /patients/{patient_id}/plans` 建立计划，医美和体重管理计划必须引用当前对应授权。
- `POST /plans/{plan_id}/{propose|activate|pause|resume|complete|cancel}` 以 `expected_version` 执行带版本保护的状态转换。
- `GET /patients/{patient_id}/weight-series` 返回按观察时间排序的测量值，不生成诊断或治疗建议。

评估签署后不可覆盖。就诊病历由章节组成，签署需要主诉、评估和计划三部分；签署后的补充内容成为新版本，原始文字仍保留。

## 预约、随访与计划节点

创建预约须提供 `Idempotency-Key`，有责任人的预约不能与未结束时段重叠。临时占位到期后由 `POST /appointments/{id}/book` 拒绝确认，过期占位可通过服务方法按限额释放。预约状态按占位、确认、到诊、服务、完成推进；开始服务时产生就诊记录。

随访和计划节点支持领取租约、版本校验、幂等创建、延期和完整处置历史。旧领取者不能以过期令牌提交结果；重新领取不会删除前次领取事件。

## 诊所耗材

- `POST /products` 登记耗材；`POST /products/{product_id}/lots` 按批号入库。
- `POST /stock/reserve` 依据失效日期按先到期先出分批预留，需要 `Idempotency-Key`。
- `POST /stock/{reservation_id}/consume` 记录患者使用；`release` 释放尚未使用的数量。
- `POST /stock/{lot_id}/quarantine`、`recall` 或 `release-quarantine` 记录批次处置及受影响预留。
- `GET /stock/lots` 查看可用数量；`GET /stock/{lot_id}/history` 查看批次流水。

入库、占用、释放与患者使用均进入不可变流水。存在不足时整笔预留回滚；被隔离、召回或在诊所本地日期已过期的批次不能继续使用。

## 不良事件与数据使用

护理人员可报告事件或患者安全关注项；临床岗位复核并记录处置，诊所负责人可作废就诊记录。`GET /audit/verify` 校验诊所哈希链，`GET /audit/diagnostics` 汇报需人工核对的一致性问题，不自动修改业务状态。

### 事件响应时限与升级链

每个事件在上报时建立独立计时器，响应时限按事件等级（low/moderate/high/urgent）以**诊所营业分钟**累计：关门、周休与例假日不消耗时限；升级宽限按**挂钟时间**计算，跨班与夜间同样计入，避免跨班患者被漏掉。计时器状态为 `running`、`paused`、`acknowledged`、`stopped`：负责人确认（triage）后计时结束并锁定；进入观察（monitor）暂停；解决或关闭停止；重开时保留已消耗营业秒与升级阶段恢复计时，不能靠重开刷新时限。

升级分两级且阶段单调递增：时限到期先提醒当前负责人（未分配时提醒上报人），宽限到期升级给值班临床负责人（依次按值班表、默认值班人、在岗负责人解析）。每次触发写入 `(incident_id,stage)` 唯一升级记录，进程重启或批量重复扫描不会重复升级。迟到的等级修订照常记录临床等级，但已提醒/已升级事件的计时器等级、剩余时限与升级阶段保持锁定，不会降回未处理。

- `GET|POST /config/calendar` 读取或配置每周班次与例假日（`weekly_hours` 按星期给出 `{open,close}` 或 `null`，`exceptions` 给出特定日期的停业或特殊班次）；仅负责人可修改。
- `GET|POST /config/incident-sla-policies` 读取或批量配置各等级的 `response_minutes`（营业分钟）与 `escalation_grace_minutes`（挂钟分钟）。未配置时使用内置默认并在首次使用时落库留痕。
- `POST /oncall/roster`、`POST /oncall/default` 维护值班时段与默认值班临床负责人；`GET /oncall?at=…` 查询当前与后续值班。值班时段不可重叠。
- `POST /incidents/scan-due` 幂等扫描到期事件，返回本次 `reminded` 与 `escalated`，可安全重复执行或重启后补扫。
- `GET /reports/incident-queue` 返回未结束事件的时限工作队列，按到期时刻排序（不再仅按严重程度），含剩余营业分钟、是否超时与升级阶段。
- `GET /reports/incident-simulation?as_of=…` 按指定时刻**只读预演**：投影当时会提醒/升级的事件与接收人，不写任何升级记录，实际升级阶段不被抹掉。
- `GET /incidents/{id}/sla` 返回计时器状态、截止时刻、升级记录与完整计时分段台账；`POST /incidents/{id}/sla/pause|resume` 手动暂停或恢复并要求填写原因。
- `POST /incidents/{id}/severity` 修订等级（乐观版本）；`POST /incidents/{id}/reassign` 转派，交接包包含原始报告、全部已采取措施、剩余营业分钟、截止时刻与当前升级阶段；`GET /incidents/{id}/handoffs` 查看历次交接。

每次开启、暂停、恢复、确认、转派、提醒与升级都写入不可变计时台账（起止时刻、触发依据）并进入审计哈希链；系统触发的动作为操作人留空。

`POST /patients/{patient_id}/export` 只在存在有效数据导出授权时返回明确选择的章节。导出字段采用白名单，联系方式密文、凭据和内部合并字段不会导出；相同幂等请求得到相同内容摘要。`GET /reports/daily`、`appointments`、`incidents` 和 `overdue-milestones` 仅返回运营汇总或经岗位授权的工作队列。

## 主要状态

- 计划：草稿 → 提议 → 生效；可暂停和恢复，完成或取消后不能重新激活。
- 预约：占位 → 确认 → 到诊 → 服务中 → 完成；取消和未到诊是独立终态。
- 不良事件：已报告 → 分诊 → 观察 → 已解决 → 关闭。每次处置单独记录操作人和理由。
- 事件计时器：运行中 →（暂停 ⇄ 恢复）→ 已确认/停止；升级阶段 未升级 → 已提醒 → 已升级，只进不退。
- 耗材预留：预留 → 释放或核销。库存数量由收货、预留、释放和更正流水求和，不直接改写历史数量。
