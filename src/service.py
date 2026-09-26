"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, number, optional_text, text
from .repository import Repository
from .rules import DomainRules
from .scheduler import build_plan


CLOSE_SOURCE_STATES = ("draft", "confirmed")


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

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

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
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ---- 封航 / 复航重排 ----

    def _channel_open(self) -> bool:
        event = self.repository.last_channel_event()
        return event is None or event["kind"] == "reopen"

    def close_channel(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "close"):
            raise PermissionDenied("角色无权执行封航")
        if not self._channel_open():
            raise Conflict("航道已处于封航状态")
        data = dict(data or {})
        close_hour = number(data, "close_hour", 0)
        reason = optional_text(data, "reason", "航道封航")

        def mutate_payload(record: Dict[str, Any]) -> Dict[str, Any]:
            _, payload, _ = self.rules.apply_action(record, "close", {"close_hour": close_hour, "reason": reason})
            return payload

        def details(record: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "summary": "封航转入候泊：%s" % reason,
                "from": record["state"],
                "to": "waiting",
                "close_hour": close_hour,
                "reason": reason,
            }

        updated = self.repository.bulk_transition(
            CLOSE_SOURCE_STATES, "waiting", mutate_payload, actor.user_id, "close", details
        )
        event = self.repository.add_channel_event(
            "close",
            actor.user_id,
            {"reason": reason, "close_hour": close_hour, "affected": [item["id"] for item in updated]},
            at_hour=close_hour,
        )
        return {"event": event, "waiting_count": len(updated), "records": updated}

    def reopen_channel(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "reopen"):
            raise PermissionDenied("角色无权执行复航")
        close_event = self.repository.last_channel_event()
        if close_event is None or close_event["kind"] != "close":
            raise Conflict("当前未处于封航状态，无需复航重排")
        close_hour = float(close_event.get("at_hour") or 0)
        params = self.rules.validate_reopen_params(data or {}, close_hour)

        all_records = self.repository.list_records(limit=500)
        waiting_records = [item for item in all_records if item["state"] == "waiting"]
        berthed_records = [item for item in all_records if item["state"] == "berthed"]
        params["pilots"] = self._resolve_pilots(params["pilots"], waiting_records)

        plan = build_plan(
            waiting_records,
            berthed_records,
            {
                "reopen_hour": params["reopen_hour"],
                "transit_hours": params["channel_transit_hours"],
                "channel_depth_m": params["channel_depth_m"],
                "pilots": params["pilots"],
            },
        )
        batch_id = self.repository.save_plan("reopen", params, actor.user_id, plan)

        slot_by_record = {item["record_id"]: item for item in plan["slots"]}
        queue_by_record = {item["record_id"]: item for item in plan["queue"]}

        def annotate(record: Dict[str, Any]) -> Dict[str, Any]:
            payload = dict(record["payload"])
            slot = slot_by_record.get(record["id"], {})
            queue = queue_by_record.get(record["id"], {})
            payload["waiting"] = {
                "reason": queue.get("reason", payload.get("waiting", {}).get("reason", "channel_closed")),
                "reason_text": queue.get("detail", ""),
                "since_hour": queue.get("since_hour"),
                "queue_position": queue.get("position"),
            }
            payload["reschedule"] = {
                "batch_id": batch_id,
                "status": slot.get("status", "postponed"),
                "channel_start_hour": slot.get("start_hour"),
                "channel_end_hour": slot.get("end_hour"),
                "pilot_id": slot.get("pilot_id"),
                "postpone_reason": slot.get("reason"),
                "postpone_detail": slot.get("detail", ""),
                "delay_detail": slot.get("detail", "") if slot.get("status") == "scheduled" else "",
            }
            return payload

        self.repository.bulk_transition(
            ("waiting",),
            "waiting",
            annotate,
            actor.user_id,
            "replan",
            lambda record, payload: {
                "summary": "复航重排结果：%s" % payload["reschedule"]["status"],
                "batch_id": batch_id,
                "reschedule": payload["reschedule"],
            },
        )
        event = self.repository.add_channel_event(
            "reopen",
            actor.user_id,
            {"batch_id": batch_id, "params": params, "scheduled": sum(1 for item in plan["slots"] if item["status"] == "scheduled"), "postponed": sum(1 for item in plan["slots"] if item["status"] == "postponed")},
            at_hour=params["reopen_hour"],
            batch_id=batch_id,
        )
        return {"event": event, "batch_id": batch_id, "params": params, "plan": plan}

    @staticmethod
    def _resolve_pilots(roster: List[Dict[str, Any]], waiting_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        pilots = {item["pilot_id"]: item["available_from"] for item in roster}
        for record in waiting_records:
            pilot_id = record["payload"].get("pilot_id")
            if isinstance(pilot_id, str) and pilot_id.strip():
                pilots.setdefault(pilot_id.strip(), 0)
        return [{"pilot_id": key, "available_from": value} for key, value in sorted(pilots.items())]

    def latest_plan(self, actor: Actor) -> Optional[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.load_latest_plan("reopen")

    def get_plan(self, actor: Actor, batch_id: int) -> Optional[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.load_plan(batch_id)

    def channel_status(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        event = self.repository.last_channel_event()
        events = self.repository.channel_events(limit=20)
        return {
            "status": "closed" if event and event["kind"] == "close" else "open",
            "last_event": event,
            "events": events,
            "latest_batch_id": self.repository.latest_batch_id("reopen"),
        }
