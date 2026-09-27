"""不良事件响应时限（SLA）、计时暂停/恢复与升级链。

计时模型：
- 响应时限按诊所营业日历以“营业秒”累计；关门、节假日不消耗时限。
- 每个事件一份计时器（incident_slas），状态为 running/paused/stopped/acknowledged；
  开启、暂停、恢复、确认、转派、升级都写入不可变分段台账并进入审计哈希链，
  每条记录说明起止时刻与触发依据。
- 升级分两级且阶段单调递增：到期先提醒当前负责人；宽限按挂钟时间计算（覆盖跨班
  与夜间），宽限到期升级值班临床负责人。升级记录有唯一约束，进程重启或批量重复
  扫描不会重复触发同一阶段。
- 迟到的低等级修订不能把已提醒/已升级事件降回未处理：临床等级照常修订留痕，
  但计时器等级、时限与升级阶段保持锁定。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import audit
from .business_calendar import DEFAULT_WEEKLY, WEEKDAYS, Calendar, DayOpening
from .db import Database, decode_json, encode_json
from .errors import Conflict, NotFound, ValidationError
from .ids import new_id
from .security import authorize, principal_for
from .validation import calendar_date, choice, integer, object_value, parsed_timestamp, text, timestamp

SEVERITIES = ("low", "moderate", "high", "urgent")

# 未配置策略时使用的内置默认值（响应营业分钟，升级宽限挂钟分钟）。
DEFAULT_POLICIES = {
    "urgent": (30, 10),
    "high": (60, 20),
    "moderate": (240, 60),
    "low": (1440, 240),
}

# 事件进入各状态时计时器遵循的规则。
TIMER_ON_STATE = {
    "reported": "run",
    "triaged": "acknowledge",
    "monitoring": "pause",
    "resolved": "stop",
    "closed": "stop",
}


class IncidentSlaService:
    def __init__(self, database: Database, clock):
        self.db = database
        self.clock = clock

    # ------------------------------------------------------------------ 日历

    def set_calendar(self, clinic_id: str, actor_id: str, weekly_hours: dict, exceptions: list) -> dict:
        weekly = self._parse_weekly(weekly_hours)
        parsed_exceptions = self._parse_exceptions(exceptions)
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "clinic:manage", clinic_id=clinic_id)
            self._clinic_zone(connection, clinic_id)
            existing = connection.execute("SELECT version FROM clinic_calendar WHERE clinic_id=?", (clinic_id,)).fetchone()
            version = (existing["version"] if existing else 0) + 1
            connection.execute(
                "INSERT INTO clinic_calendar(clinic_id,weekly_hours_json,exceptions_json,updated_by,updated_at,version) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(clinic_id) DO UPDATE SET weekly_hours_json=excluded.weekly_hours_json,"
                "exceptions_json=excluded.exceptions_json,updated_by=excluded.updated_by,updated_at=excluded.updated_at,"
                "version=excluded.version",
                (clinic_id, encode_json(weekly),
                 encode_json([self._exception_row(d, v) for d, v in sorted(parsed_exceptions.items())]),
                 actor_id, now, version))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="clinic_calendar", aggregate_id=clinic_id,
                               action="clinic_calendar.updated", occurred_at=now,
                               payload={"weekly_days": len(weekly), "exception_days": len(parsed_exceptions), "version": version})
        return self.get_calendar(clinic_id, actor_id)

    def get_calendar(self, clinic_id: str, actor_id: str) -> dict:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "clinical:read", clinic_id=clinic_id)
            calendar, zone = self._calendar_for(connection, clinic_id)
            data = calendar.summary()
            data.update({"clinic_id": clinic_id, "timezone": zone.key})
            row = connection.execute("SELECT version,updated_at,updated_by FROM clinic_calendar WHERE clinic_id=?",
                                     (clinic_id,)).fetchone()
            if row:
                data["version"] = row["version"]
                data["updated_at"] = row["updated_at"]
                data["updated_by"] = row["updated_by"]
            else:
                data["version"] = 0
                data["note"] = "未配置自定义日历，使用内置默认班次"
            return data

    @staticmethod
    def _parse_weekly(value: object) -> dict[str, list[int] | None]:
        if not isinstance(value, dict):
            raise ValidationError("每周班次必须为对象")
        result: dict[str, list[int] | None] = {}
        for day in WEEKDAYS:
            if day not in value:
                raise ValidationError(f"缺少 {day} 的班次")
            window = value[day]
            if window is None:
                result[day] = None
                continue
            object_value(window, f"{day} 班次", allowed={"open", "close"})
            opening = _clock_minutes(window.get("open"), f"{day} 开门时间")
            closing = _clock_minutes(window.get("close"), f"{day} 关门时间")
            if closing <= opening:
                raise ValidationError(f"{day} 关门时间必须晚于开门时间")
            result[day] = [opening, closing]
        extra = set(value) - set(WEEKDAYS)
        if extra:
            raise ValidationError("每周班次包含未知星期", details={"fields": sorted(extra)})
        if not any(window is not None for window in result.values()):
            raise ValidationError("每周至少需要安排一个营业日，否则响应时限无法计算")
        return result

    @staticmethod
    def _parse_exceptions(value: object) -> dict[date, tuple[str, object]]:
        if not isinstance(value, list):
            raise ValidationError("例假日必须为列表")
        if len(value) > 366:
            raise ValidationError("单次配置例假日不得超过 366 天")
        result: dict[date, tuple[str, object]] = {}
        for item in value:
            object_value(item, "例假日", allowed={"date", "status", "open", "close", "reason"})
            day = date.fromisoformat(calendar_date(item.get("date"), "例假日日期"))
            status = choice(item.get("status"), "例假日状态", {"open", "closed"})
            if day in result:
                raise ValidationError("同一日期不能配置两条例外", details={"date": day.isoformat()})
            if status == "closed":
                reason = text(item.get("reason", ""), "停业原因", minimum=1, maximum=400)
                result[day] = ("closed", reason)
            else:
                opening = _clock_minutes(item.get("open"), "假日开门时间")
                closing = _clock_minutes(item.get("close"), "假日关门时间")
                if closing <= opening:
                    raise ValidationError("假日关门时间必须晚于开门时间")
                result[day] = ("open", DayOpening(opening, closing))
        return result

    @staticmethod
    def _exception_row(day: date, value: tuple[str, object]) -> dict:
        kind, payload = value
        if kind == "closed":
            return {"date": day.isoformat(), "status": "closed", "reason": payload}
        opening: DayOpening = payload  # type: ignore[assignment]
        return {"date": day.isoformat(), "status": "open", "open": opening.open_minute, "close": opening.close_minute}

    def _calendar_for(self, connection, clinic_id: str) -> tuple[Calendar, ZoneInfo]:
        zone = self._clinic_zone(connection, clinic_id)
        row = connection.execute("SELECT weekly_hours_json,exceptions_json FROM clinic_calendar WHERE clinic_id=?",
                                 (clinic_id,)).fetchone()
        if row is None:
            return Calendar(dict(DEFAULT_WEEKLY)), zone
        weekly_raw = decode_json(row["weekly_hours_json"])
        weekly = {day: (tuple(window) if window else None) for day, window in weekly_raw.items()}
        exceptions: dict[date, tuple[str, object]] = {}
        for item in decode_json(row["exceptions_json"]):
            day = date.fromisoformat(item["date"])
            if item["status"] == "closed":
                exceptions[day] = ("closed", item["reason"])
            else:
                exceptions[day] = ("open", DayOpening(int(item["open"]), int(item["close"])))
        return Calendar(weekly, exceptions), zone

    @staticmethod
    def _clinic_zone(connection, clinic_id: str) -> ZoneInfo:
        row = connection.execute("SELECT timezone FROM clinics WHERE id=?", (clinic_id,)).fetchone()
        if row is None:
            raise NotFound("诊所不存在")
        return ZoneInfo(row["timezone"])

    # ------------------------------------------------------------------ 策略

    def put_policies(self, clinic_id: str, actor_id: str, policies: list[dict]) -> dict:
        if not isinstance(policies, list) or not policies:
            raise ValidationError("至少需要一条响应时限策略")
        normalized = []
        seen = set()
        for item in policies:
            object_value(item, "时限策略", allowed={"severity", "response_minutes", "escalation_grace_minutes"})
            severity = choice(item.get("severity"), "事件等级", set(SEVERITIES))
            if severity in seen:
                raise ValidationError("同一等级只能配置一条策略", details={"severity": severity})
            seen.add(severity)
            response = integer(item.get("response_minutes"), "响应时限（分钟）", minimum=1, maximum=100000)
            grace = integer(item.get("escalation_grace_minutes"), "升级宽限（分钟）", minimum=0, maximum=100000)
            normalized.append((severity, response, grace))
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "clinic:manage", clinic_id=clinic_id)
            self._clinic_zone(connection, clinic_id)
            for severity, response, grace in normalized:
                existing = connection.execute("SELECT version FROM incident_sla_policies WHERE clinic_id=? AND severity=?",
                                              (clinic_id, severity)).fetchone()
                version = (existing["version"] if existing else 0) + 1
                connection.execute(
                    "INSERT INTO incident_sla_policies(clinic_id,severity,response_minutes,escalation_grace_minutes,active,"
                    "updated_by,created_at,updated_at,version) VALUES(?,?,?,?,1,?,?,?,?) "
                    "ON CONFLICT(clinic_id,severity) DO UPDATE SET response_minutes=excluded.response_minutes,"
                    "escalation_grace_minutes=excluded.escalation_grace_minutes,active=1,updated_by=excluded.updated_by,"
                    "updated_at=excluded.updated_at,version=excluded.version",
                    (clinic_id, severity, response, grace, actor_id, now, now, version))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="incident_sla_policy", aggregate_id=clinic_id,
                               action="incident_sla_policy.updated", occurred_at=now,
                               payload={"severities": sorted(seen), "count": len(normalized)})
        return self.list_policies(clinic_id, actor_id)

    def list_policies(self, clinic_id: str, actor_id: str) -> dict:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "clinical:read", clinic_id=clinic_id)
            self._clinic_zone(connection, clinic_id)
            rows = connection.execute(
                "SELECT severity,response_minutes,escalation_grace_minutes,active,version,updated_at "
                "FROM incident_sla_policies WHERE clinic_id=? ORDER BY severity", (clinic_id,)).fetchall()
            configured = {row["severity"]: row for row in rows}
            items = []
            for severity in SEVERITIES:
                if severity in configured:
                    row = configured[severity]
                    items.append({"severity": severity, "response_minutes": row["response_minutes"],
                                  "escalation_grace_minutes": row["escalation_grace_minutes"], "active": bool(row["active"]),
                                  "version": row["version"], "updated_at": row["updated_at"], "source": "configured"})
                else:
                    response, grace = DEFAULT_POLICIES[severity]
                    items.append({"severity": severity, "response_minutes": response,
                                  "escalation_grace_minutes": grace, "active": True, "version": 0,
                                  "updated_at": None, "source": "default"})
            return {"clinic_id": clinic_id, "items": items}

    def _policy_for(self, connection, clinic_id: str, severity: str) -> tuple[int, int, int]:
        """返回 (响应分钟, 宽限分钟, 策略版本)；无配置时落库一条内置默认策略。"""
        row = connection.execute(
            "SELECT response_minutes,escalation_grace_minutes,version,active FROM incident_sla_policies "
            "WHERE clinic_id=? AND severity=?", (clinic_id, severity)).fetchone()
        if row is not None and row["active"]:
            return row["response_minutes"], row["escalation_grace_minutes"], row["version"]
        response, grace = DEFAULT_POLICIES[severity]
        now = timestamp(self.clock.now())
        connection.execute(
            "INSERT INTO incident_sla_policies(clinic_id,severity,response_minutes,escalation_grace_minutes,active,"
            "updated_by,created_at,updated_at,version) VALUES(?,?,?,?,1,NULL,?,?,1)",
            (clinic_id, severity, response, grace, now, now))
        audit.append_event(connection, clinic_id=clinic_id, actor_id=None, patient_id=None,
                           aggregate_type="incident_sla_policy", aggregate_id=clinic_id,
                           action="incident_sla_policy.default_seeded", occurred_at=now,
                           payload={"severity": severity, "response_minutes": response,
                                    "escalation_grace_minutes": grace})
        return response, grace, 1

    # ------------------------------------------------------------------ 值班

    def add_oncall(self, clinic_id: str, actor_id: str, staff_id: str, starts_at: str,
                   ends_at: str, note: str | None = None) -> dict:
        start = timestamp(starts_at, "值班开始时间")
        end = timestamp(ends_at, "值班结束时间")
        if parsed_timestamp(end) <= parsed_timestamp(start):
            raise ValidationError("值班结束时间必须晚于开始时间")
        note = text(note or "", "值班说明", minimum=0, maximum=400)
        roster_id = new_id("ocr")
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "clinic:manage", clinic_id=clinic_id)
            member = connection.execute("SELECT role,active FROM staff WHERE id=? AND clinic_id=?", (staff_id, clinic_id)).fetchone()
            if member is None or not member["active"] or member["role"] not in {"clinician", "owner"}:
                raise ValidationError("值班临床负责人必须是在岗医生或诊所负责人")
            overlap = connection.execute(
                "SELECT id FROM on_call_roster WHERE clinic_id=? AND starts_at<? AND ends_at>? ORDER BY id",
                (clinic_id, end, start)).fetchone()
            if overlap:
                raise Conflict("该时段已有值班安排", details={"roster_id": overlap["id"]})
            connection.execute(
                "INSERT INTO on_call_roster(id,clinic_id,staff_id,starts_at,ends_at,note,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)", (roster_id, clinic_id, staff_id, start, end, note, actor_id, now))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="on_call_roster", aggregate_id=roster_id,
                               action="oncall.roster_added", occurred_at=now,
                               payload={"staff_id": staff_id, "starts_at": start, "ends_at": end})
        return {"id": roster_id, "staff_id": staff_id, "starts_at": start, "ends_at": end, "note": note, "version": 1}

    def list_oncall(self, clinic_id: str, actor_id: str, *, at: str | None = None) -> dict:
        moment = timestamp(parsed_timestamp(at) if at else self.clock.now())
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "clinical:read", clinic_id=clinic_id)
            rows = connection.execute(
                "SELECT r.id,r.staff_id,s.display_name,s.role,r.starts_at,r.ends_at,r.note FROM on_call_roster r "
                "JOIN staff s ON s.id=r.staff_id WHERE r.clinic_id=? AND r.ends_at>? ORDER BY r.starts_at,r.id",
                (clinic_id, moment)).fetchall()
            default = connection.execute(
                "SELECT d.staff_id,s.display_name,s.role,d.updated_at FROM clinic_oncall_default d "
                "JOIN staff s ON s.id=d.staff_id WHERE d.clinic_id=?", (clinic_id,)).fetchone()
            current = None
            for row in rows:
                if row["starts_at"] <= moment < row["ends_at"]:
                    current = {"staff_id": row["staff_id"], "display_name": row["display_name"], "role": row["role"]}
                    break
            return {"clinic_id": clinic_id, "as_of": moment, "current": current,
                    "default": ({"staff_id": default["staff_id"], "display_name": default["display_name"],
                                 "role": default["role"], "updated_at": default["updated_at"]} if default else None),
                    "upcoming": [dict(row) for row in rows]}

    def set_default_oncall(self, clinic_id: str, actor_id: str, staff_id: str) -> dict:
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "clinic:manage", clinic_id=clinic_id)
            member = connection.execute("SELECT role,active FROM staff WHERE id=? AND clinic_id=?", (staff_id, clinic_id)).fetchone()
            if member is None or not member["active"] or member["role"] not in {"clinician", "owner"}:
                raise ValidationError("默认值班人必须是在岗医生或诊所负责人")
            existing = connection.execute("SELECT version FROM clinic_oncall_default WHERE clinic_id=?", (clinic_id,)).fetchone()
            version = (existing["version"] if existing else 0) + 1
            connection.execute(
                "INSERT INTO clinic_oncall_default(clinic_id,staff_id,updated_by,updated_at,version) VALUES(?,?,?,?,?) "
                "ON CONFLICT(clinic_id) DO UPDATE SET staff_id=excluded.staff_id,updated_by=excluded.updated_by,"
                "updated_at=excluded.updated_at,version=excluded.version", (clinic_id, staff_id, actor_id, now, version))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="clinic_oncall_default", aggregate_id=clinic_id,
                               action="oncall.default_set", occurred_at=now,
                               payload={"staff_id": staff_id, "version": version})
        return {"staff_id": staff_id, "version": version, "updated_at": now}

    def _resolve_oncall(self, connection, clinic_id: str, moment: str) -> tuple[str | None, str, str | None]:
        """返回 (员工编号, 依据 roster/default/fallback_owner, 值班安排编号)。"""
        roster = connection.execute(
            "SELECT id,staff_id FROM on_call_roster WHERE clinic_id=? AND starts_at<=? AND ends_at>? "
            "ORDER BY starts_at DESC,id LIMIT 1", (clinic_id, moment, moment)).fetchone()
        if roster:
            return roster["staff_id"], "roster", roster["id"]
        default = connection.execute("SELECT staff_id FROM clinic_oncall_default WHERE clinic_id=?", (clinic_id,)).fetchone()
        if default:
            return default["staff_id"], "default", None
        owner = connection.execute(
            "SELECT id FROM staff WHERE clinic_id=? AND role='owner' AND active=1 ORDER BY created_at,id LIMIT 1",
            (clinic_id,)).fetchone()
        if owner:
            return owner["id"], "fallback_owner", None
        return None, "unresolved", None

    # ------------------------------------------------------------- 开启计时器

    def open_for_incident(self, connection, *, clinic_id: str, incident_id: str, severity: str,
                          opened_at: str) -> None:
        """在上报事务内调用，随报告一起建立计时器。"""
        response_minutes, grace_minutes, policy_version = self._policy_for(connection, clinic_id, severity)
        calendar, zone = self._calendar_for(connection, clinic_id)
        start = parsed_timestamp(opened_at)
        budget = response_minutes * 60
        deadline = _ts(calendar.advance(start, budget, business=True, zone=zone))
        connection.execute(
            "INSERT INTO incident_slas(incident_id,clinic_id,severity,reported_severity,policy_version,response_minutes,"
            "escalation_grace_minutes,budget_seconds,status,escalation_stage,opened_at,running_from,elapsed_seconds,"
            "remaining_seconds,deadline_at,created_at,updated_at,version) "
            "VALUES(?,?,?,?,?,?,?,?,'running','none',?,?,0,?,?,?,?,1)",
            (incident_id, clinic_id, severity, severity, policy_version, response_minutes, grace_minutes, budget,
             opened_at, opened_at, budget, deadline, opened_at, opened_at))
        self._timeline(connection, incident_id, "running", "open", None, opened_at, None, 0,
                       "事件上报，响应时限开始按营业时间累计",
                       {"trigger": "incident.reported", "severity": severity, "policy_version": policy_version,
                        "response_minutes": response_minutes,
                        "escalation_grace_minutes": grace_minutes, "deadline_at": deadline})

    # --------------------------------------------------------- 状态变化挂钩

    def on_state_transition(self, connection, *, clinic_id: str, incident_id: str, from_state: str,
                            to_state: str, actor_id: str | None, note: str, at: str) -> None:
        sla = connection.execute("SELECT * FROM incident_slas WHERE incident_id=?", (incident_id,)).fetchone()
        if sla is None:
            return
        if from_state in {"resolved", "closed"}:
            # 重新开启：保留已消耗营业秒与升级阶段，按剩余时限恢复计时，不能靠重开刷新。
            self._reopen_running(connection, sla, actor_id, at,
                                 {"trigger": "state_change", "from": from_state, "to": to_state, "note": note})
            return
        rule = TIMER_ON_STATE.get(to_state)
        if rule == "acknowledge":
            self._acknowledge(connection, sla, actor_id, at,
                              {"trigger": "state_change", "from": from_state, "to": to_state, "note": note},
                              reason="指定负责人已确认，响应计时结束，升级阶段锁定")
        elif rule == "pause" and sla["status"] == "running":
            # 已确认的事件进入观察时计时器保持已确认；只有未确认直接进入观察才暂停。
            self._pause_segment(connection, sla, actor_id, at, "pause",
                                {"trigger": "state_change", "from": from_state, "to": to_state, "note": note},
                                audit_action="incident_sla.state_paused",
                                reason="事件进入观察状态，等待患者或外部信息，计时暂停")
        elif rule == "stop" and sla["status"] not in {"stopped", "acknowledged"}:
            self._stop_segment(connection, sla, actor_id, at,
                               {"trigger": "state_change", "from": from_state, "to": to_state, "note": note},
                               audit_action="incident_sla.state_stopped",
                               reason="事件进入终态，计时结束")
        elif rule == "stop" and sla["status"] == "acknowledged":
            self._stop_from_acknowledged(connection, sla, actor_id, at,
                                         {"trigger": "state_change", "from": from_state, "to": to_state, "note": note})

    def pause_timer(self, clinic_id: str, actor_id: str, incident_id: str, reason: str) -> dict:
        reason = text(reason, "暂停原因", maximum=600)
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            sla = self._load_authorized(connection, clinic_id, actor_id, incident_id)
            self._pause_segment(connection, sla, actor_id, now, "pause",
                                {"trigger": "manual", "note": reason},
                                audit_action="incident_sla.paused", reason=f"计时手动暂停：{reason}")
        return self.status(clinic_id, actor_id, incident_id)

    def resume_timer(self, clinic_id: str, actor_id: str, incident_id: str, reason: str) -> dict:
        reason = text(reason, "恢复原因", maximum=600)
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            sla = self._load_authorized(connection, clinic_id, actor_id, incident_id)
            self._resume_segment(connection, sla, actor_id, now,
                                 {"trigger": "manual", "note": reason},
                                 audit_action="incident_sla.resumed", reason=f"计时手动恢复：{reason}")
        return self.status(clinic_id, actor_id, incident_id)

    def _load_authorized(self, connection, clinic_id: str, actor_id: str, incident_id: str):
        principal = principal_for(connection, actor_id, clinic_id)
        authorize(principal, "incident:manage", clinic_id=clinic_id)
        sla = connection.execute("SELECT s.* FROM incident_slas s JOIN incidents i ON i.id=s.incident_id "
                                 "JOIN patients p ON p.id=i.patient_id WHERE s.incident_id=? AND p.clinic_id=?",
                                 (incident_id, clinic_id)).fetchone()
        if sla is None:
            raise NotFound("不良事件不存在或尚未建立响应时限")
        return sla

    # ------------------------------------------------------------- 等级修订

    def revise_severity(self, clinic_id: str, actor_id: str, incident_id: str, new_severity: str,
                        note: str, expected_version: int) -> dict:
        new_severity = choice(new_severity, "事件等级", set(SEVERITIES))
        note = text(note, "修订说明", maximum=2000)
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "incident:manage", clinic_id=clinic_id)
            row = connection.execute("SELECT i.*,p.clinic_id AS clinic FROM incidents i JOIN patients p ON p.id=i.patient_id "
                                     "WHERE i.id=? AND p.clinic_id=?", (incident_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("不良事件不存在")
            if row["state"] == "closed":
                raise Conflict("已关闭事件不能修订等级；如需继续处理请先重新开启")
            if row["version"] != expected_version:
                from .validation import require_match
                require_match(row["version"], expected_version, "不良事件")
            if new_severity == row["severity"]:
                raise Conflict("新等级与当前等级相同")
            sla = connection.execute("SELECT * FROM incident_slas WHERE incident_id=?", (incident_id,)).fetchone()
            locked = sla is not None and sla["escalation_stage"] != "none"
            # 临床等级始终修订留痕；锁定后计时器不跟随变化。
            connection.execute("UPDATE incidents SET severity=?,version=version+1 WHERE id=?",
                               (new_severity, incident_id))
            sequence = connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM incident_events WHERE incident_id=?",
                                          (incident_id,)).fetchone()[0]
            connection.execute(
                "INSERT INTO incident_events(id,incident_id,event_type,actor_id,note,created_at,sequence) VALUES(?,?,?,?,?,?,?)",
                (new_id("iev"), incident_id, "severity_revised", actor_id,
                 f"{row['severity']} → {new_severity}：{note}", now, sequence))
            adjusted = False
            if sla is not None and not locked and new_severity != sla["severity"]:
                response_minutes, grace_minutes, policy_version = self._policy_for(connection, clinic_id, new_severity)
                calendar, zone = self._calendar_for(connection, clinic_id)
                elapsed = self._elapsed(connection, sla, now, calendar, zone)
                budget = response_minutes * 60
                deadline = None
                if sla["status"] == "running":
                    deadline = _ts(calendar.advance(parsed_timestamp(now),
                                                     max(0, budget - elapsed), business=True, zone=zone))
                connection.execute(
                    "UPDATE incident_slas SET severity=?,policy_version=?,response_minutes=?,escalation_grace_minutes=?,"
                    "budget_seconds=?,elapsed_seconds=?,remaining_seconds=?,deadline_at=?,updated_at=?,version=version+1 "
                    "WHERE incident_id=?",
                    (new_severity, policy_version, response_minutes, grace_minutes, budget, elapsed,
                     max(0, budget - elapsed), deadline, now, incident_id))
                adjusted = True
                self._timeline(connection, incident_id, sla["status"], "severity_revision", actor_id,
                               sla["running_from"] or now, None, elapsed,
                               f"等级由 {sla['severity']} 修订为 {new_severity}，尚未升级，按新策略重算剩余时限",
                               {"trigger": "severity_revision", "from": sla["severity"], "to": new_severity,
                                "policy_version": policy_version, "locked": False, "deadline_at": deadline})
            elif locked:
                self._timeline(connection, incident_id, sla["status"], "severity_revision", actor_id,
                               sla["running_from"] or now, None, sla["elapsed_seconds"],
                               f"迟到的等级修订（{row['severity']} → {new_severity}）到达时事件已处于"
                               f"{sla['escalation_stage']} 阶段，时限、责任人与升级阶段保持锁定",
                               {"trigger": "severity_revision", "from": row["severity"], "to": new_severity,
                                "locked": True, "escalation_stage": sla["escalation_stage"],
                                "locked_severity": sla["severity"]})
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=row["patient_id"],
                               aggregate_type="incident", aggregate_id=incident_id,
                               action="incident.severity_revised", occurred_at=now,
                               payload={"from": row["severity"], "to": new_severity, "locked": bool(locked),
                                        "sla_adjusted": adjusted, "note": note, "version": expected_version + 1})
        return self.status(clinic_id, actor_id, incident_id)

    # ---------------------------------------------------------------- 转派

    def reassign(self, clinic_id: str, actor_id: str, incident_id: str, to_staff_id: str,
                 reason: str, expected_version: int) -> dict:
        reason = text(reason, "转派原因", maximum=1000)
        now = timestamp(self.clock.now())
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "incident:manage", clinic_id=clinic_id)
            row = connection.execute("SELECT i.*,p.clinic_id AS clinic FROM incidents i JOIN patients p ON p.id=i.patient_id "
                                     "WHERE i.id=? AND p.clinic_id=?", (incident_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("不良事件不存在")
            if row["state"] in {"resolved", "closed"}:
                raise Conflict("已结束事件不能转派；如需继续处理请先重新开启")
            if row["version"] != expected_version:
                from .validation import require_match
                require_match(row["version"], expected_version, "不良事件")
            target = connection.execute("SELECT role,active,display_name FROM staff WHERE id=? AND clinic_id=?",
                                        (to_staff_id, clinic_id)).fetchone()
            if target is None or not target["active"] or target["role"] not in {"clinician", "nurse", "owner"}:
                raise ValidationError("接收人必须是在岗临床岗位")
            if row["assigned_to"] == to_staff_id:
                raise Conflict("事件已分配给该负责人")
            packet = self._handoff(connection, row, actor_id, to_staff_id, reason, now, basis="manual_reassign")
            connection.execute("UPDATE incidents SET assigned_to=?,version=version+1 WHERE id=?",
                               (to_staff_id, incident_id))
            sequence = connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM incident_events WHERE incident_id=?",
                                          (incident_id,)).fetchone()[0]
            connection.execute(
                "INSERT INTO incident_events(id,incident_id,event_type,actor_id,note,created_at,sequence) VALUES(?,?,?,?,?,?,?)",
                (new_id("iev"), incident_id, "reassigned", actor_id,
                 f"转派给 {target['display_name']}（{to_staff_id}）：{reason}", now, sequence))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=row["patient_id"],
                               aggregate_type="incident", aggregate_id=incident_id,
                               action="incident.reassigned", occurred_at=now,
                               payload={"from_assignee": row["assigned_to"], "to_assignee": to_staff_id,
                                        "handoff_id": packet["id"], "reason": reason,
                                        "remaining_business_seconds": packet["remaining_business_seconds"],
                                        "deadline_at": packet["deadline_at"],
                                        "escalation_stage": packet["escalation_stage"],
                                        "version": expected_version + 1})
        return self.status(clinic_id, actor_id, incident_id)

    def _handoff(self, connection, incident_row, actor_id: str | None, to_staff_id: str, reason: str,
                 now: str, *, basis: str) -> dict:
        sla = connection.execute("SELECT * FROM incident_slas WHERE incident_id=?", (incident_row["id"],)).fetchone()
        clinic_id = sla["clinic_id"] if sla is not None else connection.execute(
            "SELECT clinic_id FROM patients WHERE id=?", (incident_row["patient_id"],)).fetchone()[0]
        calendar, zone = self._calendar_for(connection, clinic_id)
        actions = [
            {"sequence": event["sequence"], "type": event["event_type"], "actor_id": event["actor_id"],
             "note": event["note"], "created_at": event["created_at"]}
            for event in connection.execute(
                "SELECT * FROM incident_events WHERE incident_id=? ORDER BY sequence", (incident_row["id"],)).fetchall()
        ]
        remaining = deadline = escalate_after = None
        stage = "none"
        if sla is not None:
            remaining = self._remaining_seconds(connection, sla, now, calendar, zone)
            deadline = sla["deadline_at"]
            escalate_after = sla["escalate_after_at"]
            stage = sla["escalation_stage"]
        sequence = connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM incident_handoffs WHERE incident_id=?",
                                      (incident_row["id"],)).fetchone()[0]
        handoff_id = new_id("hnd")
        connection.execute(
            "INSERT INTO incident_handoffs(id,incident_id,sequence,from_assignee,to_assignee,actor_id,severity,"
            "incident_state,summary,reported_at,reported_by,actions_json,remaining_business_minutes,deadline_at,"
            "escalate_after_at,escalation_stage,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (handoff_id, incident_row["id"], sequence, incident_row["assigned_to"], to_staff_id, actor_id,
             incident_row["severity"], incident_row["state"], incident_row["summary"], incident_row["reported_at"],
             incident_row["reported_by"], encode_json(actions),
             (remaining + 59) // 60 if remaining is not None else 0, deadline, escalate_after, stage, reason, now))
        self._timeline(connection, incident_row["id"], sla["status"] if sla else "stopped", "reassign", actor_id,
                       sla["running_from"] or now if sla else now, None, sla["elapsed_seconds"] if sla else 0,
                       f"转派交接（{basis}）：原始报告、已采取措施与剩余营业时限随事件交接，升级阶段不回退",
                       {"trigger": basis, "from_assignee": incident_row["assigned_to"], "to_assignee": to_staff_id,
                        "handoff_id": handoff_id, "remaining_business_seconds": remaining,
                        "deadline_at": deadline, "escalate_after_at": escalate_after, "escalation_stage": stage})
        return {"id": handoff_id, "remaining_business_seconds": remaining, "deadline_at": deadline,
                "escalation_stage": stage}

    def list_handoffs(self, clinic_id: str, actor_id: str, incident_id: str) -> dict:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "incident:manage", clinic_id=clinic_id)
            row = connection.execute("SELECT i.id FROM incidents i JOIN patients p ON p.id=i.patient_id "
                                     "WHERE i.id=? AND p.clinic_id=?", (incident_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("不良事件不存在")
            rows = connection.execute("SELECT * FROM incident_handoffs WHERE incident_id=? ORDER BY sequence",
                                      (incident_id,)).fetchall()
            return {"incident_id": incident_id, "items": [{
                "id": r["id"], "sequence": r["sequence"], "from_assignee": r["from_assignee"],
                "to_assignee": r["to_assignee"], "actor_id": r["actor_id"], "severity": r["severity"],
                "incident_state": r["incident_state"], "original_report": {
                    "summary": r["summary"], "reported_at": r["reported_at"], "reported_by": r["reported_by"]},
                "actions_taken": decode_json(r["actions_json"]),
                "remaining_business_minutes": r["remaining_business_minutes"], "deadline_at": r["deadline_at"],
                "escalate_after_at": r["escalate_after_at"], "escalation_stage": r["escalation_stage"],
                "reason": r["reason"], "created_at": r["created_at"]} for r in rows]}

    # ------------------------------------------------------------- 到期扫描

    def scan_due(self, clinic_id: str, actor_id: str, *, limit: int = 200) -> dict:
        """幂等扫描：升级记录有唯一约束，可安全重复执行、重启后补扫或批量扫描。"""
        if not 1 <= limit <= 1000:
            raise ValidationError("扫描数量必须为 1 至 1000")
        now = timestamp(self.clock.now())
        reminded, escalated = [], []
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "incident:manage", clinic_id=clinic_id)
            candidates = connection.execute(
                "SELECT s.* FROM incident_slas s WHERE s.clinic_id=? AND s.status='running' "
                "AND ((s.escalation_stage='none' AND s.deadline_at<=?) "
                "OR (s.escalation_stage='reminded' AND s.escalate_after_at<=?)) "
                "ORDER BY s.deadline_at,s.incident_id LIMIT ?",
                (clinic_id, now, now, limit)).fetchall()
            for sla in candidates:
                if sla["escalation_stage"] == "none" and sla["deadline_at"] <= now:
                    item = self._fire_reminder(connection, sla, now)
                    if item:
                        reminded.append(item)
                        # 停机补扫：宽限也已过时，同一事务内继续升级（唯一约束仍保证幂等）。
                        if item["escalate_after_at"] <= now:
                            fresh = connection.execute(
                                "SELECT * FROM incident_slas WHERE incident_id=?", (sla["incident_id"],)).fetchone()
                            escalated_item = self._fire_escalation(connection, fresh, now)
                            if escalated_item:
                                escalated.append(escalated_item)
                elif sla["escalation_stage"] == "reminded" and sla["escalate_after_at"] and sla["escalate_after_at"] <= now:
                    item = self._fire_escalation(connection, sla, now)
                    if item:
                        escalated.append(item)
        return {"clinic_id": clinic_id, "as_of": now, "reminded": reminded, "escalated": escalated,
                "scanned": len(candidates)}

    def _fire_reminder(self, connection, sla, now: str) -> dict | None:
        incident = connection.execute("SELECT * FROM incidents WHERE id=?", (sla["incident_id"],)).fetchone()
        target_id = incident["assigned_to"] or incident["reported_by"]
        target_basis = "assignee" if incident["assigned_to"] else "reporter_fallback"
        nonce = new_id("esn")
        inserted = connection.execute(
            "INSERT OR IGNORE INTO incident_sla_escalations(incident_id,stage,assignee_id,due_at,fired_at,nonce,created_at) "
            "VALUES(?, 'reminded', ?, ?, ?, ?, ?)",
            (sla["incident_id"], target_id, sla["deadline_at"], now, nonce, now)).rowcount
        if not inserted:
            return None  # 进程重启或重复扫描：该阶段已经触发过。
        # 宽限按挂钟时间累计，跨班与关门后的等待同样计入。
        escalate_after = _ts(parsed_timestamp(sla["deadline_at"])
                             + timedelta(minutes=sla["escalation_grace_minutes"]))
        connection.execute(
            "UPDATE incident_slas SET escalation_stage='reminded',escalate_after_at=?,updated_at=?,version=version+1 "
            "WHERE incident_id=?", (escalate_after, now, sla["incident_id"]))
        self._append_incident_event(
            connection, sla["incident_id"], "sla_reminded", None,
            f"响应时限已到（{sla['deadline_at']}），提醒当前负责人；{sla['escalation_grace_minutes']} 分钟宽限后仍未确认将升级",
            now)
        connection.execute("UPDATE incidents SET version=version+1 WHERE id=?", (sla["incident_id"],))
        self._timeline(connection, sla["incident_id"], "running", "remind", None, sla["running_from"], None,
                       sla["elapsed_seconds"], "响应时限到期，提醒当前负责人",
                       {"trigger": "deadline", "deadline_at": sla["deadline_at"], "notified": target_id,
                        "target_basis": target_basis, "grace_minutes": sla["escalation_grace_minutes"],
                        "grace_basis": "wall_clock", "escalate_after_at": escalate_after, "nonce": nonce})
        audit.append_event(connection, clinic_id=sla["clinic_id"], actor_id=None, patient_id=incident["patient_id"],
                           aggregate_type="incident_sla", aggregate_id=sla["incident_id"],
                           action="incident_sla.reminded", occurred_at=now,
                           payload={"deadline_at": sla["deadline_at"], "notified": target_id,
                                    "target_basis": target_basis, "escalate_after_at": escalate_after, "nonce": nonce})
        return {"incident_id": sla["incident_id"], "stage": "reminded", "notified": target_id,
                "target_basis": target_basis, "deadline_at": sla["deadline_at"],
                "escalate_after_at": escalate_after, "fired_at": now}

    def _fire_escalation(self, connection, sla, now: str) -> dict | None:
        target_id, target_basis, roster_id = self._resolve_oncall(connection, sla["clinic_id"], now)
        if target_id is None:
            # 没有可升级对象时不写升级记录；值班配置补齐后下次扫描触发。
            return None
        nonce = new_id("esn")
        inserted = connection.execute(
            "INSERT OR IGNORE INTO incident_sla_escalations(incident_id,stage,assignee_id,due_at,fired_at,nonce,created_at) "
            "VALUES(?, 'escalated', ?, ?, ?, ?, ?)",
            (sla["incident_id"], target_id, sla["escalate_after_at"] or now, now, nonce, now)).rowcount
        if not inserted:
            return None
        incident = connection.execute("SELECT * FROM incidents WHERE id=?", (sla["incident_id"],)).fetchone()
        self._append_incident_event(
            connection, incident["id"], "sla_escalated", None,
            f"宽限到期（{sla['escalate_after_at']}），升级至值班临床负责人 {target_id}（依据：{target_basis}）",
            now)
        packet = self._handoff(connection, incident, None, target_id,
                               "响应宽限到期，系统升级给值班临床负责人", now, basis="sla_escalation")
        connection.execute("UPDATE incidents SET assigned_to=?,version=version+1 WHERE id=?",
                           (target_id, incident["id"]))
        connection.execute(
            "UPDATE incident_slas SET escalation_stage='escalated',escalated_to=?,updated_at=?,version=version+1 "
            "WHERE incident_id=?", (target_id, now, incident["id"]))
        self._timeline(connection, incident["id"], "running", "escalate", None, sla["running_from"], None,
                       sla["elapsed_seconds"], "宽限到期，升级至值班临床负责人并交接完整资料",
                       {"trigger": "grace_deadline", "escalate_after_at": sla["escalate_after_at"],
                        "escalated_to": target_id, "target_basis": target_basis, "roster_id": roster_id,
                        "handoff_id": packet["id"], "nonce": nonce})
        audit.append_event(connection, clinic_id=sla["clinic_id"], actor_id=None, patient_id=incident["patient_id"],
                           aggregate_type="incident_sla", aggregate_id=incident["id"],
                           action="incident_sla.escalated", occurred_at=now,
                           payload={"escalate_after_at": sla["escalate_after_at"], "escalated_to": target_id,
                                    "target_basis": target_basis, "roster_id": roster_id,
                                    "handoff_id": packet["id"], "nonce": nonce})
        return {"incident_id": incident["id"], "stage": "escalated", "escalated_to": target_id,
                "target_basis": target_basis, "roster_id": roster_id, "handoff_id": packet["id"], "fired_at": now}

    def _append_incident_event(self, connection, incident_id: str, event_type: str, actor_id: str | None,
                               note: str, at: str) -> None:
        sequence = connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM incident_events WHERE incident_id=?",
                                      (incident_id,)).fetchone()[0]
        connection.execute(
            "INSERT INTO incident_events(id,incident_id,event_type,actor_id,note,created_at,sequence) VALUES(?,?,?,?,?,?,?)",
            (new_id("iev"), incident_id, event_type, actor_id, note, at, sequence))

    # ------------------------------------------------------------- 工作队列

    def queue(self, clinic_id: str, actor_id: str, *, limit: int = 200) -> dict:
        if not 1 <= limit <= 1000:
            raise ValidationError("查询数量必须为 1 至 1000")
        now = timestamp(self.clock.now())
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "incident:manage", clinic_id=clinic_id)
            calendar, zone = self._calendar_for(connection, clinic_id)
            rows = connection.execute(
                "SELECT s.*,i.patient_id,i.assigned_to,i.state AS incident_state,i.severity AS current_severity,"
                "i.category,p.external_ref FROM incident_slas s JOIN incidents i ON i.id=s.incident_id "
                "JOIN patients p ON p.id=i.patient_id WHERE s.clinic_id=? AND i.state NOT IN ('resolved','closed') "
                "ORDER BY (s.deadline_at IS NULL),s.deadline_at,s.incident_id LIMIT ?",
                (clinic_id, limit)).fetchall()
            items = []
            for row in rows:
                elapsed = self._elapsed(connection, row, now, calendar, zone)
                remaining = max(0, row["budget_seconds"] - elapsed)
                items.append({"incident_id": row["incident_id"], "patient_id": row["patient_id"],
                              "patient_ref": row["external_ref"], "category": row["category"],
                              "severity": row["current_severity"], "sla_severity": row["severity"],
                              "incident_state": row["incident_state"], "assigned_to": row["assigned_to"],
                              "timer_status": row["status"], "escalation_stage": row["escalation_stage"],
                              "opened_at": row["opened_at"], "deadline_at": row["deadline_at"],
                              "escalate_after_at": row["escalate_after_at"],
                              "elapsed_business_minutes": elapsed // 60,
                              "remaining_business_minutes": (remaining + 59) // 60,
                              "overdue": row["status"] == "running" and now >= (row["deadline_at"] or now)})
            return {"clinic_id": clinic_id, "as_of": now, "returned": len(items), "items": items}

    def simulate(self, clinic_id: str, actor_id: str, as_of: str) -> dict:
        """按指定时刻只读预演：不写升级记录、不改责任人，只投影当时的阶段。"""
        moment = timestamp(as_of, "预演时刻")
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "incident:manage", clinic_id=clinic_id)
            rows = connection.execute(
                "SELECT s.*,i.patient_id,i.assigned_to,i.state AS incident_state,i.severity AS current_severity,"
                "i.category,p.external_ref FROM incident_slas s JOIN incidents i ON i.id=s.incident_id "
                "JOIN patients p ON p.id=i.patient_id WHERE s.clinic_id=? AND i.state NOT IN ('resolved','closed') "
                "ORDER BY s.opened_at,s.incident_id", (clinic_id,)).fetchall()
            items = []
            for row in rows:
                projected = "none"
                projected_escalate_after = None
                if row["status"] == "running" and row["deadline_at"]:
                    if row["escalation_stage"] == "reminded" and row["escalate_after_at"]:
                        if moment >= row["escalate_after_at"]:
                            projected = "escalate"
                        elif moment >= row["deadline_at"]:
                            projected = "remind"
                    elif moment >= row["deadline_at"]:
                        simulated_after = _ts(parsed_timestamp(row["deadline_at"])
                                              + timedelta(minutes=row["escalation_grace_minutes"]))
                        projected_escalate_after = simulated_after
                        projected = "escalate" if moment >= simulated_after else "remind"
                effective_stage = max_stage(row["escalation_stage"],
                                            {"none": "none", "remind": "reminded",
                                             "escalate": "escalated"}[projected]
                                            if row["status"] == "running" else "none")
                target_id = basis = roster_id = None
                if effective_stage == "escalated":
                    if row["escalation_stage"] == "escalated":
                        target_id, basis = row["escalated_to"], "actual"
                    else:
                        target_id, basis, roster_id = self._resolve_oncall(connection, clinic_id, moment)
                items.append({"incident_id": row["incident_id"], "patient_ref": row["external_ref"],
                              "category": row["category"], "severity": row["current_severity"],
                              "sla_severity": row["severity"], "incident_state": row["incident_state"],
                              "assigned_to": row["assigned_to"], "timer_status": row["status"],
                              "actual_stage": row["escalation_stage"], "projected_stage": projected,
                              "effective_stage": effective_stage,
                              "would_remind": row["escalation_stage"] == "none" and projected in {"remind", "escalate"},
                              "would_escalate": row["escalation_stage"] != "escalated" and projected == "escalate",
                              "projected_escalate_after_at": projected_escalate_after or row["escalate_after_at"],
                              "projected_escalate_to": target_id, "target_basis": basis, "roster_id": roster_id})
            return {"clinic_id": clinic_id, "as_of": moment, "mode": "simulation", "returned": len(items), "items": items}

    # ------------------------------------------------------------- 状态查询

    def status(self, clinic_id: str, actor_id: str, incident_id: str) -> dict:
        now = timestamp(self.clock.now())
        with self.db.transaction(write=False) as connection:
            self._load_authorized(connection, clinic_id, actor_id, incident_id)
            return self._status(connection, incident_id, now)

    def _status(self, connection, incident_id: str, now: str) -> dict:
        sla = connection.execute("SELECT * FROM incident_slas WHERE incident_id=?", (incident_id,)).fetchone()
        if sla is None:
            raise NotFound("响应时限不存在")
        calendar, zone = self._calendar_for(connection, sla["clinic_id"])
        elapsed = self._elapsed(connection, sla, now, calendar, zone)
        remaining = max(0, sla["budget_seconds"] - elapsed)
        timeline = connection.execute("SELECT * FROM incident_sla_timeline WHERE incident_id=? ORDER BY sequence",
                                      (incident_id,)).fetchall()
        escalations = connection.execute("SELECT * FROM incident_sla_escalations WHERE incident_id=? ORDER BY fired_at",
                                         (incident_id,)).fetchall()
        return {"incident_id": incident_id, "severity": sla["severity"], "reported_severity": sla["reported_severity"],
                "policy_version": sla["policy_version"], "response_minutes": sla["response_minutes"],
                "escalation_grace_minutes": sla["escalation_grace_minutes"],
                "timer_status": sla["status"], "escalation_stage": sla["escalation_stage"],
                "opened_at": sla["opened_at"], "running_from": sla["running_from"], "paused_at": sla["paused_at"],
                "closed_at": sla["closed_at"], "deadline_at": sla["deadline_at"],
                "escalate_after_at": sla["escalate_after_at"], "escalated_to": sla["escalated_to"],
                "as_of": now, "elapsed_business_seconds": elapsed,
                "remaining_business_seconds": remaining,
                "escalations": [{"stage": r["stage"], "assignee_id": r["assignee_id"], "due_at": r["due_at"],
                                 "fired_at": r["fired_at"], "nonce": r["nonce"]} for r in escalations],
                "timeline": [{
                    "sequence": r["sequence"], "segment": r["segment"], "action": r["action"],
                    "actor_id": r["actor_id"], "started_at": r["started_at"], "ended_at": r["ended_at"],
                    "business_elapsed_seconds": r["business_elapsed"], "reason": r["reason"],
                    "basis": decode_json(r["basis_json"])} for r in timeline]}

    # ------------------------------------------------------------- 计时原语

    def _elapsed(self, connection, sla, now: str, calendar: Calendar, zone: ZoneInfo) -> int:
        elapsed = sla["elapsed_seconds"]
        if sla["status"] == "running" and sla["running_from"]:
            elapsed += calendar.business_seconds(parsed_timestamp(sla["running_from"]),
                                                 parsed_timestamp(now), zone)
        return elapsed

    def _remaining_seconds(self, connection, sla, now: str, calendar: Calendar, zone: ZoneInfo) -> int:
        return max(0, sla["budget_seconds"] - self._elapsed(connection, sla, now, calendar, zone))

    def _settle(self, connection, sla, at: str, calendar: Calendar, zone: ZoneInfo) -> int:
        """把当前 running 段结算进累计营业秒。"""
        elapsed = sla["elapsed_seconds"]
        if sla["status"] == "running" and sla["running_from"]:
            elapsed += calendar.business_seconds(parsed_timestamp(sla["running_from"]), parsed_timestamp(at), zone)
        return elapsed

    def _pause_segment(self, connection, sla, actor_id: str | None, at: str, timeline_action: str,
                       basis: dict, *, audit_action: str, reason: str) -> None:
        if sla["status"] != "running":
            raise Conflict("计时器当前不在运行，不能暂停")
        calendar, zone = self._calendar_for(connection, sla["clinic_id"])
        elapsed = self._settle(connection, sla, at, calendar, zone)
        connection.execute(
            "UPDATE incident_slas SET status='paused',running_from=NULL,elapsed_seconds=?,remaining_seconds=?,"
            "deadline_at=NULL,escalate_after_at=NULL,paused_at=?,updated_at=?,version=version+1 WHERE incident_id=?",
            (elapsed, max(0, sla["budget_seconds"] - elapsed), at, at, sla["incident_id"]))
        self._timeline(connection, sla["incident_id"], "paused", timeline_action, actor_id, sla["running_from"], at,
                       elapsed, reason, {**basis, "prior_escalate_after_at": sla["escalate_after_at"]})
        audit.append_event(connection, clinic_id=sla["clinic_id"], actor_id=actor_id, patient_id=None,
                           aggregate_type="incident_sla", aggregate_id=sla["incident_id"],
                           action=audit_action, occurred_at=at,
                           payload={**basis, "elapsed_business_seconds": elapsed, "started_at": sla["running_from"],
                                    "ended_at": at})

    def _resume_segment(self, connection, sla, actor_id: str | None, at: str, basis: dict, *,
                        audit_action: str, reason: str) -> None:
        if sla["status"] != "paused":
            raise Conflict("计时器当前未暂停，不能恢复")
        calendar, zone = self._calendar_for(connection, sla["clinic_id"])
        remaining = max(0, sla["budget_seconds"] - sla["elapsed_seconds"])
        deadline = _ts(calendar.advance(parsed_timestamp(at), remaining, business=True, zone=zone))
        # 宽限钟随暂停一起停住：恢复时从当前时刻重新给完整挂钟宽限。
        new_grace_after = None
        if sla["escalation_stage"] == "reminded":
            new_grace_after = _ts(parsed_timestamp(at) + timedelta(minutes=sla["escalation_grace_minutes"]))
        connection.execute(
            "UPDATE incident_slas SET status='running',running_from=?,paused_at=NULL,remaining_seconds=?,"
            "deadline_at=?,escalate_after_at=COALESCE(?,escalate_after_at),updated_at=?,version=version+1 "
            "WHERE incident_id=?",
            (at, remaining, deadline, new_grace_after, at, sla["incident_id"]))
        basis = {**basis, "remaining_business_seconds": remaining, "deadline_at": deadline,
                 "grace_restarted_at": new_grace_after}
        self._timeline(connection, sla["incident_id"], "running", "resume", actor_id, at, None,
                       sla["elapsed_seconds"], reason, basis)
        audit.append_event(connection, clinic_id=sla["clinic_id"], actor_id=actor_id, patient_id=None,
                           aggregate_type="incident_sla", aggregate_id=sla["incident_id"],
                           action=audit_action, occurred_at=at,
                           payload={**basis, "started_at": at})

    def _acknowledge(self, connection, sla, actor_id: str | None, at: str, basis: dict, *, reason: str) -> None:
        if sla["status"] in {"acknowledged", "stopped"}:
            return
        calendar, zone = self._calendar_for(connection, sla["clinic_id"])
        elapsed = self._settle(connection, sla, at, calendar, zone) if sla["status"] == "running" else sla["elapsed_seconds"]
        connection.execute(
            "UPDATE incident_slas SET status='acknowledged',running_from=NULL,elapsed_seconds=?,remaining_seconds=?,"
            "deadline_at=NULL,escalate_after_at=NULL,paused_at=NULL,updated_at=?,version=version+1 WHERE incident_id=?",
            (elapsed, max(0, sla["budget_seconds"] - elapsed), at, sla["incident_id"]))
        self._timeline(connection, sla["incident_id"], "acknowledged", "acknowledge", actor_id,
                       sla["running_from"] or sla["paused_at"] or at, at, elapsed, reason, basis)
        audit.append_event(connection, clinic_id=sla["clinic_id"], actor_id=actor_id, patient_id=None,
                           aggregate_type="incident_sla", aggregate_id=sla["incident_id"],
                           action="incident_sla.acknowledged", occurred_at=at,
                           payload={**basis, "elapsed_business_seconds": elapsed, "escalation_stage": sla["escalation_stage"],
                                    "ended_at": at})

    def _stop_segment(self, connection, sla, actor_id: str | None, at: str, basis: dict, *,
                      audit_action: str, reason: str) -> None:
        if sla["status"] == "stopped":
            return
        calendar, zone = self._calendar_for(connection, sla["clinic_id"])
        elapsed = self._settle(connection, sla, at, calendar, zone) if sla["status"] == "running" else sla["elapsed_seconds"]
        connection.execute(
            "UPDATE incident_slas SET status='stopped',running_from=NULL,elapsed_seconds=?,remaining_seconds=?,"
            "deadline_at=NULL,escalate_after_at=NULL,paused_at=NULL,closed_at=?,updated_at=?,version=version+1 "
            "WHERE incident_id=?",
            (elapsed, max(0, sla["budget_seconds"] - elapsed), at, at, sla["incident_id"]))
        self._timeline(connection, sla["incident_id"], "stopped", "stop", actor_id,
                       sla["running_from"] or sla["paused_at"] or at, at, elapsed, reason, basis)
        audit.append_event(connection, clinic_id=sla["clinic_id"], actor_id=actor_id, patient_id=None,
                           aggregate_type="incident_sla", aggregate_id=sla["incident_id"],
                           action=audit_action, occurred_at=at,
                           payload={**basis, "elapsed_business_seconds": elapsed, "ended_at": at})

    def _stop_from_acknowledged(self, connection, sla, actor_id: str | None, at: str, basis: dict) -> None:
        connection.execute(
            "UPDATE incident_slas SET status='stopped',closed_at=?,updated_at=?,version=version+1 WHERE incident_id=?",
            (at, at, sla["incident_id"]))
        self._timeline(connection, sla["incident_id"], "stopped", "stop", actor_id, at, at,
                       sla["elapsed_seconds"], "已确认事件进入终态，计时结束", basis)
        audit.append_event(connection, clinic_id=sla["clinic_id"], actor_id=actor_id, patient_id=None,
                           aggregate_type="incident_sla", aggregate_id=sla["incident_id"],
                           action="incident_sla.state_stopped", occurred_at=at,
                           payload={**basis, "elapsed_business_seconds": sla["elapsed_seconds"], "ended_at": at})

    def _reopen_running(self, connection, sla, actor_id: str | None, at: str, basis: dict) -> None:
        calendar, zone = self._calendar_for(connection, sla["clinic_id"])
        remaining = max(0, sla["budget_seconds"] - sla["elapsed_seconds"])
        deadline = _ts(calendar.advance(parsed_timestamp(at), remaining, business=True, zone=zone))
        new_grace_after = None
        if sla["escalation_stage"] == "reminded":
            new_grace_after = _ts(parsed_timestamp(at) + timedelta(minutes=sla["escalation_grace_minutes"]))
        connection.execute(
            "UPDATE incident_slas SET status='running',running_from=?,paused_at=NULL,closed_at=NULL,"
            "remaining_seconds=?,deadline_at=?,escalate_after_at=?,updated_at=?,version=version+1 "
            "WHERE incident_id=?",
            (at, remaining, deadline, new_grace_after, at, sla["incident_id"]))
        basis = {**basis, "remaining_business_seconds": remaining, "deadline_at": deadline,
                 "grace_restarted_at": new_grace_after}
        self._timeline(connection, sla["incident_id"], "running", "resume", actor_id, at, None,
                       sla["elapsed_seconds"],
                       "事件重新开启，按剩余营业时限恢复计时；升级阶段保持不回退，已消耗时限不重置",
                       basis)
        audit.append_event(connection, clinic_id=sla["clinic_id"], actor_id=actor_id, patient_id=None,
                           aggregate_type="incident_sla", aggregate_id=sla["incident_id"],
                           action="incident_sla.reopened", occurred_at=at,
                           payload={**basis, "remaining_business_seconds": remaining, "deadline_at": deadline,
                                    "escalation_stage": sla["escalation_stage"], "started_at": at})

    def _timeline(self, connection, incident_id: str, segment: str, action: str, actor_id: str | None,
                  started_at: str, ended_at: str | None, business_elapsed: int, reason: str, basis: dict) -> None:
        sequence = connection.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM incident_sla_timeline WHERE incident_id=?",
                                      (incident_id,)).fetchone()[0]
        connection.execute(
            "INSERT INTO incident_sla_timeline(id,incident_id,sequence,segment,action,actor_id,started_at,ended_at,"
            "business_elapsed,reason,basis_json,occurred_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id("ist"), incident_id, sequence, segment, action, actor_id, started_at, ended_at,
             business_elapsed, reason, encode_json(basis), ended_at or started_at))


def _clock_minutes(value: object, field: str) -> int:
    if not isinstance(value, str):
        raise ValidationError(f"{field}必须为 HH:MM")
    try:
        clock = time.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field}必须为 HH:MM") from exc
    return clock.hour * 60 + clock.minute


def _ts(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


_STAGE_ORDER = {"none": 0, "reminded": 1, "escalated": 2}


def max_stage(a: str, b: str) -> str:
    return a if _STAGE_ORDER[a] >= _STAGE_ORDER[b] else b
