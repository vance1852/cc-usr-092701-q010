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

`POST /patients/{patient_id}/export` 只在存在有效数据导出授权时返回明确选择的章节。导出字段采用白名单，联系方式密文、凭据和内部合并字段不会导出；相同幂等请求得到相同内容摘要。`GET /reports/daily`、`appointments`、`incidents` 和 `overdue-milestones` 仅返回运营汇总或经岗位授权的工作队列。

## 不良事件响应时限与升级

每个事件等级的首响与处置时限、升级链可由诊所负责人配置；时限以**营业分钟**计量，按诊所营业日历（每周营业时段、闭馆日、特殊营业时间）计算，闭馆与暂停期间不消耗时限。

- `GET|PUT /config/incident-policy` 读取或更新响应策略（四个等级的 `respond_minutes`/`resolve_minutes` 及 1–6 个升级阶段），PUT 需 `expected_version`，首次为 0。
- `GET|PUT /config/incident-calendar` 读取或更新营业日历，字段为 `weekday_hours`（键 0=周一至 6=周日）、`closures`（闭馆日期）、`special_hours`（按日期覆盖）。
- `POST /duty-assignments`、`GET /duty-assignments` 维护值班临床负责人排班；升级到 `duty_clinician` 时按扫描时刻取当班的医生或负责人，无人当班则记为 `deferred`，不升级也不重复通知，补排后下一次扫描补发。
- `GET /incidents/worklist` 返回待处理队列，按升级级别、截止时间排序，并给出已用/剩余/超时营业分钟；护理、临床岗位与运营协调员可查看。
- `POST /incidents/scan` 执行一次超时扫描：到点先产生对当前负责人的 `remind`，超过配置偏移后产生对值班临床负责人的 `escalate` 并转派。每个 `(事件,计时阶段,阶段序号)` 只触发一次，重启或重复批量扫描不会重复升级。
- `POST /incidents/rehearse` 按请求中的 `as_of` **只读预演**该时刻将触发的动作，不写任何状态；运营协调员可使用。
- `POST /incidents/{id}/pause-clock`、`resume-clock` 显式暂停/恢复处置计时；恢复时按暂停期间经过的营业分钟顺延截止时间。转入观察自动暂停，重新打开自动恢复。
- `POST /incidents/{id}/transfer` 转派并返回交接包：原始报告、全部已采取措施事件、当前计时状态与剩余/超时营业分钟。
- `POST /incidents/{id}/reclassify` 修正等级：升高会按新等级收紧当前阶段截止时间；迟到的低等级更新只改记录等级，已达到的最高等级、升级级别和截止时间保持不变，不会退回未处理。
- `GET /incidents/{id}/timer-history` 返回每次计时暂停、恢复、停止的时间段（含起止依据）、升级记录和转派记录，哈希链审计中每个动作都带有 `basis` 说明。

## 主要状态

- 计划：草稿 → 提议 → 生效；可暂停和恢复，完成或取消后不能重新激活。
- 预约：占位 → 确认 → 到诊 → 服务中 → 完成；取消和未到诊是独立终态。
- 不良事件：已报告 → 分诊 → 观察 → 已解决 → 关闭。每次处置单独记录操作人和理由；响应计时按状态启动、暂停、恢复或停止，超时按升级链只升级一次。
- 耗材预留：预留 → 释放或核销。库存数量由收货、预留、释放和更正流水求和，不直接改写历史数量。
