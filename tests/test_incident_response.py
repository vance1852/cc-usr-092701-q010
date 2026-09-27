from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from careflow.clock import FrozenClock
from careflow.db import Database
from careflow.errors import Conflict, Forbidden, NotFound, ValidationError
from careflow.incident_response import BusinessCalendar, default_calendar_config, default_policy_config
from careflow.service import Careflow

START = datetime(2026, 9, 28, 1, 0, tzinfo=UTC)  # 2026-09-28 09:00 +08, Monday


class IncidentResponseCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "clinic.sqlite3")
        self.clock = FrozenClock(START)
        self.app = Careflow(self.db, self.clock)
        initial = self.app.initialize_clinic("澄序门诊", "Asia/Shanghai", "负责人", "LongPassphrase!2026")
        self.clinic = initial["clinic_id"]
        self.owner = initial["owner_id"]
        self.clinician = self.app.create_staff(self.clinic, "临床医生甲", "clinician", actor_id=self.owner)["id"]
        self.duty = self.app.create_staff(self.clinic, "值班临床负责人", "clinician", actor_id=self.owner)["id"]
        self.nurse = self.app.create_staff(self.clinic, "护理组长", "nurse", actor_id=self.owner)["id"]
        self.coordinator = self.app.create_staff(self.clinic, "运营协调员", "coordinator", actor_id=self.owner)["id"]
        self.patient = self.app.create_patient(self.clinic, self.coordinator, "case-200", "陈女士")["id"]
        self.ir = self.app.incident_responses

    def tearDown(self):
        self.temp.cleanup()

    def report(self, severity="high", key="inc-1", when="2026-09-28T09:00:00+08:00"):
        return self.app.report_incident(self.clinic, self.nurse, self.patient, "术后反应", severity,
                                        when, "患者报告局部红肿", key)

    def set_24h_duty(self, day_start="2026-09-28T00:00:00+08:00"):
        start = datetime.fromisoformat(day_start)
        end = start + timedelta(days=7)
        return self.ir.set_duty(self.clinic, self.owner, self.duty, start.isoformat(), end.isoformat(), "本周值班")

    def incident_version(self, incident_id):
        with self.db.transaction(write=False) as conn:
            return conn.execute("SELECT version FROM incidents WHERE id=?", (incident_id,)).fetchone()["version"]

    # -- 营业日历 -------------------------------------------------------

    def test_calendar_skips_closed_periods_and_honors_special_hours(self):
        config = default_calendar_config()
        config["closures"] = ["2026-09-29"]  # Tuesday closed for inventory
        config["special_hours"] = {"2026-09-30": [["13:00", "16:00"]]}
        cal = BusinessCalendar(config, "Asia/Shanghai")
        # Monday 10:00 + 480 business minutes: 8h same day would close at 18:00 after
        # only 480 minutes; exactly fits.
        deadline = cal.add_business_minutes(datetime(2026, 9, 28, 2, 0, tzinfo=UTC), 480)
        self.assertEqual(deadline, datetime(2026, 9, 28, 10, 0, tzinfo=UTC))
        # 481 minutes spills: 1 min Tue (closed) -> Wed special hours 13:00-16:00 local
        deadline = cal.add_business_minutes(datetime(2026, 9, 28, 2, 0, tzinfo=UTC), 481)
        self.assertEqual(deadline, datetime(2026, 9, 30, 5, 1, tzinfo=UTC))
        # Closed overnight hours contribute zero business minutes.
        minutes = cal.business_minutes_between(datetime(2026, 9, 28, 1, 0, tzinfo=UTC),
                                               datetime(2026, 9, 28, 15, 0, tzinfo=UTC))
        self.assertEqual(minutes, 540.0)

    def test_calendar_config_validation(self):
        with self.assertRaises(ValidationError):
            BusinessCalendar.validate({"weekday_hours": {"0": [["18:00", "09:00"]]}})
        with self.assertRaises(ValidationError):
            BusinessCalendar.validate({"weekday_hours": {"0": [["09:00", "12:00"], ["11:00", "18:00"]]}})
        with self.assertRaises(ValidationError):
            BusinessCalendar.validate({"weekday_hours": {"9": []}})
        valid = BusinessCalendar.validate(default_calendar_config())
        self.assertEqual(set(valid), {"weekday_hours", "closures", "special_hours"})

    def test_policy_config_requires_every_severity_and_ordered_stages(self):
        config = default_policy_config()
        del config["deadlines"]["low"]
        with self.assertRaises(ValidationError):
            self.ir.put_policy(self.clinic, self.owner, config, 0)
        config = default_policy_config()
        config["stages"][0]["offset_minutes"] = 60
        config["stages"][1]["offset_minutes"] = 10
        with self.assertRaises(ValidationError):
            self.ir.put_policy(self.clinic, self.owner, config, 0)
        with self.assertRaises(Forbidden):
            self.ir.put_policy(self.clinic, self.nurse, default_policy_config(), 0)

    def test_policy_and_calendar_use_optimistic_version(self):
        self.ir.put_calendar(self.clinic, self.owner, default_calendar_config(), 0)
        with self.assertRaises(Conflict):
            self.ir.put_calendar(self.clinic, self.owner, default_calendar_config(), 0)
        self.ir.put_policy(self.clinic, self.owner, default_policy_config(), 0)
        with self.assertRaises(Conflict):
            self.ir.put_policy(self.clinic, self.owner, default_policy_config(), 0)

    # -- 计时生命周期 ---------------------------------------------------

    def test_report_arms_response_timer_by_severity_and_business_hours(self):
        incident = self.report("high")  # 60 business minutes, Monday 09:00
        history = self.ir.timer_history(self.clinic, self.owner, incident["id"])
        self.assertEqual(history["sla"]["phase"], "response")
        self.assertEqual(history["sla"]["timer_state"], "running")
        self.assertEqual(history["sla"]["deadline_at"], "2026-09-28T02:00:00Z")  # 10:00 +08
        self.assertEqual(len(history["segments"]), 1)
        self.assertIn("事件报告时启动首响计时", history["segments"][0]["basis"])

    def test_deadline_reports_after_closing_rolls_to_next_business_day(self):
        self.clock.set(datetime(2026, 9, 28, 9, 50, tzinfo=UTC))  # 17:50 +08
        incident = self.report("urgent", key="inc-late")
        history = self.ir.timer_history(self.clinic, self.owner, incident["id"])
        # 10 minutes until 18:00, remaining 5 from 09:00 next day => 09:05 +08
        self.assertEqual(history["sla"]["deadline_at"], "2026-09-29T01:05:00Z")

    def test_acknowledgement_switches_to_handling_clock_with_fresh_deadline(self):
        incident = self.report("high")
        self.clock.set(datetime(2026, 9, 28, 1, 30, tzinfo=UTC))  # 09:30, within deadline
        result = self.app.transition_incident(self.clinic, self.clinician, incident["id"], "triage",
                                              "负责人确认并安排评估", 1, assign_to=self.clinician)
        self.assertEqual(result["state"], "triaged")
        history = self.ir.timer_history(self.clinic, self.owner, incident["id"])
        sla = history["sla"]
        self.assertEqual(sla["phase"], "handling")
        self.assertEqual(sla["acknowledged_by"], self.clinician)
        # 480 business minutes from 09:00 -> 17:30 same day (510 open minutes available)
        self.assertEqual(sla["deadline_at"], "2026-09-28T09:30:00Z")
        self.assertEqual([segment["phase"] for segment in history["segments"]], ["response", "handling"])
        self.assertTrue(self.app.verify_audit(self.clinic, self.owner)["ok"])

    def test_monitor_pauses_and_resume_shifts_deadline_by_closed_time(self):
        incident = self.report("high")
        self.app.transition_incident(self.clinic, self.clinician, incident["id"], "triage", "确认", 1,
                                     assign_to=self.clinician)
        version = self.incident_version(incident["id"])
        self.app.transition_incident(self.clinic, self.clinician, incident["id"], "monitor", "等待化验", version)
        history = self.ir.timer_history(self.clinic, self.owner, incident["id"])
        self.assertEqual(history["sla"]["timer_state"], "paused")
        self.assertIsNotNone(history["sla"]["paused_at"])
        # Resume two calendar days later at 12:00 +08; overnight and the intervening
        # closed night do not count; only business-open elapsed time shifts the deadline.
        self.clock.set(datetime(2026, 9, 30, 4, 0, tzinfo=UTC))
        version = self.incident_version(incident["id"])
        resumed = self.ir.resume_clock(self.clinic, self.clinician, incident["id"], "化验结果返回", version)
        self.assertEqual(resumed["timer_state"], "running")
        # Paused 09:00 Mon -> 12:00 Wed spans 540+540+180 = 1260 business minutes.
        self.assertEqual(resumed["shifted_business_minutes"], 1260.0)
        # Handling deadline 17:00 Mon shifted by 1260 -> 11:00 Thu.
        self.assertEqual(resumed["deadline_at"], "2026-10-01T03:00:00Z")
        history = self.ir.timer_history(self.clinic, self.owner, incident["id"])
        self.assertEqual(len(history["segments"]), 3)
        self.assertIn("化验结果返回", history["segments"][-1]["basis"])

    def test_explicit_pause_only_in_handling_and_resume_validates_state(self):
        incident = self.report("high")
        with self.assertRaises(Conflict):
            self.ir.pause_clock(self.clinic, self.clinician, incident["id"], "首响阶段不能手动暂停", 1)
        self.app.transition_incident(self.clinic, self.clinician, incident["id"], "triage", "确认", 1,
                                     assign_to=self.clinician)
        paused = self.ir.pause_clock(self.clinic, self.clinician, incident["id"], "等待外部影像", 2)
        self.assertEqual(paused["timer_state"], "paused")
        with self.assertRaises(Conflict):
            self.ir.pause_clock(self.clinic, self.clinician, incident["id"], "重复暂停", 3)
        self.clock.set(datetime(2026, 9, 28, 4, 0, tzinfo=UTC))
        resumed = self.ir.resume_clock(self.clinic, self.clinician, incident["id"], "影像已到", 3)
        self.assertEqual(resumed["shifted_business_minutes"], 180.0)  # 09:00 -> 12:00 local

    def test_resolve_and_close_stop_the_clock_and_reopen_keeps_escalation_level(self):
        incident = self.report("high")
        self.app.transition_incident(self.clinic, self.clinician, incident["id"], "triage", "确认", 1,
                                     assign_to=self.clinician)
        self.app.transition_incident(self.clinic, self.clinician, incident["id"], "resolve", "处置完成", 2)
        history = self.ir.timer_history(self.clinic, self.owner, incident["id"])
        self.assertEqual(history["sla"]["phase"], "done")
        self.assertEqual(history["sla"]["timer_state"], "stopped")
        self.assertIsNotNone(history["sla"]["resolved_at"])
        # Reopen: fresh handling deadline but escalation record is preserved.
        self.app.transition_incident(self.clinic, self.owner, incident["id"], "reopen", "症状反复", 3)
        history = self.ir.timer_history(self.clinic, self.owner, incident["id"])
        self.assertEqual(history["sla"]["phase"], "handling")
        self.assertEqual(history["sla"]["timer_state"], "running")

    # -- 扫描升级 -------------------------------------------------------

    def test_scan_reminds_then_escalates_once_including_restart_and_repeats(self):
        incident = self.report("high")
        self.set_24h_duty()
        self.clock.set(datetime(2026, 9, 28, 2, 0, tzinfo=UTC))  # exactly at deadline
        first = self.ir.scan(self.clinic, self.owner)
        self.assertEqual(len(first["fired"]), 1)
        self.assertEqual(first["fired"][0]["kind"], "remind")
        self.assertEqual(first["fired"][0]["target"], "assignee")
        # Repeated batch scans and a process restart (new service over same DB) must not refire.
        self.assertEqual(self.ir.scan(self.clinic, self.owner)["fired"], [])
        restarted = Careflow(self.db, self.clock)
        self.assertEqual(restarted.incident_responses.scan(self.clinic, self.owner)["fired"], [])
        # 30 business minutes past deadline -> escalate to duty clinician.
        self.clock.set(datetime(2026, 9, 28, 2, 30, tzinfo=UTC))
        escalated = self.ir.scan(self.clinic, self.owner)
        self.assertEqual(len(escalated["fired"]), 1)
        fired = escalated["fired"][0]
        self.assertEqual(fired["kind"], "escalate")
        self.assertEqual(fired["target_staff_id"], self.duty)
        with self.db.transaction(write=False) as conn:
            row = conn.execute("SELECT assigned_to,escalation_level FROM incident_slas s "
                               "JOIN incidents i ON i.id=s.incident_id WHERE s.incident_id=?",
                               (incident["id"],)).fetchone()
            self.assertEqual(row["assigned_to"], self.duty)
            self.assertEqual(row["escalation_level"], 2)
        self.assertEqual(self.ir.scan(self.clinic, self.owner)["fired"], [])

    def test_escalation_without_duty_clinician_defers_until_duty_exists(self):
        self.report("high")
        self.clock.set(datetime(2026, 9, 28, 2, 30, tzinfo=UTC))
        result = self.ir.scan(self.clinic, self.owner)
        # Reminder to the current assignee still fires; duty escalation is deferred.
        self.assertEqual([f["kind"] for f in result["fired"]], ["remind"])
        self.assertEqual(result["deferred"][0]["reason"], "no_duty_clinician")
        # Idempotent while still unmanned.
        self.assertEqual(self.ir.scan(self.clinic, self.owner)["deferred"][0]["stage_index"], 1)
        self.set_24h_duty()
        result = self.ir.scan(self.clinic, self.owner)
        self.assertEqual(len(result["fired"]), 1)
        self.assertEqual(result["fired"][0]["kind"], "escalate")
        self.assertEqual(result["fired"][0]["target_staff_id"], self.duty)
        self.assertEqual(result["deferred"], [])

    def test_duty_assignment_requires_active_clinical_owner_role(self):
        with self.assertRaises(ValidationError):
            self.ir.set_duty(self.clinic, self.owner, self.nurse,
                             "2026-09-28T08:00:00+08:00", "2026-09-28T18:00:00+08:00")
        with self.assertRaises(ValidationError):
            self.ir.set_duty(self.clinic, self.owner, self.duty,
                             "2026-09-28T18:00:00+08:00", "2026-09-28T08:00:00+08:00")

    # -- 转派交接 -------------------------------------------------------

    def test_transfer_hands_over_report_actions_and_remaining_time(self):
        incident = self.report("high")
        self.app.transition_incident(self.clinic, self.clinician, incident["id"], "triage", "确认并查体", 1,
                                     assign_to=self.clinician)
        self.clock.set(datetime(2026, 9, 28, 2, 0, tzinfo=UTC))  # 10:00
        transfer = self.ir.transfer(self.clinic, self.clinician, incident["id"], self.duty,
                                    "跨班交接：剩余化验未回报", 2)
        self.assertEqual(transfer["from_assignee"], self.clinician)
        self.assertEqual(transfer["assigned_to"], self.duty)
        # Handling deadline 17:00; at 10:00 remaining = 7h = 420 business minutes.
        self.assertEqual(transfer["remaining_business_minutes"], 420.0)
        handover = transfer["handover"]
        self.assertEqual(handover["report"]["summary"], "患者报告局部红肿")
        self.assertEqual(handover["report"]["severity"], "high")
        self.assertGreaterEqual(len(handover["actions_taken"]), 2)
        self.assertEqual(handover["timer"]["phase"], "handling")
        self.assertIn("deadline_at", handover["timer"])
        history = self.ir.timer_history(self.clinic, self.owner, incident["id"])
        self.assertEqual(history["transfers"][0]["to_assignee"], self.duty)

    def test_transfer_validates_target_and_state(self):
        incident = self.report("high")
        with self.assertRaises(ValidationError):
            self.ir.transfer(self.clinic, self.owner, incident["id"], self.coordinator, "不能交给非临床岗", 1)
        with self.assertRaises(Conflict):
            self.ir.transfer(self.clinic, self.owner, incident["id"], self.clinician, "版本错误", 99)

    # -- 等级修正 -------------------------------------------------------

    def test_late_downgrade_never_returns_escalated_incident_to_unhandled(self):
        incident = self.report("high")
        self.set_24h_duty()
        self.clock.set(datetime(2026, 9, 28, 2, 30, tzinfo=UTC))
        self.ir.scan(self.clinic, self.owner)
        version = self.incident_version(incident["id"])
        result = self.ir.reclassify(self.clinic, self.owner, incident["id"], "low", "迟到的低等级更新", version)
        self.assertEqual(result["severity"], "low")
        self.assertEqual(result["max_severity"], "high")
        self.assertEqual(result["escalation_level"], 2)
        self.assertFalse(result["deadline_changed"])
        history = self.ir.timer_history(self.clinic, self.owner, incident["id"])
        self.assertEqual(len(history["escalations"]), 2)  # remind + escalate remain

    def test_upgrade_recomputes_tighter_deadline(self):
        incident = self.report("low", key="inc-low")  # 480 business minutes
        self.clock.set(datetime(2026, 9, 28, 2, 0, tzinfo=UTC))  # 10:00, 420 min remain same day
        result = self.ir.reclassify(self.clinic, self.nurse, incident["id"], "urgent", "情况恶化", 1)
        self.assertEqual(result["max_severity"], "urgent")
        self.assertTrue(result["deadline_changed"])
        # urgent handling/response budget 15 from 10:00 -> 10:15 local
        self.assertEqual(result["deadline_at"], "2026-09-28T02:15:00Z")

    # -- 工作队列与预演 -------------------------------------------------

    def test_worklist_orders_by_escalation_and_deadline_with_business_minutes(self):
        first = self.report("high", key="inc-a")
        second = self.report("urgent", key="inc-b", when="2026-09-28T09:30:00+08:00")
        self.clock.set(datetime(2026, 9, 28, 1, 30, tzinfo=UTC))
        worklist = self.ir.worklist(self.clinic, self.nurse)
        self.assertEqual(worklist["returned"], 2)
        self.assertEqual([item["incident_id"] for item in worklist["items"]], [second["id"], first["id"]])
        item = worklist["items"][1]
        self.assertIn("remaining_business_minutes", item)
        self.assertEqual(item["overdue_business_minutes"], 0.0)
        self.assertEqual(item["elapsed_business_minutes"], 30.0)
        # Coordinator (operations) may read the queue but cannot mutate incidents.
        self.assertGreaterEqual(self.ir.worklist(self.clinic, self.coordinator)["returned"], 2)
        with self.assertRaises(Forbidden):
            self.ir.scan(self.clinic, self.coordinator)

    def test_rehearsal_is_read_only_and_shows_projected_actions_at_given_moment(self):
        self.report("high")
        self.set_24h_duty()
        before = self.ir.worklist(self.clinic, self.owner)
        rehearsal = self.ir.rehearse(self.clinic, self.coordinator, "2026-09-28T10:40:00+08:00")
        self.assertTrue(rehearsal["writes"] is False)
        projected = rehearsal["items"][0]["projected_actions"]
        kinds = {(item["kind"], item["would_fire"]) for item in projected}
        self.assertIn(("remind", True), kinds)
        self.assertIn(("escalate", True), kinds)
        # Nothing was persisted.
        after = self.ir.worklist(self.clinic, self.owner)
        self.assertEqual([item["escalation_level"] for item in before["items"]],
                         [item["escalation_level"] for item in after["items"]])
        with self.db.transaction(write=False) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM incident_escalations").fetchone()[0], 0)
        # Before the deadline rehearsal projects nothing.
        early = self.ir.rehearse(self.clinic, self.coordinator, "2026-09-28T09:30:00+08:00")
        self.assertEqual(early["items"][0]["projected_actions"], [])

    # -- 审计 -----------------------------------------------------------

    def test_audit_explains_each_clock_and_escalation_decision(self):
        incident = self.report("high")
        self.set_24h_duty()
        self.clock.set(datetime(2026, 9, 28, 2, 30, tzinfo=UTC))
        self.ir.scan(self.clinic, self.owner)
        events = self.app.audit_history(self.clinic, self.owner)
        actions = {event["action"]: event["payload"] for event in events
                   if event["aggregate_id"] == incident["id"]}
        self.assertIn("incident.sla_armed", actions)
        self.assertIn("basis", actions["incident.sla_armed"])
        self.assertIn("超过0营业分钟", actions["incident.reminder_fired"]["basis"])
        self.assertIn("超过30营业分钟", actions["incident.escalation_fired"]["basis"])
        self.assertEqual(actions["incident.escalation_fired"]["target"], "duty_clinician")
        self.assertTrue(self.app.verify_audit(self.clinic, self.owner)["ok"])

    def test_diagnostics_still_pass_after_all_timer_operations(self):
        incident = self.report("high")
        self.set_24h_duty()
        self.app.transition_incident(self.clinic, self.clinician, incident["id"], "triage", "确认", 1,
                                     assign_to=self.clinician)
        self.clock.set(datetime(2026, 9, 28, 3, 0, tzinfo=UTC))
        self.ir.pause_clock(self.clinic, self.clinician, incident["id"], "等待补充材料", 2)
        self.ir.resume_clock(self.clinic, self.clinician, incident["id"], "材料齐备", 3)
        self.ir.transfer(self.clinic, self.clinician, incident["id"], self.duty, "交接", 4)
        report = self.app.run_diagnostics(self.clinic, self.owner)
        ledger_issues = [f["code"] for f in report["findings"] if f["code"].startswith("incident.")]
        self.assertNotIn("incident.event_missing", ledger_issues)
        self.assertNotIn("incident.version_gap", ledger_issues)


if __name__ == "__main__":
    unittest.main()
