"""不良事件响应时限、营业日历计时、升级链与转派交接。

计时规则：
- 事件报告时按当时策略与营业日历快照启动「首响」计时；负责人确认（分诊）后
  切换为「处置」计时，解决或关闭后停止。
- 暂停以时间段（segment）记账；恢复时把暂停期间经过的营业分钟顺延到截止时间，
  闭馆时间本身不计入营业分钟。
- 每个升级阶段以 (事件, 计时阶段, 阶段序号) 去重，重复或重启后的扫描不会重复
  升级同一阶段；已达到的升级级别单调不降，迟到的低等级更新不能撤销。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import audit
from .db import decode_json, encode_json
from .errors import Conflict, Forbidden, NotFound, ValidationError
from .ids import new_id
from .security import ROLE_PERMISSIONS, authorize, principal_for
from .validation import calendar_date, choice, parsed_timestamp, text, timestamp

SEVERITY_ORDER = {"low": 0, "moderate": 1, "high": 2, "urgent": 3}
ACTIVE_STATES = {"reported", "triaged", "monitoring"}
STAGE_KINDS = {"remind", "escalate"}
STAGE_TARGETS = {"assignee", "duty_clinician"}
_CLINICAL_ROLES = {"clinician", "nurse", "owner"}


def default_calendar_config() -> dict:
    return {"weekday_hours": {str(weekday): [["09:00", "18:00"]] for weekday in range(7)},
            "closures": [], "special_hours": {}}


def default_policy_config() -> dict:
    return {
        "deadlines": {
            "low": {"respond_minutes": 480, "resolve_minutes": 2880},
            "moderate": {"respond_minutes": 240, "resolve_minutes": 1440},
            "high": {"respond_minutes": 60, "resolve_minutes": 480},
            "urgent": {"respond_minutes": 15, "resolve_minutes": 120},
        },
        "stages": [
            {"kind": "remind", "target": "assignee", "offset_minutes": 0},
            {"kind": "escalate", "target": "duty_clinician", "offset_minutes": 30},
        ],
    }


class BusinessCalendar:
    """按诊所本地营业日历计算时间；闭馆时段与暂停时段不消耗时限。"""

    def __init__(self, config: dict, timezone_name: str):
        self.config = config
        self.zone = ZoneInfo(timezone_name)

    @staticmethod
    def validate(config: object) -> dict:
        if not isinstance(config, dict):
            raise ValidationError("营业日历配置必须是对象")
        weekday_hours = config.get("weekday_hours")
        if not isinstance(weekday_hours, dict):
            raise ValidationError("营业日历需要包含 weekday_hours")
        normalized_hours: dict[str, list[list[str]]] = {}
        for key, segments in weekday_hours.items():
            if key not in {str(day) for day in range(7)}:
                raise ValidationError("星期取值必须为 0（周一）至 6（周日）")
            normalized_hours[key] = BusinessCalendar._validate_segments(segments, f"星期{key}营业时间")
        closures = config.get("closures", [])
        if not isinstance(closures, list) or any(not isinstance(day, str) for day in closures):
            raise ValidationError("闭馆日期必须是日期列表")
        normalized_closures = sorted({calendar_date(day, "闭馆日期") for day in closures})
        special = config.get("special_hours", {})
        if not isinstance(special, dict):
            raise ValidationError("特殊营业时间必须是按日期映射的对象")
        normalized_special: dict[str, list[list[str]]] = {}
        for day, segments in special.items():
            day_text = calendar_date(day, "特殊营业日期")
            normalized_special[day_text] = BusinessCalendar._validate_segments(segments, f"{day_text} 营业时间")
        unknown = set(config) - {"weekday_hours", "closures", "special_hours"}
        if unknown:
            raise ValidationError("营业日历包含未知字段", details={"fields": sorted(unknown)})
        return {"weekday_hours": normalized_hours, "closures": normalized_closures,
                "special_hours": normalized_special}

    @staticmethod
    def _validate_segments(segments: object, field: str) -> list[list[str]]:
        if not isinstance(segments, list):
            raise ValidationError(f"{field}必须是时间段列表")
        result = []
        for segment in segments:
            if not isinstance(segment, list) or len(segment) != 2 or not all(isinstance(item, str) for item in segment):
                raise ValidationError(f"{field}的每个时间段必须包含开始和结束时间")
            try:
                start = time.fromisoformat(segment[0])
                end = time.fromisoformat(segment[1])
            except ValueError as exc:
                raise ValidationError(f"{field}必须使用 HH:MM 格式") from exc
            if end <= start:
                raise ValidationError(f"{field}结束时间必须晚于开始时间")
            result.append([start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")])
        result.sort()
        for earlier, later in zip(result, result[1:]):
            if later[0] < earlier[1]:
                raise ValidationError(f"{field}时间段不能重叠")
        return result

    def segments_for(self, day: date) -> list[tuple[datetime, datetime]]:
        day_text = day.isoformat()
        if day_text in self.config["closures"]:
            raw: list[list[str]] = []
        elif day_text in self.config["special_hours"]:
            raw = self.config["special_hours"][day_text]
        else:
            raw = self.config["weekday_hours"].get(str(day.weekday()), [])
        segments = []
        for start_text, end_text in raw:
            start = datetime.combine(day, time.fromisoformat(start_text), self.zone).astimezone(UTC)
            end = datetime.combine(day, time.fromisoformat(end_text), self.zone).astimezone(UTC)
            segments.append((start, end))
        return sorted(segments)

    def add_business_minutes(self, start: datetime, minutes: float) -> datetime:
        """从 start（含）起累加营业分钟，越过闭馆区间；起点在闭馆时移到下一次开门。"""
        if minutes <= 0:
            return start.astimezone(UTC)
        current = start.astimezone(UTC)
        remaining = float(minutes)
        for _ in range(10000):
            local_day = current.astimezone(self.zone).date()
            for opened, closed in self.segments_for(local_day):
                if current < opened:
                    current = opened
                if opened <= current < closed:
                    available = (closed - current).total_seconds() / 60.0
                    if remaining <= available + 1e-9:
                        return current + timedelta(minutes=remaining)
                    remaining -= available
                    current = closed
            next_midnight = datetime.combine(local_day + timedelta(days=1), time.min, self.zone).astimezone(UTC)
            if current < next_midnight:
                current = next_midnight
        raise ValidationError("营业日历无法覆盖所需时限")

    def business_minutes_between(self, start: datetime, end: datetime) -> float:
        """区间与营业时段的交集分钟数；跨 DST 与闭馆日安全。"""
        first = start.astimezone(UTC)
        last = end.astimezone(UTC)
        if last <= first:
            return 0.0
        total = 0.0
        day = first.astimezone(self.zone).date()
        final_day = last.astimezone(self.zone).date()
        while day <= final_day:
            for opened, closed in self.segments_for(day):
                lower = max(first, opened)
                upper = min(last, closed)
                if upper > lower:
                    total += (upper - lower).total_seconds() / 60.0
            day += timedelta(days=1)
        return total


def validate_policy(config: object) -> dict:
    if not isinstance(config, dict):
        raise ValidationError("响应策略必须是对象")
    deadlines = config.get("deadlines")
    if not isinstance(deadlines, dict) or set(deadlines) != set(SEVERITY_ORDER):
        raise ValidationError("响应策略必须为每个事件等级配置时限", details={"severities": sorted(SEVERITY_ORDER)})
    normalized_deadlines = {}
    for severity, values in deadlines.items():
        if severity not in SEVERITY_ORDER or not isinstance(values, dict):
            raise ValidationError("事件等级取值无效")
        try:
            respond = int(values["respond_minutes"])
            resolve = int(values["resolve_minutes"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError(f"{severity} 时限必须包含 respond_minutes 与 resolve_minutes") from exc
        if not 1 <= respond <= 100000 or not 1 <= resolve <= 100000:
            raise ValidationError(f"{severity} 时限必须为 1 至 100000 营业分钟")
        normalized_deadlines[severity] = {"respond_minutes": respond, "resolve_minutes": resolve}
    stages = config.get("stages")
    if not isinstance(stages, list) or not 1 <= len(stages) <= 6:
        raise ValidationError("升级链必须包含 1 至 6 个阶段")
    normalized_stages = []
    previous_offset = -1
    for stage in stages:
        if not isinstance(stage, dict):
            raise ValidationError("升级阶段必须是对象")
        kind = choice(stage.get("kind"), "升级方式", STAGE_KINDS)
        target = choice(stage.get("target"), "升级对象", STAGE_TARGETS)
        try:
            offset = int(stage["offset_minutes"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("升级阶段需要 offset_minutes") from exc
        if offset < 0 or offset > 100000 or offset < previous_offset:
            raise ValidationError("升级偏移必须为非负且按阶段递增的营业分钟")
        previous_offset = offset
        normalized_stages.append({"kind": kind, "target": target, "offset_minutes": offset})
    unknown = set(config) - {"deadlines", "stages"}
    if unknown:
        raise ValidationError("响应策略包含未知字段", details={"fields": sorted(unknown)})
    return {"deadlines": normalized_deadlines, "stages": normalized_stages}


class IncidentResponseService:
    """事件响应时限配置、计时、扫描升级、交接与预演。"""

    def __init__(self, database, clock):
        self.db = database
        self.clock = clock

    def _now(self) -> str:
        return timestamp(self.clock.now())

    # -- 配置 -----------------------------------------------------------

    def _clinic_zone(self, connection, clinic_id: str) -> str:
        row = connection.execute("SELECT timezone FROM clinics WHERE id=?", (clinic_id,)).fetchone()
        if row is None:
            raise NotFound("诊所不存在")
        return row["timezone"]

    def _calendar(self, connection, clinic_id: str) -> tuple[BusinessCalendar, dict, int, str]:
        timezone_name = self._clinic_zone(connection, clinic_id)
        row = connection.execute("SELECT config_json,version FROM business_calendars WHERE clinic_id=?", (clinic_id,)).fetchone()
        if row is None:
            config = default_calendar_config()
            return BusinessCalendar(config, timezone_name), config, 0, timezone_name
        config = decode_json(row["config_json"])
        return BusinessCalendar(config, timezone_name), config, row["version"], timezone_name

    def _policy(self, connection, clinic_id: str) -> tuple[dict, int]:
        self._clinic_zone(connection, clinic_id)
        row = connection.execute("SELECT config_json,version FROM incident_policies WHERE clinic_id=?", (clinic_id,)).fetchone()
        if row is None:
            config = default_policy_config()
            return config, 0
        return decode_json(row["config_json"]), row["version"]

    def get_calendar(self, clinic_id: str, actor_id: str) -> dict:
        with self.db.transaction(write=False) as connection:
            self._authorize_queue(principal_for(connection, actor_id, clinic_id))
            _, config, version, timezone_name = self._calendar(connection, clinic_id)
            return {"clinic_id": clinic_id, "timezone": timezone_name, "config": config,
                    "version": version, "defaulted": version == 0}

    def put_calendar(self, clinic_id: str, actor_id: str, config: dict, expected_version: int) -> dict:
        config = BusinessCalendar.validate(config)
        now = self._now()
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "clinic:manage", clinic_id=clinic_id)
            row = connection.execute("SELECT version FROM business_calendars WHERE clinic_id=?", (clinic_id,)).fetchone()
            if row is None:
                if expected_version not in (0, None):
                    raise Conflict("营业日历尚未建立，期望版本必须为 0")
                connection.execute(
                    "INSERT INTO business_calendars(clinic_id,config_json,updated_by,updated_at,version) VALUES(?,?,?,?,1)",
                    (clinic_id, encode_json(config), actor_id, now))
                version = 1
            else:
                if row["version"] != expected_version:
                    raise Conflict("营业日历配置已被更新", details={"expected_version": expected_version,
                                                                  "actual_version": row["version"]})
                version = row["version"] + 1
                connection.execute("UPDATE business_calendars SET config_json=?,updated_by=?,updated_at=?,version=? WHERE clinic_id=?",
                                   (encode_json(config), actor_id, now, version, clinic_id))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="business_calendar", aggregate_id=clinic_id,
                               action="calendar.configured", occurred_at=now,
                               payload={"version": version, "weekday_count": len(config["weekday_hours"]),
                                        "closures": config["closures"]})
        return {"clinic_id": clinic_id, "config": config, "version": version}

    def get_policy(self, clinic_id: str, actor_id: str) -> dict:
        with self.db.transaction(write=False) as connection:
            self._authorize_queue(principal_for(connection, actor_id, clinic_id))
            config, version = self._policy(connection, clinic_id)
            return {"clinic_id": clinic_id, "config": config, "version": version, "defaulted": version == 0}

    def put_policy(self, clinic_id: str, actor_id: str, config: dict, expected_version: int) -> dict:
        config = validate_policy(config)
        now = self._now()
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "clinic:manage", clinic_id=clinic_id)
            row = connection.execute("SELECT version FROM incident_policies WHERE clinic_id=?", (clinic_id,)).fetchone()
            if row is None:
                if expected_version not in (0, None):
                    raise Conflict("响应策略尚未建立，期望版本必须为 0")
                connection.execute(
                    "INSERT INTO incident_policies(clinic_id,config_json,updated_by,updated_at,version) VALUES(?,?,?,?,1)",
                    (clinic_id, encode_json(config), actor_id, now))
                version = 1
            else:
                if row["version"] != expected_version:
                    raise Conflict("响应策略已被更新", details={"expected_version": expected_version,
                                                                "actual_version": row["version"]})
                version = row["version"] + 1
                connection.execute("UPDATE incident_policies SET config_json=?,updated_by=?,updated_at=?,version=? WHERE clinic_id=?",
                                   (encode_json(config), actor_id, now, version, clinic_id))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="incident_policy", aggregate_id=clinic_id,
                               action="incident_policy.configured", occurred_at=now,
                               payload={"version": version, "stages": len(config["stages"])})
        return {"clinic_id": clinic_id, "config": config, "version": version}

    # -- 值班表 ---------------------------------------------------------

    def set_duty(self, clinic_id: str, actor_id: str, staff_id: str, starts_at: str,
                 ends_at: str, note: str | None = None) -> dict:
        starts = timestamp(starts_at, "值班开始时间")
        ends = timestamp(ends_at, "值班结束时间")
        if parsed_timestamp(ends) <= parsed_timestamp(starts):
            raise ValidationError("值班结束时间必须晚于开始时间")
        note_text = text(note, "值班说明", minimum=0, maximum=600) if note is not None else None
        duty_id = new_id("dty")
        now = self._now()
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "clinic:manage", clinic_id=clinic_id)
            staff = connection.execute("SELECT role,active FROM staff WHERE id=? AND clinic_id=?", (staff_id, clinic_id)).fetchone()
            if staff is None or not staff["active"] or staff["role"] not in {"clinician", "owner"}:
                raise ValidationError("值班临床负责人必须是在岗的医生或诊所负责人")
            connection.execute(
                "INSERT INTO duty_assignments(id,clinic_id,staff_id,starts_at,ends_at,note,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)", (duty_id, clinic_id, staff_id, starts, ends, note_text, actor_id, now))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="duty_assignment", aggregate_id=duty_id, action="duty.configured",
                               occurred_at=now, payload={"staff_id": staff_id, "starts_at": starts, "ends_at": ends})
        return {"id": duty_id, "staff_id": staff_id, "starts_at": starts, "ends_at": ends, "note": note_text}

    def list_duty(self, clinic_id: str, actor_id: str) -> dict:
        with self.db.transaction(write=False) as connection:
            self._authorize_queue(principal_for(connection, actor_id, clinic_id))
            rows = connection.execute(
                "SELECT id,staff_id,starts_at,ends_at,note FROM duty_assignments WHERE clinic_id=? ORDER BY starts_at,id",
                (clinic_id,)).fetchall()
            return {"clinic_id": clinic_id, "items": [dict(row) for row in rows]}

    def _duty_clinician(self, connection, clinic_id: str, as_of: str) -> str | None:
        row = connection.execute(
            "SELECT staff_id FROM duty_assignments WHERE clinic_id=? AND starts_at<=? AND ends_at>? "
            "ORDER BY starts_at,id LIMIT 1", (clinic_id, as_of, as_of)).fetchone()
        return row["staff_id"] if row else None

    # -- 计时生命周期钩子（由 Careflow 在同一事务内调用） ---------------

    def arm(self, connection, *, clinic_id: str, incident_id: str, patient_id: str,
            severity: str, now: str, actor_id: str | None = None) -> None:
        if connection.execute("SELECT 1 FROM incident_slas WHERE incident_id=?", (incident_id,)).fetchone():
            return
        calendar, _, calendar_version, timezone_name = self._calendar(connection, clinic_id)
        policy, policy_version = self._policy(connection, clinic_id)
        budget = policy["deadlines"][severity]["respond_minutes"]
        deadline = timestamp(calendar.add_business_minutes(parsed_timestamp(now), budget))
        connection.execute(
            "INSERT INTO incident_slas(incident_id,clinic_id,timezone,phase,timer_state,armed_at,deadline_at,"
            "current_severity,max_severity,escalation_level,policy_json,policy_version,calendar_json,calendar_version,updated_at,version) "
            "VALUES(?,?,?, 'response','running', ?,?, ?,?,0, ?,?,?,?, ?,1)",
            (incident_id, clinic_id, timezone_name, now, deadline, severity, severity,
             encode_json(policy), policy_version, encode_json(calendar.config), calendar_version, now))
        self._open_segment(connection, incident_id, 1, "response", now,
                           "事件报告时启动首响计时", None)
        audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=patient_id,
                           aggregate_type="incident", aggregate_id=incident_id, action="incident.sla_armed",
                           occurred_at=now, payload={"severity": severity, "phase": "response",
                                                     "budget_business_minutes": budget, "deadline_at": deadline,
                                                     "policy_version": policy_version,
                                                     "calendar_version": calendar_version,
                                                     "basis": "事件报告时启动首响计时"})

    def after_state_change(self, connection, *, clinic_id: str, incident_id: str, previous_state: str,
                           action: str, actor_id: str | None, now: str) -> None:
        sla = connection.execute("SELECT * FROM incident_slas WHERE incident_id=?", (incident_id,)).fetchone()
        if sla is None:
            return
        incident = connection.execute("SELECT patient_id,assigned_to FROM incidents WHERE id=?", (incident_id,)).fetchone()
        patient_id = incident["patient_id"]
        calendar = self._sla_calendar(sla)
        policy = decode_json(sla["policy_json"])
        if action == "triage" and sla["phase"] == "response":
            self._acknowledge(connection, clinic_id, incident_id, patient_id, sla, calendar, policy,
                              now, actor_id, previous_state, basis="指定负责人确认（分诊）后切换为处置计时")
        elif action == "monitor":
            if sla["phase"] == "response":
                # 直接转入观察同样代表负责人接手：先确认再暂停。
                self._acknowledge(connection, clinic_id, incident_id, patient_id, sla, calendar, policy,
                                  now, actor_id, previous_state, basis="负责人接手并转入观察，首响计时结束")
                sla = self._load_sla(connection, incident_id)
            if sla["timer_state"] == "running":
                self._pause(connection, clinic_id, incident_id, patient_id, sla, calendar, now, actor_id,
                            basis="事件转入观察状态，等待患者反馈，处置计时暂停", audit_action="incident.clock_paused")
        elif action == "resolve":
            self._stop(connection, clinic_id, incident_id, patient_id, sla, now, actor_id,
                        basis="事件已解决，计时停止")
        elif action == "close" and sla["timer_state"] != "stopped":
            self._stop(connection, clinic_id, incident_id, patient_id, sla, now, actor_id,
                       basis="事件关闭，计时停止")
        elif action == "reopen":
            budget = policy["deadlines"][sla["max_severity"]]["resolve_minutes"]
            new_deadline = timestamp(calendar.add_business_minutes(parsed_timestamp(now), budget))
            if sla["timer_state"] == "running":
                self._close_segment(connection, incident_id, now, "事件重新打开前结束原计时段")
            next_sequence = self._next_sequence(connection, incident_id)
            self._open_segment(connection, incident_id, next_sequence, "handling", now,
                               "事件重新打开，处置计时恢复", actor_id)
            connection.execute(
                "UPDATE incident_slas SET phase='handling',timer_state='running',deadline_at=?,paused_at=NULL,"
                "resolved_at=NULL,updated_at=? WHERE incident_id=?", (new_deadline, now, incident_id))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=patient_id,
                               aggregate_type="incident", aggregate_id=incident_id,
                               action="incident.clock_resumed", occurred_at=now,
                               payload={"from_state": previous_state, "new_deadline_at": new_deadline,
                                        "preserved_escalation_level": sla["escalation_level"],
                                        "preserved_max_severity": sla["max_severity"],
                                        "basis": "事件重新打开恢复计时；已升级级别与最高等级保留，不退回未处理"})

    # -- 显式暂停/恢复 --------------------------------------------------

    def _acknowledge(self, connection, clinic_id, incident_id, patient_id, sla, calendar, policy,
                     now, actor_id, previous_state, *, basis: str) -> None:
        budget = policy["deadlines"][sla["max_severity"]]["resolve_minutes"]
        new_deadline = timestamp(calendar.add_business_minutes(parsed_timestamp(now), budget))
        self._close_segment(connection, incident_id, now, "负责人确认，首响计时结束")
        self._open_segment(connection, incident_id, self._next_sequence(connection, incident_id), "handling",
                           now, "负责人确认后进入临床处置阶段", actor_id)
        connection.execute(
            "UPDATE incident_slas SET phase='handling',timer_state='running',deadline_at=?,acknowledged_at=?,"
            "acknowledged_by=COALESCE(acknowledged_by,?),paused_at=NULL,updated_at=? WHERE incident_id=?",
            (new_deadline, now, actor_id, now, incident_id))
        audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=patient_id,
                           aggregate_type="incident", aggregate_id=incident_id,
                           action="incident.acknowledged", occurred_at=now,
                           payload={"from_state": previous_state, "acknowledged_by": actor_id,
                                    "response_deadline_at": sla["deadline_at"],
                                    "handling_deadline_at": new_deadline,
                                    "resolve_budget_business_minutes": budget, "basis": basis})

    def pause_clock(self, clinic_id: str, actor_id: str, incident_id: str, reason: str,
                    expected_version: int) -> dict:
        reason = text(reason, "暂停原因", maximum=600)
        now = self._now()
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "incident:manage", clinic_id=clinic_id)
            incident = self._load_incident(connection, clinic_id, incident_id)
            if incident["version"] != expected_version:
                raise Conflict("不良事件已被其他操作更新", details={"expected_version": expected_version,
                                                                   "actual_version": incident["version"]})
            if incident["state"] not in ACTIVE_STATES:
                raise Conflict("只有处置中的事件可以暂停计时")
            sla = self._load_sla(connection, incident_id)
            if sla["phase"] != "handling" or sla["timer_state"] != "running":
                raise Conflict("当前计时状态不能暂停", details={"phase": sla["phase"], "timer_state": sla["timer_state"]})
            calendar = self._sla_calendar(sla)
            self._pause(connection, clinic_id, incident_id, incident["patient_id"], sla, calendar, now, actor_id,
                        basis=reason, audit_action="incident.clock_paused")
            self._append_incident_event(connection, incident_id, "clock_paused", actor_id,
                                        f"计时暂停：{reason}", now)
            connection.execute("UPDATE incidents SET version=version+1 WHERE id=?", (incident_id,))
        return {"id": incident_id, "timer_state": "paused", "deadline_at": sla["deadline_at"],
                "paused_at": now, "version": expected_version + 1}

    def resume_clock(self, clinic_id: str, actor_id: str, incident_id: str, reason: str,
                     expected_version: int) -> dict:
        reason = text(reason, "恢复原因", maximum=600)
        now_text = self._now()
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "incident:manage", clinic_id=clinic_id)
            incident = self._load_incident(connection, clinic_id, incident_id)
            if incident["version"] != expected_version:
                raise Conflict("不良事件已被其他操作更新", details={"expected_version": expected_version,
                                                                   "actual_version": incident["version"]})
            if incident["state"] not in ACTIVE_STATES:
                raise Conflict("只有处置中的事件可以恢复计时")
            sla = self._load_sla(connection, incident_id)
            if sla["timer_state"] != "paused":
                raise Conflict("计时当前未暂停")
            calendar = self._sla_calendar(sla)
            now_dt, paused_dt = parsed_timestamp(now_text), parsed_timestamp(sla["paused_at"])
            shifted = calendar.business_minutes_between(paused_dt, now_dt)
            new_deadline = timestamp(calendar.add_business_minutes(parsed_timestamp(sla["deadline_at"]), shifted))
            self._open_segment(connection, incident_id, self._next_sequence(connection, incident_id), sla["phase"],
                               now_text, reason, actor_id)
            connection.execute(
                "UPDATE incident_slas SET timer_state='running',deadline_at=?,paused_at=NULL,updated_at=?,version=version+1 "
                "WHERE incident_id=?", (new_deadline, now_text, incident_id))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=incident["patient_id"],
                               aggregate_type="incident", aggregate_id=incident_id, action="incident.clock_resumed",
                               occurred_at=now_text,
                               payload={"phase": sla["phase"], "paused_at": sla["paused_at"], "resumed_at": now_text,
                                        "previous_deadline_at": sla["deadline_at"], "new_deadline_at": new_deadline,
                                        "shifted_business_minutes": round(shifted, 2),
                                        "basis": f"恢复计时：{reason}"})
            self._append_incident_event(connection, incident_id, "clock_resumed", actor_id,
                                        f"计时恢复：{reason}", now_text)
            connection.execute("UPDATE incidents SET version=version+1 WHERE id=?", (incident_id,))
        return {"id": incident_id, "timer_state": "running", "deadline_at": new_deadline,
                "shifted_business_minutes": round(shifted, 2), "version": expected_version + 1}

    def _pause(self, connection, clinic_id, incident_id, patient_id, sla, calendar, now, actor_id,
               *, basis: str, audit_action: str) -> None:
        self._close_segment(connection, incident_id, now, basis)
        connection.execute(
            "UPDATE incident_slas SET timer_state='paused',paused_at=?,updated_at=? WHERE incident_id=?",
            (now, now, incident_id))
        audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=patient_id,
                           aggregate_type="incident", aggregate_id=incident_id, action=audit_action,
                           occurred_at=now, payload={"phase": sla["phase"], "paused_at": now,
                                                     "deadline_at": sla["deadline_at"], "basis": basis})

    def _stop(self, connection, clinic_id, incident_id, patient_id, sla, now, actor_id, *, basis: str) -> None:
        if sla["timer_state"] == "running":
            self._close_segment(connection, incident_id, now, basis)
        connection.execute(
            "UPDATE incident_slas SET phase='done',timer_state='stopped',resolved_at=?,paused_at=NULL,updated_at=? "
            "WHERE incident_id=?", (now, now, incident_id))
        audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=patient_id,
                           aggregate_type="incident", aggregate_id=incident_id, action="incident.clock_stopped",
                           occurred_at=now, payload={"phase": sla["phase"], "stopped_at": now, "basis": basis})

    # -- 转派 -----------------------------------------------------------

    def transfer(self, clinic_id: str, actor_id: str, incident_id: str, to_staff_id: str,
                 note: str, expected_version: int) -> dict:
        note = text(note, "转派说明", maximum=1000)
        now_text = self._now()
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "incident:manage", clinic_id=clinic_id)
            incident = self._load_incident(connection, clinic_id, incident_id)
            if incident["version"] != expected_version:
                raise Conflict("不良事件已被其他操作更新", details={"expected_version": expected_version,
                                                                   "actual_version": incident["version"]})
            if incident["state"] not in ACTIVE_STATES:
                raise Conflict("已结束事件不能转派")
            if to_staff_id == incident["assigned_to"]:
                raise ValidationError("事件已由该负责人承担")
            target = connection.execute("SELECT active,role FROM staff WHERE id=? AND clinic_id=?",
                                        (to_staff_id, clinic_id)).fetchone()
            if target is None or not target["active"] or target["role"] not in _CLINICAL_ROLES:
                raise ValidationError("接手人必须是在岗的临床岗位")
            sla = self._load_sla(connection, incident_id)
            calendar = self._sla_calendar(sla)
            reference_dt = parsed_timestamp(sla["paused_at"] if sla["timer_state"] == "paused" else now_text)
            raw_remaining = calendar.business_minutes_between(reference_dt, parsed_timestamp(sla["deadline_at"]))
            remaining = round(max(0.0, raw_remaining), 2)
            overdue = round(max(0.0, -raw_remaining), 2)
            events = [{"sequence": row["sequence"], "type": row["event_type"], "actor_id": row["actor_id"],
                       "note": row["note"], "created_at": row["created_at"]}
                      for row in connection.execute("SELECT * FROM incident_events WHERE incident_id=? ORDER BY sequence",
                                                    (incident_id,)).fetchall()]
            escalations = [dict(row) for row in connection.execute(
                "SELECT phase,stage_index,stage_kind,stage_target,target_staff_id,outcome,fired_at,due_at "
                "FROM incident_escalations WHERE incident_id=? ORDER BY phase,stage_index", (incident_id,)).fetchall()]
            handover = {
                "report": {"id": incident_id, "patient_id": incident["patient_id"], "severity": incident["severity"],
                           "max_severity": sla["max_severity"], "category": incident["category"],
                           "onset_at": incident["onset_at"], "reported_at": incident["reported_at"],
                           "reported_by": incident["reported_by"], "summary": incident["summary"]},
                "actions_taken": events,
                "timer": {"phase": sla["phase"], "timer_state": sla["timer_state"],
                          "deadline_at": sla["deadline_at"], "paused_at": sla["paused_at"],
                          "acknowledged_at": sla["acknowledged_at"], "escalation_level": sla["escalation_level"],
                          "stages": escalations},
            }
            transfer_id = new_id("trf")
            connection.execute(
                "INSERT INTO incident_transfers(id,clinic_id,incident_id,from_assignee,to_assignee,deadline_at,"
                "remaining_business_minutes,overdue_business_minutes,handover_json,actor_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (transfer_id, clinic_id, incident_id, incident["assigned_to"], to_staff_id, sla["deadline_at"],
                 remaining, overdue, encode_json(handover), actor_id, now_text))
            connection.execute("UPDATE incidents SET assigned_to=?,version=version+1 WHERE id=?",
                               (to_staff_id, incident_id))
            self._append_incident_event(connection, incident_id, "transfer", actor_id,
                                        f"转派给 {to_staff_id}：{note}", now_text)
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=incident["patient_id"],
                               aggregate_type="incident", aggregate_id=incident_id, action="incident.transferred",
                               occurred_at=now_text,
                               payload={"from_assignee": incident["assigned_to"], "to_assignee": to_staff_id,
                                        "deadline_at": sla["deadline_at"], "phase": sla["phase"],
                                        "timer_state": sla["timer_state"],
                                        "remaining_business_minutes": remaining,
                                        "overdue_business_minutes": overdue,
                                        "actions_handed_over": len(events), "note": note,
                                        "basis": "转派交接原始报告、已采取措施与剩余时限"})
            return {"id": transfer_id, "incident_id": incident_id, "from_assignee": incident["assigned_to"],
                    "assigned_to": to_staff_id, "deadline_at": sla["deadline_at"], "phase": sla["phase"],
                    "timer_state": sla["timer_state"], "remaining_business_minutes": remaining,
                    "overdue_business_minutes": overdue, "version": incident["version"] + 1,
                    "handover": handover}

    # -- 等级修正 -------------------------------------------------------

    def reclassify(self, clinic_id: str, actor_id: str, incident_id: str, severity: str,
                   note: str, expected_version: int) -> dict:
        severity = choice(severity, "严重程度", set(SEVERITY_ORDER))
        note = text(note, "等级修正说明", maximum=1000)
        now_text = self._now()
        with self.db.transaction() as connection:
            principal = principal_for(connection, actor_id, clinic_id)
            authorize(principal, "incident:manage", clinic_id=clinic_id)
            incident = self._load_incident(connection, clinic_id, incident_id)
            if incident["version"] != expected_version:
                raise Conflict("不良事件已被其他操作更新", details={"expected_version": expected_version,
                                                                   "actual_version": incident["version"]})
            if incident["state"] not in ACTIVE_STATES:
                raise Conflict("已结束事件不能修正等级；如需处理请先重新打开")
            sla = self._load_sla(connection, incident_id)
            previous_severity = incident["severity"]
            previous_max = sla["max_severity"]
            new_max = previous_max if SEVERITY_ORDER[previous_max] >= SEVERITY_ORDER[severity] else severity
            policy = decode_json(sla["policy_json"])
            calendar = self._sla_calendar(sla)
            phase_budget = policy["deadlines"][new_max][
                "respond_minutes" if sla["phase"] == "response" else "resolve_minutes"]
            deadline_changed = False
            new_deadline = sla["deadline_at"]
            if SEVERITY_ORDER[severity] > SEVERITY_ORDER[previous_max]:
                # 等级升高：从确认恶化的时刻起，按新等级给予当前阶段的完整营业分钟预算。
                reference = parsed_timestamp(sla["paused_at"] if sla["timer_state"] == "paused" else now_text)
                candidate = timestamp(calendar.add_business_minutes(reference, phase_budget))
                if candidate < new_deadline:
                    new_deadline = candidate
                    deadline_changed = True
            connection.execute("UPDATE incidents SET severity=?,version=version+1 WHERE id=?", (severity, incident_id))
            connection.execute(
                "UPDATE incident_slas SET current_severity=?,max_severity=?,deadline_at=?,updated_at=? WHERE incident_id=?",
                (severity, new_max, new_deadline, now_text, incident_id))
            self._append_incident_event(connection, incident_id, "reclassify", actor_id,
                                        f"等级修正为 {severity}：{note}", now_text)
            downgrade = SEVERITY_ORDER[severity] < SEVERITY_ORDER[previous_max]
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=incident["patient_id"],
                               aggregate_type="incident", aggregate_id=incident_id,
                               action="incident.severity_reclassified", occurred_at=now_text,
                               payload={"from_severity": previous_severity, "to_severity": severity,
                                        "previous_max_severity": previous_max, "max_severity": new_max,
                                        "deadline_changed": deadline_changed, "new_deadline_at": new_deadline,
                                        "escalation_level_preserved": sla["escalation_level"],
                                        "downgrade": downgrade,
                                        "basis": ("降级仅更新记录等级；已升级级别、最高等级与截止时间保持不变，不退回未处理"
                                                  if downgrade else "按更高等级重新计算当前阶段截止时间")})
            return {"id": incident_id, "severity": severity, "max_severity": new_max,
                    "deadline_at": new_deadline, "deadline_changed": deadline_changed,
                    "escalation_level": sla["escalation_level"], "version": incident["version"] + 1}

    # -- 工作队列、扫描与预演 ------------------------------------------

    def worklist(self, clinic_id: str, actor_id: str, *, as_of: str | None = None, limit: int = 200) -> dict:
        if not 1 <= limit <= 1000:
            raise ValidationError("查询数量须为 1 至 1000")
        as_of_text = timestamp(as_of, "查询时刻") if as_of else self._now()
        with self.db.transaction(write=False) as connection:
            self._authorize_queue(principal_for(connection, actor_id, clinic_id))
            items = self._collect(connection, clinic_id, parsed_timestamp(as_of_text), limit)
            return {"clinic_id": clinic_id, "as_of": as_of_text, "returned": len(items), "items": items}

    def scan(self, clinic_id: str, actor_id: str | None = None, *, as_of: str | None = None,
             limit: int = 200) -> dict:
        """执行一次超时扫描并持久化提醒/升级；重复扫描同一阶段不会产生新动作。"""
        if not 1 <= limit <= 1000:
            raise ValidationError("扫描数量须为 1 至 1000")
        as_of_text = timestamp(as_of, "扫描时刻") if as_of else self._now()
        batch_id = new_id("scan")
        fired: list[dict] = []
        deferred: list[dict] = []
        with self.db.transaction() as connection:
            if actor_id is not None:
                authorize(principal_for(connection, actor_id, clinic_id), "incident:manage", clinic_id=clinic_id)
            elif connection.execute("SELECT 1 FROM clinics WHERE id=?", (clinic_id,)).fetchone() is None:
                raise NotFound("诊所不存在")
            as_of_dt = parsed_timestamp(as_of_text)
            rows = self._active_rows(connection, clinic_id, limit)
            for row in rows:
                sla = self._sla_dict(row)
                if sla["timer_state"] != "running":
                    continue
                calendar = BusinessCalendar(decode_json(sla["calendar_json"]), sla["timezone"])
                existing = {(item["phase"], item["stage_index"]): item
                            for item in connection.execute(
                                "SELECT * FROM incident_escalations WHERE incident_id=?", (row["id"],)).fetchall()}
                for candidate in self._due_stages(connection, row, sla, calendar, as_of_text, as_of_dt):
                    key = (candidate["phase"], candidate["stage_index"])
                    prior = existing.get(key)
                    if prior and prior["outcome"] == "fired":
                        continue
                    if candidate["target_staff_id"] is None:
                        if prior is None:
                            connection.execute(
                                "INSERT INTO incident_escalations(id,clinic_id,incident_id,phase,stage_index,stage_kind,"
                                "stage_target,deadline_at,offset_minutes,due_at,outcome,scan_batch,basis,created_at) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?, 'deferred', ?,?,?)",
                                (new_id("esc"), clinic_id, row["id"], candidate["phase"], candidate["stage_index"],
                                 candidate["kind"], candidate["target"], sla["deadline_at"],
                                 candidate["offset_minutes"], candidate["due_at"], batch_id,
                                 candidate["basis"], as_of_text))
                        deferred.append({"incident_id": row["id"], "phase": candidate["phase"],
                                         "stage_index": candidate["stage_index"], "kind": candidate["kind"],
                                         "target": candidate["target"], "reason": "no_duty_clinician"})
                        continue
                    if prior is None:
                        connection.execute(
                            "INSERT INTO incident_escalations(id,clinic_id,incident_id,phase,stage_index,stage_kind,"
                            "stage_target,deadline_at,offset_minutes,due_at,fired_at,target_staff_id,outcome,scan_batch,basis,created_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'fired',?,?,?)",
                            (new_id("esc"), clinic_id, row["id"], candidate["phase"], candidate["stage_index"],
                             candidate["kind"], candidate["target"], sla["deadline_at"],
                             candidate["offset_minutes"], candidate["due_at"], as_of_text,
                             candidate["target_staff_id"], batch_id, candidate["basis"], as_of_text))
                    else:
                        connection.execute(
                            "UPDATE incident_escalations SET outcome='fired',fired_at=?,target_staff_id=?,scan_batch=?,"
                            "basis=? WHERE incident_id=? AND phase=? AND stage_index=?",
                            (as_of_text, candidate["target_staff_id"], batch_id, candidate["basis"],
                             row["id"], candidate["phase"], candidate["stage_index"]))
                    level = candidate["stage_index"] + 1
                    connection.execute(
                        "UPDATE incident_slas SET escalation_level=MAX(escalation_level,?),updated_at=? WHERE incident_id=?",
                        (level, as_of_text, row["id"]))
                    if candidate["kind"] == "escalate":
                        connection.execute("UPDATE incidents SET assigned_to=? WHERE id=?",
                                           (candidate["target_staff_id"], row["id"]))
                    connection.execute("UPDATE incidents SET version=version+1 WHERE id=?", (row["id"],))
                    event_type = "escalation_reminder" if candidate["kind"] == "remind" else "escalation_notice"
                    self._append_incident_event(connection, row["id"], event_type, None, candidate["basis"], as_of_text)
                    audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=row["patient_id"],
                                       aggregate_type="incident", aggregate_id=row["id"],
                                       action="incident.reminder_fired" if candidate["kind"] == "remind"
                                       else "incident.escalation_fired",
                                       occurred_at=as_of_text,
                                       payload={"phase": candidate["phase"], "stage_index": candidate["stage_index"],
                                                "target": candidate["target"], "target_staff_id": candidate["target_staff_id"],
                                                "deadline_at": sla["deadline_at"], "due_at": candidate["due_at"],
                                                "fired_at": as_of_text, "scan_batch": batch_id,
                                                "overdue_business_minutes": candidate["overdue_minutes"],
                                                "escalation_level": level, "basis": candidate["basis"]})
                    fired.append({"incident_id": row["id"], "patient_id": row["patient_id"],
                                  "phase": candidate["phase"], "stage_index": candidate["stage_index"],
                                  "kind": candidate["kind"], "target": candidate["target"],
                                  "target_staff_id": candidate["target_staff_id"], "fired_at": as_of_text,
                                  "deadline_at": sla["deadline_at"], "due_at": candidate["due_at"],
                                  "escalation_level": level})
        return {"clinic_id": clinic_id, "as_of": as_of_text, "scan_batch": batch_id,
                "scanned": len(rows), "fired": fired, "deferred": deferred}

    def rehearse(self, clinic_id: str, actor_id: str, as_of: str, *, limit: int = 200) -> dict:
        """按指定时刻只读预演：返回将触发的提醒/升级，不写任何状态。"""
        if not 1 <= limit <= 1000:
            raise ValidationError("预演数量须为 1 至 1000")
        as_of_text = timestamp(as_of, "预演时刻")
        with self.db.transaction(write=False) as connection:
            self._authorize_queue(principal_for(connection, actor_id, clinic_id))
            as_of_dt = parsed_timestamp(as_of_text)
            rows = self._active_rows(connection, clinic_id, limit)
            items = []
            for row in rows:
                sla = self._sla_dict(row)
                calendar = BusinessCalendar(decode_json(sla["calendar_json"]), sla["timezone"])
                projected = []
                if sla["timer_state"] == "running":
                    projected = [{
                        "phase": candidate["phase"], "stage_index": candidate["stage_index"],
                        "kind": candidate["kind"], "target": candidate["target"],
                        "target_staff_id": candidate["target_staff_id"], "due_at": candidate["due_at"],
                        "would_fire": candidate["target_staff_id"] is not None,
                        "reason": None if candidate["target_staff_id"] is not None else "no_duty_clinician",
                    } for candidate in self._due_stages(connection, row, sla, calendar, as_of_text, as_of_dt)]
                metrics = self._item_metrics(connection, sla, calendar, as_of_dt)
                items.append({
                    "incident_id": row["id"], "patient_id": row["patient_id"], "severity": row["severity"],
                    "max_severity": sla["max_severity"], "state": row["state"], "assigned_to": row["assigned_to"],
                    "phase": sla["phase"], "timer_state": sla["timer_state"], "deadline_at": sla["deadline_at"],
                    "escalation_level": sla["escalation_level"], "projected_actions": projected,
                    **metrics,
                })
            return {"clinic_id": clinic_id, "as_of": as_of_text, "mode": "rehearsal", "writes": False,
                    "returned": len(items), "items": items}

    def timer_history(self, clinic_id: str, actor_id: str, incident_id: str) -> dict:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "incident:manage", clinic_id=clinic_id)
            incident = self._load_incident(connection, clinic_id, incident_id)
            sla = self._load_sla(connection, incident_id)
            segments = [{"sequence": row["sequence"], "phase": row["phase"], "started_at": row["started_at"],
                         "ended_at": row["ended_at"], "basis": row["basis"], "actor_id": row["actor_id"]}
                        for row in connection.execute(
                            "SELECT * FROM incident_timer_segments WHERE incident_id=? ORDER BY sequence",
                            (incident_id,)).fetchall()]
            escalations = [dict(row) for row in connection.execute(
                "SELECT phase,stage_index,stage_kind,stage_target,target_staff_id,outcome,due_at,fired_at,scan_batch,basis "
                "FROM incident_escalations WHERE incident_id=? ORDER BY phase,stage_index", (incident_id,)).fetchall()]
            transfers = [{"id": row["id"], "from_assignee": row["from_assignee"], "to_assignee": row["to_assignee"],
                          "deadline_at": row["deadline_at"], "remaining_business_minutes": row["remaining_business_minutes"],
                          "overdue_business_minutes": row["overdue_business_minutes"], "actor_id": row["actor_id"],
                          "created_at": row["created_at"]}
                         for row in connection.execute(
                             "SELECT * FROM incident_transfers WHERE incident_id=? ORDER BY created_at",
                             (incident_id,)).fetchall()]
            return {"incident_id": incident_id, "patient_id": incident["patient_id"],
                    "sla": {"phase": sla["phase"], "timer_state": sla["timer_state"], "armed_at": sla["armed_at"],
                            "deadline_at": sla["deadline_at"], "acknowledged_at": sla["acknowledged_at"],
                            "acknowledged_by": sla["acknowledged_by"], "paused_at": sla["paused_at"],
                            "resolved_at": sla["resolved_at"], "current_severity": sla["current_severity"],
                            "max_severity": sla["max_severity"], "escalation_level": sla["escalation_level"],
                            "policy_version": sla["policy_version"], "calendar_version": sla["calendar_version"]},
                    "segments": segments, "escalations": escalations, "transfers": transfers}

    # -- 内部辅助 -------------------------------------------------------

    def _due_stages(self, connection, row, sla, calendar: BusinessCalendar, as_of_text: str, as_of_dt: datetime):
        policy = decode_json(sla["policy_json"])
        deadline_dt = parsed_timestamp(sla["deadline_at"])
        existing = {(item["phase"], item["stage_index"]): item
                    for item in connection.execute(
                        "SELECT phase,stage_index,outcome FROM incident_escalations WHERE incident_id=?",
                        (row["id"],)).fetchall()}
        candidates = []
        for index, stage in enumerate(policy["stages"]):
            key = (sla["phase"], index)
            if key in existing and existing[key]["outcome"] == "fired":
                continue
            due_dt = calendar.add_business_minutes(deadline_dt, stage["offset_minutes"])
            if as_of_dt < due_dt:
                break
            if stage["target"] == "assignee":
                target = row["assigned_to"] or row["reported_by"]
            else:
                target = self._duty_clinician(connection, sla["clinic_id"], as_of_text)
            overdue = round(calendar.business_minutes_between(deadline_dt, as_of_dt), 2)
            label = "提醒当前负责人" if stage["kind"] == "remind" else "升级到值班临床负责人"
            candidates.append({
                "phase": sla["phase"], "stage_index": index, "kind": stage["kind"],
                "target": stage["target"], "target_staff_id": target,
                "offset_minutes": stage["offset_minutes"], "due_at": timestamp(due_dt),
                "overdue_minutes": overdue,
                "basis": f"超过{stage['offset_minutes']}营业分钟处置时限仍未完成当前阶段，{label}",
            })
        return candidates

    def _collect(self, connection, clinic_id: str, as_of_dt: datetime, limit: int) -> list[dict]:
        items = []
        for row in self._active_rows(connection, clinic_id, limit):
            sla = self._sla_dict(row)
            calendar = BusinessCalendar(decode_json(sla["calendar_json"]), sla["timezone"])
            metrics = self._item_metrics(connection, sla, calendar, as_of_dt)
            items.append({
                "incident_id": row["id"], "patient_id": row["patient_id"], "severity": row["severity"],
                "max_severity": sla["max_severity"], "state": row["state"], "assigned_to": row["assigned_to"],
                "reported_by": row["reported_by"], "phase": sla["phase"], "timer_state": sla["timer_state"],
                "armed_at": sla["armed_at"], "acknowledged_at": sla["acknowledged_at"],
                "deadline_at": sla["deadline_at"], "escalation_level": sla["escalation_level"], **metrics,
            })
        items.sort(key=lambda item: (-item["escalation_level"], item["deadline_at"], item["incident_id"]))
        return items

    def _item_metrics(self, connection, sla, calendar: BusinessCalendar, as_of_dt: datetime) -> dict:
        elapsed, remaining, overdue = self._phase_metrics(connection, sla, calendar, as_of_dt)
        return {"elapsed_business_minutes": round(elapsed, 2),
                "remaining_business_minutes": round(remaining, 2),
                "overdue_business_minutes": round(overdue, 2)}

    def _phase_metrics(self, connection, sla, calendar: BusinessCalendar, as_of_dt: datetime) -> tuple[float, float, float]:
        reference = as_of_dt if sla["timer_state"] == "running" else parsed_timestamp(sla["paused_at"])
        elapsed = 0.0
        rows = connection.execute(
            "SELECT started_at,ended_at FROM incident_timer_segments WHERE incident_id=? AND phase=? ORDER BY sequence",
            (sla["incident_id"], sla["phase"])).fetchall()
        for row in rows:
            start = parsed_timestamp(row["started_at"])
            end_raw = parsed_timestamp(row["ended_at"]) if row["ended_at"] else reference
            end = min(end_raw, as_of_dt)
            if end > start:
                elapsed += calendar.business_minutes_between(start, end)
        raw_remaining = calendar.business_minutes_between(reference, parsed_timestamp(sla["deadline_at"]))
        return elapsed, max(0.0, raw_remaining), max(0.0, -raw_remaining)

    def _active_rows(self, connection, clinic_id: str, limit: int):
        return connection.execute(
            "SELECT i.id,i.patient_id,i.severity,i.state,i.assigned_to,i.reported_by,i.category,i.onset_at,"
            "i.reported_at,i.summary,s.phase AS sla_phase,s.timer_state AS sla_timer_state,s.armed_at AS sla_armed_at,"
            "s.acknowledged_at AS sla_acknowledged_at,s.paused_at AS sla_paused_at,s.deadline_at AS sla_deadline,"
            "s.current_severity AS sla_current_severity,s.max_severity AS sla_max_severity,"
            "s.escalation_level AS sla_escalation_level,s.policy_json AS sla_policy_json,"
            "s.calendar_json AS sla_calendar_json,s.timezone AS sla_timezone,s.clinic_id AS sla_clinic_id "
            "FROM incidents i JOIN incident_slas s ON s.incident_id=i.id JOIN patients p ON p.id=i.patient_id "
            "WHERE p.clinic_id=? AND i.state NOT IN ('resolved','closed') ORDER BY s.deadline_at,i.id LIMIT ?",
            (clinic_id, limit)).fetchall()

    @staticmethod
    def _sla_dict(row) -> dict:
        return {"incident_id": row["id"], "clinic_id": row["sla_clinic_id"], "phase": row["sla_phase"],
                "timer_state": row["sla_timer_state"], "armed_at": row["sla_armed_at"],
                "acknowledged_at": row["sla_acknowledged_at"], "paused_at": row["sla_paused_at"],
                "deadline_at": row["sla_deadline"], "current_severity": row["sla_current_severity"],
                "max_severity": row["sla_max_severity"], "escalation_level": row["sla_escalation_level"],
                "policy_json": row["sla_policy_json"], "calendar_json": row["sla_calendar_json"],
                "timezone": row["sla_timezone"]}

    @staticmethod
    def _sla_calendar(sla) -> BusinessCalendar:
        return BusinessCalendar(decode_json(sla["calendar_json"]), sla["timezone"])

    def _load_incident(self, connection, clinic_id: str, incident_id: str):
        row = connection.execute(
            "SELECT i.* FROM incidents i JOIN patients p ON p.id=i.patient_id WHERE i.id=? AND p.clinic_id=?",
            (incident_id, clinic_id)).fetchone()
        if row is None:
            raise NotFound("不良事件不存在")
        return row

    def _load_sla(self, connection, incident_id: str):
        row = connection.execute("SELECT * FROM incident_slas WHERE incident_id=?", (incident_id,)).fetchone()
        if row is None:
            raise Conflict("该事件没有响应计时记录")
        return row

    @staticmethod
    def _authorize_queue(principal) -> None:
        permissions = ROLE_PERMISSIONS.get(principal.role, set())
        if "incident:manage" not in permissions and "incident:oversee" not in permissions:
            raise Forbidden("当前岗位无权查看事件处置队列")

    @staticmethod
    def _next_sequence(connection, incident_id: str) -> int:
        return connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM incident_timer_segments WHERE incident_id=?",
            (incident_id,)).fetchone()[0]

    def _open_segment(self, connection, incident_id: str, sequence: int, phase: str, now: str,
                      basis: str, actor_id: str | None) -> None:
        connection.execute(
            "INSERT INTO incident_timer_segments(id,incident_id,sequence,phase,started_at,ended_at,basis,actor_id) "
            "VALUES(?,?,?,?,?,NULL,?,?)", (new_id("seg"), incident_id, sequence, phase, now, basis, actor_id))

    @staticmethod
    def _close_segment(connection, incident_id: str, now: str, basis: str) -> None:
        connection.execute(
            "UPDATE incident_timer_segments SET ended_at=?,basis=? WHERE incident_id=? AND ended_at IS NULL",
            (now, basis, incident_id))

    @staticmethod
    def _append_incident_event(connection, incident_id: str, event_type: str, actor_id: str | None,
                               note: str, now: str) -> None:
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM incident_events WHERE incident_id=?", (incident_id,)).fetchone()[0]
        connection.execute(
            "INSERT INTO incident_events(id,incident_id,event_type,actor_id,note,created_at,sequence) VALUES(?,?,?,?,?,?,?)",
            (new_id("iev"), incident_id, event_type, actor_id, note, now, sequence))
