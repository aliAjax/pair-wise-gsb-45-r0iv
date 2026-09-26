"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, integer, number, text, text_list
from .repository import Repository
from .reschedule import build_schedule, order_waiting
from .rules import SCHEDULED_STATE, UNSTARTED_STATES, WAITING_STATE, DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _port_closed(self) -> bool:
        event = self.repository.latest_port_event()
        return bool(event and event["event_type"] == "closed")

    def _sync_waiting(self) -> None:
        """按危险品优先、同风险先到先服务重排候泊顺序并落库。"""
        ordered = order_waiting(self.repository.list_by_states([WAITING_STATE]))
        entries = []
        for index, record in enumerate(ordered):
            reason = record["payload"].get("wait_reason") or "候泊中"
            entries.append({"record_id": record["id"], "position": index + 1, "reason": reason})
        self.repository.replace_waiting(entries)

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        record = self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)
        if self._port_closed():
            new_payload = dict(record["payload"])
            new_payload["wait_reason"] = "封航期间新计划，直接转入候泊"
            record = self.repository.mutate(
                record_id=record["id"],
                expected_version=record["version"],
                state=WAITING_STATE,
                payload=new_payload,
                actor_id=actor.user_id,
                action="port_close",
                details={"summary": "封航期间创建，转入候泊", "from": record["state"], "to": WAITING_STATE},
            )
            self._sync_waiting()
        return record

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        result = self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )
        if action == "berth" and not self.repository.occupancy_for_record(record_id):
            payload = result["payload"]
            self.repository.add_berth_occupancy(
                payload["berth"], record_id, payload.get("vessel", ""), int(payload["eta_hour"]), int(payload["etd_hour"]), "berth"
            )
        elif action in {"depart", "cancel"}:
            self.repository.delete_channel_slots([record_id])
            self.repository.delete_berth_occupancy([record_id])
            self._sync_waiting()
        return result

    def close_port(self, actor: Actor, reason: str, closed_at_hour: Any) -> Dict[str, Any]:
        """封航：未开始的计划转入候泊并写明原因，已靠泊的继续作业。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "port_close"):
            raise PermissionDenied("角色无权执行封航")
        reason = text({"reason": reason}, "reason")
        hour = integer({"closed_at_hour": closed_at_hour}, "closed_at_hour", 0, 96)
        if self._port_closed():
            raise Conflict("港口已处于封航状态")
        self.repository.add_port_event("closed", reason, actor.user_id)
        moved: List[int] = []
        for record in self.repository.list_by_states(sorted(UNSTARTED_STATES)):
            payload = dict(record["payload"])
            payload["wait_reason"] = "封航候泊：%s" % reason
            payload["wait_since_hour"] = hour
            payload.pop("channel_slot", None)
            payload.pop("berth_window", None)
            self.repository.mutate(
                record_id=record["id"],
                expected_version=record["version"],
                state=WAITING_STATE,
                payload=payload,
                actor_id=actor.user_id,
                action="port_close",
                details={"summary": "封航转入候泊", "reason": reason, "from": record["state"], "to": WAITING_STATE},
            )
            self.repository.delete_channel_slots([record["id"]])
            self.repository.delete_berth_occupancy([record["id"]], source="schedule")
            moved.append(record["id"])
        continued: List[int] = []
        for record in self.repository.list_by_states(["berthed"]):
            if not self.repository.occupancy_for_record(record["id"]):
                payload = record["payload"]
                self.repository.add_berth_occupancy(
                    payload["berth"], record["id"], payload.get("vessel", ""), int(payload["eta_hour"]), int(payload["etd_hour"]), "closure"
                )
            continued.append(record["id"])
        self._sync_waiting()
        return {"reason": reason, "closed_at_hour": hour, "moved_to_waiting": moved, "berthed_continue": continued}

    def reopen_port(self, actor: Actor, reopen_hour: Any, channel_depth_m: Any, transit_hours: Any, available_pilots: Any) -> Dict[str, Any]:
        """复航重排：危险品优先、同风险先到先服务，独占航道时段，不足条件顺延。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "port_reopen"):
            raise PermissionDenied("角色无权执行复航")
        if not self._port_closed():
            raise Conflict("港口当前未处于封航状态")
        hour = integer({"reopen_hour": reopen_hour}, "reopen_hour", 0, 96)
        depth = number({"channel_depth_m": channel_depth_m}, "channel_depth_m", 1)
        transit = integer({"transit_hours": transit_hours}, "transit_hours", 1, 8)
        pilots = text_list({"available_pilots": available_pilots}, "available_pilots", 0)
        waiting = self.repository.list_by_states([WAITING_STATE])
        scheduled, postponed = build_schedule(waiting, self.repository.berth_free_at(), hour, depth, transit, pilots)
        scheduled_result: List[Dict[str, Any]] = []
        for sequence, item in enumerate(scheduled, start=1):
            record = item["record"]
            payload = dict(record["payload"])
            payload["pilot_id"] = item["pilot_id"]
            payload["channel_slot"] = {"start_hour": item["slot_start"], "end_hour": item["slot_end"]}
            payload["berth_window"] = {"start_hour": item["berth_start"], "end_hour": item["berth_end"]}
            payload.pop("wait_reason", None)
            payload.pop("wait_since_hour", None)
            self.repository.mutate(
                record_id=record["id"],
                expected_version=record["version"],
                state=SCHEDULED_STATE,
                payload=payload,
                actor_id=actor.user_id,
                action="reschedule",
                details={
                    "summary": "复航重排进场",
                    "channel_slot": payload["channel_slot"],
                    "berth_window": payload["berth_window"],
                    "pilot_id": item["pilot_id"],
                    "from": WAITING_STATE,
                    "to": SCHEDULED_STATE,
                },
            )
            self.repository.add_channel_slot(record["id"], payload.get("vessel", ""), sequence, item["slot_start"], item["slot_end"], item["pilot_id"])
            self.repository.add_berth_occupancy(payload["berth"], record["id"], payload.get("vessel", ""), item["berth_start"], item["berth_end"], "schedule")
            scheduled_result.append({
                "record_id": record["id"],
                "vessel": payload.get("vessel", ""),
                "berth": payload.get("berth", ""),
                "sequence": sequence,
                "channel_slot": payload["channel_slot"],
                "berth_window": payload["berth_window"],
                "pilot_id": item["pilot_id"],
            })
        postponed_result: List[Dict[str, Any]] = []
        for item in postponed:
            record = item["record"]
            payload = dict(record["payload"])
            payload["wait_reason"] = item["reason"]
            self.repository.mutate(
                record_id=record["id"],
                expected_version=record["version"],
                state=WAITING_STATE,
                payload=payload,
                actor_id=actor.user_id,
                action="postpone",
                details={"summary": item["reason"], "from": WAITING_STATE, "to": WAITING_STATE},
            )
            postponed_result.append({"record_id": record["id"], "vessel": payload.get("vessel", ""), "reason": item["reason"]})
        self._sync_waiting()
        self.repository.add_port_event("reopened", "复航重排：进场%d艘，顺延%d艘" % (len(scheduled), len(postponed)), actor.user_id)
        return {"reopen_hour": hour, "scheduled": scheduled_result, "postponed": postponed_result}

    def port_status(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        event = self.repository.latest_port_event()
        return {
            "status": "closed" if event and event["event_type"] == "closed" else "open",
            "latest_event": event,
            "events": self.repository.port_events(),
        }

    def waiting_queue(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.waiting_list()

    def channel_slots(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.channel_slots()

    def berth_occupancy(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.berth_occupancy()

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
