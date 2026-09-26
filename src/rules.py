"""港口泊位与航道调度领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "draft"
RISK_LEVELS = ["low", "medium", "high"]
CREATE_ROLES = {'port_controller'}
ACTION_ROLES = {
    'confirm': {'port_controller'},
    'close': {'port_controller'},
    'reopen': {'port_controller'},
    'berth': {'port_controller'},
    'depart': {'port_controller'},
    'cancel': {'port_controller'},
}
TRANSITIONS = {
    'confirm': {'draft': 'confirmed'},
    'close': {'draft': 'waiting', 'confirmed': 'waiting'},
    'berth': {'confirmed': 'berthed', 'waiting': 'berthed'},
    'depart': {'berthed': 'departed'},
    'cancel': {'draft': 'cancelled', 'confirmed': 'cancelled', 'waiting': 'cancelled'},
}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        vessel = text(p, "vessel")
        captain = text(p, "captain")
        berth = text(p, "berth")
        vessel_length = number(p, "vessel_length_m", 1)
        berth_length = number(p, "berth_length_m", 1)
        draft = number(p, "draft_m", 0)
        berth_depth = number(p, "berth_depth_m", 0)
        eta = integer(p, "eta_hour", 0, 23)
        choice(p, "risk_level", RISK_LEVELS)
        dangerous = boolean(p, "dangerous_goods")
        if dangerous:
            text(p, "dangerous_class")
        else:
            optional_text(p, "dangerous_class", "")
        duration = p.get("operation_duration_hours")
        etd = p.get("etd_hour")
        if duration is None and etd is None:
            raise ValidationError("operation_duration_hours和etd_hour至少提供一个")
        if duration is not None:
            duration = number(p, "operation_duration_hours", 0.1, 48)
        if etd is not None:
            etd = integer(p, "etd_hour", 1, 240)
            if etd <= eta:
                raise ValidationError("etd_hour必须晚于eta_hour")
        if duration is None:
            duration = round(etd - eta, 2)
        if etd is None:
            etd = eta + int(duration)
        p["operation_duration_hours"] = duration
        p["etd_hour"] = etd
        if berth_length < vessel_length:
            raise ValidationError("泊位长度不足")
        if berth_depth - draft < 0.5:
            raise ValidationError("剩余水深不足")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["safety_margin_m"] = round(float(p["berth_depth_m"]) - float(p["draft_m"]), 2)
        p["window_hours"] = int(round(float(p["operation_duration_hours"])))
        p["quay_ok"] = bool(p["berth_length_m"] >= p["vessel_length_m"] and p["safety_margin_m"] >= 0.5)
        p["operation_end_hour"] = round(float(p["eta_hour"]) + float(p["operation_duration_hours"]), 2)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        eta = int(payload["eta_hour"])
        end = float(payload["operation_end_hour"])
        for item in existing:
            other = item["payload"]
            if item["state"] in {"cancelled", "departed"} or other.get("berth") != payload.get("berth"):
                continue
            other_start = int(other.get("eta_hour", 0))
            other_end = float(other.get("operation_end_hour", other.get("etd_hour", 0)))
            if eta < other_end and end > other_start:
                raise Conflict("同一泊位时间窗冲突")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def validate_reopen_params(self, params: Dict[str, Any], close_hour: float) -> Dict[str, Any]:
        p = dict(params or {})
        reopen_hour = number(p, "reopen_hour", close_hour)
        transit_hours = number(p, "channel_transit_hours", 0.1, 6)
        channel_depth = number(p, "channel_depth_m", 0.1)
        roster = p.get("pilots", [])
        if roster is None:
            roster = []
        if not isinstance(roster, list):
            raise ValidationError("pilots必须是列表")
        pilots = []
        for item in roster:
            if not isinstance(item, dict):
                raise ValidationError("pilots项必须是对象")
            pilot_id = text(item, "pilot_id")
            available_from = number(item, "available_from", 0)
            pilots.append({"pilot_id": pilot_id, "available_from": available_from})
        return {
            "reopen_hour": reopen_hour,
            "channel_transit_hours": transit_hours,
            "channel_depth_m": channel_depth,
            "pilots": pilots,
        }

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "confirm":
            pilot = text(data, "pilot_id")
            changes["pilot_id"] = pilot
            summary = "已确认引航员"
        elif action == "close":
            close_hour = number(data, "close_hour", 0)
            reason = optional_text(data, "reason", "航道封航")
            changes["waiting"] = {
                "reason": "channel_closed",
                "reason_text": reason,
                "since_hour": close_hour,
            }
            summary = "航道封航，转入候泊：%s" % reason
        elif action == "berth":
            actual = number(data, "actual_draft_m", 0)
            if float(p["berth_depth_m"]) - actual < 0.5:
                raise ValidationError("实际吃水导致水深不足")
            changes["actual_draft_m"] = actual
            summary = "船舶已靠泊"
        elif action == "depart":
            if not boolean(data, "cargo_operation_complete"):
                raise ValidationError("货物作业尚未完成")
            summary = "船舶已离泊"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            summary = "计划已取消"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
