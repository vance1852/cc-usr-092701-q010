from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from careflow.api import create_handler
from careflow.business_calendar import Calendar
from careflow.clock import FrozenClock
from careflow.db import Database
from careflow.errors import Conflict, Forbidden, ValidationError
from careflow.service import Careflow

WEEKLY = {day: {"open": "09:00", "close": "18:00"}
          for day in ("monday", "tuesday", "wednesday", "thursday", "friday")}
WEEKLY["saturday"] = None
WEEKLY["sunday"] = None


class SlaCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "clinic.sqlite3")
        self.clock = FrozenClock(datetime(2026, 9, 28, 10, 0, tzinfo=UTC))  # 周一 18:00 上海
        self.app = Careflow(self.db, self.clock)
        initial = self.app.initialize_clinic("澄序门诊", "Asia/Shanghai", "负责人", "LongPassphrase!2026")
        self.clinic = initial["clinic_id"]
        self.owner = initial["owner_id"]
        self.clinician = self.app.create_staff(self.clinic, "值班医生", "clinician", actor_id=self.owner)["id"]
        self.nurse = self.app.create_staff(self.clinic, "护理组长", "nurse", actor_id=self.owner)["id"]
        self.patient = self.app.create_patient(self.clinic, self.owner, "case-sla", "陈女士")["id"]
        self.app.incident_sla.set_calendar(self.clinic, self.owner, WEEKLY, [
            {"date": "2026-10-01", "status": "closed", "reason": "国庆停业"}])
        self.app.incident_sla.put_policies(self.clinic, self.owner, [
            {"severity": "urgent", "response_minutes": 60, "escalation_grace_minutes": 30},
            {"severity": "high", "response_minutes": 120, "escalation_grace_minutes": 30},
            {"severity": "moderate", "response_minutes": 240, "escalation_grace_minutes": 60},
            {"severity": "low", "response_minutes": 1440, "escalation_grace_minutes": 240}])

    def tearDown(self):
        self.temp.cleanup()

    def report(self, severity="urgent", key="inc-1", when="2026-09-28T18:00:00+08:00",
               category="术后反应", summary="面部肿胀", actor=None):
        return self.app.report_incident(
            self.clinic, actor or self.nurse, self.patient, category, severity, when, summary, key)

    def advance(self, day, hour, minute=0):
        self.clock.set(datetime(2026, 9, day, hour, minute, tzinfo=UTC))

    # ------------------------------------------------------------ 日历引擎

    def test_business_calendar_skips_nights_weekends_holidays_and_dst(self):
        from zoneinfo import ZoneInfo
        from datetime import date
        tz = ZoneInfo("Asia/Shanghai")
        weekdays = {day: (9 * 60, 18 * 60) for day in
                    ("monday", "tuesday", "wednesday", "thursday", "friday")}
        all_week = {day: (9 * 60, 18 * 60) for day in
                    ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")}
        with_saturday = {**weekdays, "saturday": (9 * 60, 18 * 60), "sunday": None}
        # 周五 17:00 起 120 营业分钟：周五剩 60，周六再 60。
        result = Calendar(with_saturday).advance(
            datetime(2026, 10, 2, 17, tzinfo=tz), 7200, business=True, zone=tz)
        self.assertEqual(result.astimezone(tz).strftime("%a %H:%M"), "Sat 10:00")
        # 国庆停业：周五整天跳过，落到周六。
        holiday = Calendar(with_saturday, {date(2026, 10, 2): ("closed", "国庆")})
        result = holiday.advance(datetime(2026, 10, 1, 17, tzinfo=tz), 7200, business=True, zone=tz)
        self.assertEqual(result.astimezone(tz).strftime("%a %H:%M"), "Sat 10:00")
        # 纽约春令时跳过与秋令时拨回日，营业段仍为完整 9 小时。
        ny_tz = ZoneInfo("America/New_York")
        spring = Calendar(all_week).business_seconds(
            datetime(2026, 3, 8, tzinfo=ny_tz), datetime(2026, 3, 9, tzinfo=ny_tz), ny_tz)
        fall = Calendar(all_week).business_seconds(
            datetime(2026, 11, 1, tzinfo=ny_tz), datetime(2026, 11, 2, tzinfo=ny_tz), ny_tz)
        self.assertEqual(spring, 9 * 3600)
        self.assertEqual(fall, 9 * 3600)

    # ------------------------------------------------------------ 时限计算

    def test_deadline_uses_business_hours_and_clinic_timezone(self):
        incident = self.report()  # 周一 18:00 已关门
        status = self.app.incident_sla.status(self.clinic, self.owner, incident["id"])
        # 60 营业分钟从周二 09:00 起算 -> 10:00 本地 = 02:00Z。
        self.assertEqual(status["deadline_at"], "2026-09-29T02:00:00Z")
        self.assertEqual(status["timer_status"], "running")
        self.assertEqual(status["escalation_stage"], "none")
        open_event = status["timeline"][0]
        self.assertEqual(open_event["action"], "open")
        self.assertEqual(open_event["basis"]["trigger"], "incident.reported")

    def test_default_policy_seeded_when_no_explicit_configuration(self):
        # 新诊所不配置策略也能上报，使用内置默认并落库留痕。
        clinic = self.app.create_clinic("备用诊所", "Asia/Shanghai")
        staff = self.app.create_staff(clinic["id"], "负责人", "owner")
        reporter = self.app.create_staff(clinic["id"], "护士", "nurse", actor_id=staff["id"])["id"]
        patient = self.app.create_patient(clinic["id"], staff["id"], "x1", "某患者")["id"]
        incident = self.app.report_incident(clinic["id"], reporter, patient, "淤青", "high",
                                            "2026-09-28T10:00:00+08:00", "描述", "key-x")
        status = self.app.incident_sla.status(clinic["id"], staff["id"], incident["id"])
        self.assertEqual(status["response_minutes"], 60)
        policies = {p["severity"]: p for p in
                    self.app.incident_sla.list_policies(clinic["id"], staff["id"])["items"]}
        # 首次上报时内置默认已落库留痕，其值与内置默认一致。
        self.assertEqual(policies["high"]["response_minutes"], 60)
        self.assertEqual(policies["high"]["escalation_grace_minutes"], 20)

    # ------------------------------------------------------------ 提醒/升级

    def test_reminder_then_escalation_fires_once_across_rescans_and_restart(self):
        incident = self.report()
        # 关门期间扫描不触发。
        self.advance(28, 11)
        self.assertEqual(self.app.incident_sla.scan_due(self.clinic, self.owner)["reminded"], [])
        # 到期提醒当前负责人（未分配时提醒上报护士）。
        self.advance(29, 2, 0)
        result = self.app.incident_sla.scan_due(self.clinic, self.owner)
        self.assertEqual(len(result["reminded"]), 1)
        reminded = result["reminded"][0]
        self.assertEqual(reminded["notified"], self.nurse)
        self.assertEqual(reminded["target_basis"], "reporter_fallback")
        # 宽限按挂钟时间，30 分钟后升级。
        self.assertEqual(reminded["escalate_after_at"], "2026-09-29T02:30:00Z")
        # 宽限未到、重复扫描都不重复提醒或升级。
        self.advance(29, 2, 15)
        again = self.app.incident_sla.scan_due(self.clinic, self.owner)
        self.assertEqual(again["reminded"], [])
        self.assertEqual(again["escalated"], [])
        # 配置值班医生后宽限到期升级（依据值班表）。
        self.app.incident_sla.add_oncall(
            self.clinic, self.owner, self.clinician,
            "2026-09-29T00:00:00+08:00", "2026-09-30T00:00:00+08:00", "周二值班")
        self.advance(29, 2, 30)
        result = self.app.incident_sla.scan_due(self.clinic, self.owner)
        self.assertEqual(len(result["escalated"]), 1)
        self.assertEqual(result["escalated"][0]["escalated_to"], self.clinician)
        self.assertEqual(result["escalated"][0]["target_basis"], "roster")
        # 模拟进程重启：重新构建应用对象后再扫，升级不重复。
        restarted = Careflow(self.db, self.clock)
        self.assertEqual(restarted.incident_sla.scan_due(self.clinic, self.owner)["escalated"], [])
        status = self.app.incident_sla.status(self.clinic, self.owner, incident["id"])
        self.assertEqual([e["stage"] for e in status["escalations"]], ["reminded", "escalated"])
        self.assertTrue(self.app.verify_audit(self.clinic, self.owner)["ok"])
        # 系统事件与版本同步递增，一致性巡检不应报告事件台账问题。
        diagnostics = self.app.run_diagnostics(self.clinic, self.owner)
        self.assertFalse(any("incident" in finding["code"] for finding in diagnostics["findings"]),
                         diagnostics["findings"])

    def test_escalation_falls_back_to_default_then_owner(self):
        # high：09:00 上报，120 营业分钟 -> 11:00 到期，11:30 升级；无值班配置回退负责人。
        incident = self.report(severity="high", key="inc-2", when="2026-09-29T09:00:00+08:00")
        self.advance(29, 3, 0)  # 11:00
        self.app.incident_sla.scan_due(self.clinic, self.owner)
        self.advance(29, 3, 30)
        result = self.app.incident_sla.scan_due(self.clinic, self.owner)
        self.assertEqual(len(result["escalated"]), 1)
        self.assertEqual(result["escalated"][0]["incident_id"], incident["id"])
        self.assertEqual(result["escalated"][0]["target_basis"], "fallback_owner")
        self.assertEqual(result["escalated"][0]["escalated_to"], self.owner)
        # 配置默认值班人后，新事件升级给默认值班医生。
        self.app.incident_sla.set_default_oncall(self.clinic, self.owner, self.clinician)
        self.advance(29, 5, 0)  # 13:00
        incident2 = self.report(severity="high", key="inc-3", when="2026-09-29T13:00:00+08:00")
        self.advance(29, 7, 0)  # 15:00
        self.app.incident_sla.scan_due(self.clinic, self.owner)
        self.advance(29, 7, 30)
        result = self.app.incident_sla.scan_due(self.clinic, self.owner)
        escalated = {e["incident_id"]: e for e in result["escalated"]}
        self.assertIn(incident2["id"], escalated)
        self.assertEqual(escalated[incident2["id"]]["target_basis"], "default")
        self.assertEqual(escalated[incident2["id"]]["escalated_to"], self.clinician)

    def test_catch_up_scan_after_outage_fires_both_stages_atomically(self):
        # 长时间停机后首次扫描：提醒与升级在同一事务内补齐，且只各触发一次。
        self.app.incident_sla.set_default_oncall(self.clinic, self.owner, self.clinician)
        incident = self.report()
        self.advance(29, 3)  # 已超过宽限
        result = self.app.incident_sla.scan_due(self.clinic, self.owner)
        self.assertEqual([i["incident_id"] for i in result["reminded"]], [incident["id"]])
        self.assertEqual([i["incident_id"] for i in result["escalated"]], [incident["id"]])
        again = self.app.incident_sla.scan_due(self.clinic, self.owner)
        self.assertEqual(again["reminded"], [])
        self.assertEqual(again["escalated"], [])

    def test_oncall_roster_rejects_overlap_and_non_clinical_roles(self):
        self.app.incident_sla.add_oncall(
            self.clinic, self.owner, self.clinician,
            "2026-09-29T08:00:00+08:00", "2026-09-29T18:00:00+08:00")
        with self.assertRaises(Conflict):
            self.app.incident_sla.add_oncall(
                self.clinic, self.owner, self.owner,
                "2026-09-29T09:00:00+08:00", "2026-09-29T10:00:00+08:00")
        with self.assertRaises(ValidationError):
            self.app.incident_sla.add_oncall(
                self.clinic, self.owner, self.nurse,
                "2026-09-30T08:00:00+08:00", "2026-09-30T18:00:00+08:00")

    # ------------------------------------------------------------ 单调升级

    def test_late_downgrade_never_demotes_escalated_incident(self):
        incident = self.report()
        self.advance(29, 2, 0)
        self.app.incident_sla.scan_due(self.clinic, self.owner)
        self.app.incident_sla.set_default_oncall(self.clinic, self.owner, self.clinician)
        self.advance(29, 2, 30)
        self.app.incident_sla.scan_due(self.clinic, self.owner)
        # 版本：上报 1，提醒 2，升级 3。迟到的低等级修订到达。
        result = self.app.incident_sla.revise_severity(
            self.clinic, self.owner, incident["id"], "low", "事后判为轻微反应", 3)
        self.assertEqual(result["escalation_stage"], "escalated")
        self.assertEqual(result["timer_status"], "running")
        status = self.app.incident_sla.status(self.clinic, self.owner, incident["id"])
        # 临床等级已修订，计时器等级与时限保持锁定。
        self.assertEqual(status["severity"], "urgent")
        history = self.app.incident_history(self.clinic, self.owner, incident["id"])
        self.assertEqual(history["incident"]["severity"], "low")
        self.assertIn("severity_revised", [e["type"] for e in history["events"]])
        lock_event = [e for e in status["timeline"] if e["action"] == "severity_revision"][-1]
        self.assertTrue(lock_event["basis"]["locked"])

    def test_severity_revision_before_escalation_recomputes_deadline(self):
        incident = self.report(severity="high", key="inc-high",
                               when="2026-09-29T09:00:00+08:00")  # 120 营业分钟
        self.advance(29, 1)
        self.assertEqual(self.app.incident_sla.status(self.clinic, self.owner, incident["id"])["deadline_at"],
                         "2026-09-29T03:00:00Z")
        # 9 点半升级为 urgent（60 营业分钟）：已消耗 30，剩余 30，截止 10 点。
        self.advance(29, 1, 30)
        result = self.app.incident_sla.revise_severity(
            self.clinic, self.owner, incident["id"], "urgent", "发现喉头水肿", 1)
        self.assertEqual(result["deadline_at"], "2026-09-29T02:00:00Z")
        self.assertEqual(result["escalation_stage"], "none")

    # ------------------------------------------------------------ 计时暂停/恢复

    def test_monitoring_pauses_timer_and_resume_recomputes_deadline(self):
        incident = self.report(severity="high", key="inc-pause",
                               when="2026-09-29T09:00:00+08:00")
        self.advance(29, 1)
        # 未确认直接进入观察：计时暂停。
        self.app.transition_incident(self.clinic, self.nurse, incident["id"], "monitor", "等患者化验", 1)
        status = self.app.incident_sla.status(self.clinic, self.owner, incident["id"])
        self.assertEqual(status["timer_status"], "paused")
        self.assertEqual(status["elapsed_business_seconds"], 0)  # 9:00 刚开门
        # 关门期间暂停不消耗任何营业秒。
        self.advance(30, 3)  # 周三 11:00
        with self.assertRaises(ValidationError):
            self.app.incident_sla.resume_timer(self.clinic, self.nurse, incident["id"], "   ")
        self.app.incident_sla.resume_timer(self.clinic, self.nurse, incident["id"], "化验结果已回")
        status = self.app.incident_sla.status(self.clinic, self.owner, incident["id"])
        self.assertEqual(status["timer_status"], "running")
        with self.assertRaises(Conflict):  # 运行中重复恢复被拒绝
            self.app.incident_sla.resume_timer(self.clinic, self.nurse, incident["id"], "再次恢复")
        # 剩余 120 营业分钟从周三 11:00 起 -> 周三 13:00。
        self.assertEqual(status["deadline_at"], "2026-09-30T05:00:00Z")
        reasons = [(e["action"], e["reason"]) for e in status["timeline"]]
        self.assertIn(("pause", "事件进入观察状态，等待患者或外部信息，计时暂停"), reasons)
        self.assertTrue(any(a == "resume" for a, _ in reasons))

    def test_acknowledged_timer_cannot_be_paused_but_stage_is_locked(self):
        incident = self.report(severity="high", key="inc-ack",
                               when="2026-09-29T09:00:00+08:00")
        self.advance(29, 1)
        self.app.transition_incident(self.clinic, self.clinician, incident["id"], "triage", "已确认接手", 1)
        status = self.app.incident_sla.status(self.clinic, self.owner, incident["id"])
        self.assertEqual(status["timer_status"], "acknowledged")
        with self.assertRaises(Conflict):
            self.app.incident_sla.pause_timer(self.clinic, self.clinician, incident["id"], "尝试暂停")
        # 即使后来真正超时，已确认事件不会再被升级。
        self.advance(30, 8)
        self.assertEqual(self.app.incident_sla.scan_due(self.clinic, self.owner)["reminded"], [])
        self.assertEqual(self.app.incident_sla.scan_due(self.clinic, self.owner)["escalated"], [])

    def test_reopen_keeps_elapsed_budget_and_escalation_stage(self):
        incident = self.report()
        self.advance(29, 2, 0)
        self.app.incident_sla.scan_due(self.clinic, self.owner)
        self.app.incident_sla.set_default_oncall(self.clinic, self.owner, self.clinician)
        self.advance(29, 2, 30)
        self.app.incident_sla.scan_due(self.clinic, self.owner)
        # 版本：上报 1，提醒 2，升级（转派责任人）3。
        self.app.transition_incident(self.clinic, self.clinician, incident["id"], "triage", "接手", 3)
        self.app.transition_incident(self.clinic, self.clinician, incident["id"], "resolve", "恢复", 4)
        self.assertEqual(self.app.incident_sla.status(self.clinic, self.owner, incident["id"])["timer_status"],
                         "stopped")
        self.app.transition_incident(self.clinic, self.owner, incident["id"], "reopen", "症状反复", 5)
        status = self.app.incident_sla.status(self.clinic, self.owner, incident["id"])
        self.assertEqual(status["timer_status"], "running")
        self.assertEqual(status["escalation_stage"], "escalated")
        self.assertGreaterEqual(status["elapsed_business_seconds"], 60 * 60)

    # ------------------------------------------------------------ 转派交接

    def test_reassign_hands_over_report_actions_and_remaining_time(self):
        incident = self.report()
        self.advance(29, 2, 0)
        self.app.incident_sla.scan_due(self.clinic, self.owner)
        self.advance(29, 2, 30)
        self.app.incident_sla.set_default_oncall(self.clinic, self.owner, self.clinician)
        result = self.app.incident_sla.scan_due(self.clinic, self.owner)
        self.assertTrue(result["escalated"])
        # 系统升级已产生一份交接；护理组长再手动转回护士。
        second_nurse = self.app.create_staff(self.clinic, "夜班护士", "nurse", actor_id=self.owner)["id"]
        status = self.app.incident_sla.reassign(
            self.clinic, self.owner, incident["id"], second_nurse, "跨班交回护理组", 3)
        self.assertEqual(status["escalation_stage"], "escalated")
        handoffs = self.app.incident_sla.list_handoffs(self.clinic, self.owner, incident["id"])["items"]
        self.assertGreaterEqual(len(handoffs), 2)
        latest = handoffs[-1]
        self.assertEqual(latest["to_assignee"], second_nurse)
        self.assertEqual(latest["original_report"]["summary"], "面部肿胀")
        self.assertEqual(latest["original_report"]["reported_by"], self.nurse)
        self.assertIn("sla_escalated", [a["type"] for a in latest["actions_taken"]])
        self.assertIn("reported", [a["type"] for a in latest["actions_taken"]])
        self.assertGreaterEqual(latest["remaining_business_minutes"], 0)

    def test_reassign_requires_version_and_valid_target(self):
        incident = self.report()
        with self.assertRaises(Conflict):
            self.app.incident_sla.reassign(
                self.clinic, self.owner, incident["id"], self.clinician, "过期版本", 99)
        with self.assertRaises(ValidationError):
            self.app.incident_sla.reassign(
                self.clinic, self.owner, incident["id"], self.coordinator_id(), "协调员不能接手", 1)

    def coordinator_id(self):
        return self.app.create_staff(self.clinic, "协调员", "coordinator", actor_id=self.owner)["id"]

    # ------------------------------------------------------------ 队列与预演

    def test_queue_orders_by_deadline_and_marks_overdue(self):
        self.advance(29, 1, 0)  # 周二 09:00
        first = self.report(severity="urgent", key="q1", when="2026-09-29T09:00:00+08:00")
        self.advance(29, 1, 30)
        second = self.report(severity="low", key="q2", when="2026-09-29T09:30:00+08:00")
        self.advance(29, 3)  # 11:00，urgent 10:00 已超时
        queue = self.app.incident_sla.queue(self.clinic, self.owner)
        ids = [item["incident_id"] for item in queue["items"]]
        self.assertLess(ids.index(first["id"]), ids.index(second["id"]))
        overdue = {item["incident_id"]: item for item in queue["items"]}
        self.assertTrue(overdue[first["id"]]["overdue"])
        self.assertFalse(overdue[second["id"]]["overdue"])

    def test_simulation_is_read_only_and_never_writes_escalations(self):
        incident = self.report(severity="high", key="sim1",
                               when="2026-09-29T09:00:00+08:00")
        before = self.app.incident_sla.status(self.clinic, self.owner, incident["id"])
        simulation = self.app.incident_sla.simulate(
            self.clinic, self.owner, "2026-09-29T15:00:00+08:00")
        item = next(i for i in simulation["items"] if i["incident_id"] == incident["id"])
        self.assertEqual(simulation["mode"], "simulation")
        self.assertTrue(item["would_escalate"])
        self.assertEqual(item["actual_stage"], "none")
        self.assertEqual(item["projected_escalate_to"], self.owner)  # 无值班配置回退负责人
        after = self.app.incident_sla.status(self.clinic, self.owner, incident["id"])
        self.assertEqual(after["escalation_stage"], "none")
        self.assertEqual(after["escalations"], [])
        self.assertEqual(after["timeline"], before["timeline"])

    def test_simulation_preserves_actual_escalation_stage(self):
        incident = self.report()
        self.advance(29, 2, 0)
        self.app.incident_sla.scan_due(self.clinic, self.owner)
        simulation = self.app.incident_sla.simulate(
            self.clinic, self.owner, "2026-10-05T12:00:00+08:00")
        item = next(i for i in simulation["items"] if i["incident_id"] == incident["id"])
        self.assertEqual(item["actual_stage"], "reminded")
        self.assertEqual(item["effective_stage"], "escalated")  # 预演时刻已过宽限
        self.assertTrue(item["would_escalate"])

    # ------------------------------------------------------------ 权限与审计

    def test_calendar_and_policy_management_requires_owner(self):
        with self.assertRaises(Forbidden):
            self.app.incident_sla.put_policies(
                self.clinic, self.nurse, [{"severity": "low", "response_minutes": 60,
                                           "escalation_grace_minutes": 10}])
        with self.assertRaises(Forbidden):
            self.app.incident_sla.set_calendar(self.clinic, self.nurse, WEEKLY, [])

    def test_calendar_validation(self):
        with self.assertRaises(ValidationError):
            self.app.incident_sla.set_calendar(
                self.clinic, self.owner, {**WEEKLY, "monday": {"open": "18:00", "close": "09:00"}}, [])
        with self.assertRaises(ValidationError):
            self.app.incident_sla.set_calendar(
                self.clinic, self.owner, {d: None for d in
                                          ("monday", "tuesday", "wednesday", "thursday", "friday",
                                           "saturday", "sunday")}, [])  # 全周休息无法计算时限
        with self.assertRaises(ValidationError):
            bad = dict(WEEKLY); del bad["monday"]
            self.app.incident_sla.set_calendar(self.clinic, self.owner, bad, [])
        with self.assertRaises(ValidationError):
            self.app.incident_sla.put_policies(
                self.clinic, self.owner, [{"severity": "unknown", "response_minutes": 10,
                                           "escalation_grace_minutes": 5}])

    def test_timer_timeline_explains_every_start_and_stop(self):
        incident = self.report(severity="moderate", key="audit1",
                               when="2026-09-29T09:00:00+08:00")
        self.advance(29, 1)
        self.app.transition_incident(self.clinic, self.nurse, incident["id"], "monitor", "等待检查", 1)
        self.advance(29, 2)
        self.app.incident_sla.resume_timer(self.clinic, self.nurse, incident["id"], "检查完成")
        status = self.app.incident_sla.status(self.clinic, self.owner, incident["id"])
        actions = [e["action"] for e in status["timeline"]]
        self.assertEqual(actions, ["open", "pause", "resume"])
        for event in status["timeline"]:
            self.assertIsNotNone(event["started_at"])
        # 已结算的暂停段必须同时记录起止时刻与依据；仍在运行的段允许没有结束时刻。
        pause_event = next(e for e in status["timeline"] if e["action"] == "pause")
        self.assertIsNotNone(pause_event["ended_at"])
        self.assertLessEqual(pause_event["started_at"], pause_event["ended_at"])
        self.assertEqual(pause_event["basis"]["trigger"], "state_change")
        self.assertTrue(self.app.verify_audit(self.clinic, self.owner)["ok"])

    # ------------------------------------------------------------ HTTP 接口

    def test_http_routes_for_config_queue_scan_and_simulation(self):
        self.advance(29, 1)  # 周二 09:00，先拨钟再登录以免令牌跨过有效期
        server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            login = Request(base + "/auth/token", method="POST",
                            data=json.dumps({"staff_id": self.owner,
                                             "password": "LongPassphrase!2026"}).encode(),
                            headers={"X-Clinic-ID": self.clinic, "Content-Type": "application/json"})
            with urlopen(login, timeout=3) as response:
                token = json.loads(response.read())["access_token"]
            headers = {"X-Clinic-ID": self.clinic, "Authorization": f"Bearer {token}",
                       "Content-Type": "application/json"}

            def call(method, path, body=None):
                request = Request(base + path, method=method,
                                  data=json.dumps(body).encode() if body is not None else None,
                                  headers=headers)
                with urlopen(request, timeout=3) as response:
                    return response.status, json.loads(response.read())

            status, calendar = call("GET", "/config/calendar")
            self.assertEqual(status, 200)
            self.assertEqual(calendar["timezone"], "Asia/Shanghai")
            status, policies = call("GET", "/config/incident-sla-policies")
            self.assertEqual({p["severity"] for p in policies["items"]},
                             {"low", "moderate", "high", "urgent"})
            self.report(severity="high", key="http1", when="2026-09-29T09:00:00+08:00")
            status, queue = call("GET", "/reports/incident-queue")
            self.assertEqual(status, 200)
            self.assertEqual(queue["returned"], 1)
            status, scan = call("POST", "/incidents/scan-due", {"limit": 50})
            self.assertEqual(status, 200)
            self.assertEqual(scan["reminded"], [])
            status, simulation = call("GET", "/reports/incident-simulation?as_of=2026-09-29T15:00:00%2B08:00")
            self.assertEqual(status, 200)
            self.assertEqual(simulation["mode"], "simulation")
            self.assertTrue(simulation["items"][0]["would_escalate"])
            status, roster = call("GET", "/oncall")
            self.assertEqual(status, 200)
            self.assertIsNone(roster["current"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
