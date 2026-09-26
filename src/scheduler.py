"""复航重排算法：候泊顺序、独占航道时段与泊位占用。

排序规则：危险品船优先；同风险等级按到港时刻先到先服务；
航道为单船独占资源，同时受引航员出勤时刻与泊位释放时刻约束，
吃水相对航道水深富余不足或无引航员时顺延，并记录具体原因。
"""
from typing import Any, Dict, List, Optional

DRAFT_MARGIN = 0.5
RISK_ORDER = {"high": 0, "medium": 1, "low": 2}
MAX_ATTEMPTS = 64


def fmt_hour(value: float) -> str:
    """把相对时刻（小时，可超过24表示次日）渲染为可读文本。"""
    total_minutes = int(round(float(value) * 60))
    day, minutes = divmod(total_minutes, 24 * 60)
    hour, minute = divmod(minutes, 60)
    clock = "%02d:%02d" % (hour, minute) if minute else "%02d:00" % hour
    if day:
        return "第%s天%s" % (day + 1, clock)
    return clock


def _priority_key(record: Dict[str, Any]):
    payload = record["payload"]
    dangerous_first = 0 if payload.get("dangerous_goods") else 1
    risk_rank = RISK_ORDER.get(payload.get("risk_level", "low"), 3)
    return dangerous_first, risk_rank, int(payload["eta_hour"]), record["id"]


def _waiting_info(payload: Dict[str, Any]) -> Dict[str, Any]:
    info = payload.get("waiting") or {}
    return {
        "reason": str(info.get("reason", "channel_closed")),
        "detail": str(info.get("reason_text", "封航后转入候泊")),
        "since_hour": info.get("since_hour"),
    }


def _vessel_row(record: Dict[str, Any]) -> Dict[str, Any]:
    payload = record["payload"]
    return {
        "record_id": record["id"],
        "reference": record.get("reference", ""),
        "vessel": payload.get("vessel", ""),
        "berth": payload.get("berth", ""),
    }


def _queue_entry(position: int, record: Dict[str, Any]) -> Dict[str, Any]:
    payload = record["payload"]
    waiting = _waiting_info(payload)
    entry = _vessel_row(record)
    entry.update(
        {
            "position": position,
            "risk_level": payload.get("risk_level", "low"),
            "dangerous_goods": bool(payload.get("dangerous_goods")),
            "dangerous_class": payload.get("dangerous_class", ""),
            "eta_hour": int(payload["eta_hour"]),
            "reason": waiting["reason"],
            "detail": waiting["detail"],
            "since_hour": waiting["since_hour"],
        }
    )
    return entry


def _end_hour(payload: Dict[str, Any]) -> float:
    if payload.get("operation_end_hour") is not None:
        return float(payload["operation_end_hour"])
    return float(payload.get("etd_hour"))


def _duration(payload: Dict[str, Any]) -> float:
    if payload.get("operation_duration_hours") is not None:
        return float(payload["operation_duration_hours"])
    return _end_hour(payload) - float(payload["eta_hour"])


def build_plan(
    waiting_records: List[Dict[str, Any]],
    berthed_records: List[Dict[str, Any]],
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """根据候泊船舶、在泊船舶和复航参数生成重排计划。

    返回三个彼此独立的清单：queue（候泊顺序）、slots（航道时段）、
    occupancy（泊位占用）。未获时段的船在 slots 中以 postponed 落项。
    """
    reopen_hour = float(params["reopen_hour"])
    transit_hours = float(params["transit_hours"])
    channel_depth = float(params["channel_depth_m"])
    pilots = [
        {"pilot_id": str(item["pilot_id"]), "free_from": float(item["available_from"])}
        for item in params.get("pilots", [])
    ]

    ordered = sorted(waiting_records, key=_priority_key)
    queue = [_queue_entry(position, record) for position, record in enumerate(ordered, start=1)]

    occupancy: List[Dict[str, Any]] = []
    berth_free: Dict[str, float] = {}
    for record in berthed_records:
        payload = record["payload"]
        end_hour = _end_hour(payload)
        if end_hour <= reopen_hour:
            # 作业在复航前结束，泊位已释放，不进入占用表。
            continue
        start_hour = float(payload.get("eta_hour", 0))
        item = _vessel_row(record)
        item.update(
            {
                "start_hour": start_hour,
                "end_hour": end_hour,
                "source": "ongoing",
                "detail": "封航期间泊位作业继续",
            }
        )
        occupancy.append(item)
        berth_free[payload["berth"]] = max(berth_free.get(payload["berth"], -1.0), end_hour)

    slots: List[Dict[str, Any]] = []
    channel_cursor = reopen_hour
    for position, record in enumerate(ordered, start=1):
        payload = record["payload"]
        berth = payload["berth"]
        eta_hour = float(payload["eta_hour"])
        draft = float(payload["draft_m"])
        base_slot = _vessel_row(record)
        base_slot["sequence"] = position

        if channel_depth - draft < DRAFT_MARGIN:
            slot = dict(base_slot)
            slot.update(
                {
                    "start_hour": None,
                    "end_hour": None,
                    "pilot_id": None,
                    "status": "postponed",
                    "reason": "draft_insufficient",
                    "detail": "吃水%s米，航道水深%s米，富余不足%s米，等待乘潮或疏浚"
                    % (draft, channel_depth, DRAFT_MARGIN),
                }
            )
            slots.append(slot)
            continue

        earliest = max(channel_cursor, eta_hour, reopen_hour)
        causes = set()
        if channel_cursor > max(reopen_hour, eta_hour):
            causes.add("channel")

        start_hour: Optional[float] = None
        pilot_id: Optional[str] = None
        postponed: Optional[tuple] = None
        for _ in range(MAX_ATTEMPTS):
            candidate = earliest
            pushed = False
            occupied_until = berth_free.get(berth)
            if occupied_until is not None and occupied_until - transit_hours > candidate:
                earliest = occupied_until - transit_hours
                causes.add("berth")
                pushed = True
            available = [item for item in pilots if item["free_from"] <= candidate]
            if not available:
                if not pilots:
                    postponed = ("no_pilot", "复航时没有可派遣的引航员，等待引航员到位")
                    break
                next_pilot = min(pilots, key=lambda item: (item["free_from"], item["pilot_id"]))
                if next_pilot["free_from"] > candidate:
                    earliest = max(earliest, next_pilot["free_from"])
                    causes.add("pilot")
                    pushed = True
            if pushed:
                continue
            chosen = min(available, key=lambda item: (item["free_from"], item["pilot_id"]))
            finish = candidate + transit_hours
            occupied_until = berth_free.get(berth)
            if occupied_until is not None and finish + 1e-9 < occupied_until:
                earliest = max(earliest, occupied_until - transit_hours)
                causes.add("berth")
                continue
            start_hour = candidate
            pilot_id = chosen["pilot_id"]
            chosen["free_from"] = finish
            break
        else:
            postponed = ("constraint_loop", "约束推演未收敛，需人工介入")

        if postponed is not None:
            slot = dict(base_slot)
            slot.update(
                {
                    "start_hour": None,
                    "end_hour": None,
                    "pilot_id": None,
                    "status": "postponed",
                    "reason": postponed[0],
                    "detail": postponed[1],
                }
            )
            slots.append(slot)
            continue

        end_hour = start_hour + transit_hours
        reasons: List[str] = []
        if "berth" in causes:
            reasons.append("泊位%s被占用至%s" % (berth, fmt_hour(berth_free[berth])))
        if "pilot" in causes:
            next_pilot = min(pilots, key=lambda item: (item["free_from"], item["pilot_id"]))
            reasons.append("引航员最早%s可用" % fmt_hour(next_pilot["free_from"]))
        if "channel" in causes:
            reasons.append("前序船舶占用航道，顺延进场")

        slot = dict(base_slot)
        slot.update(
            {
                "start_hour": start_hour,
                "end_hour": end_hour,
                "pilot_id": pilot_id,
                "status": "scheduled",
                "reason": None,
                "detail": "；".join(reasons) if reasons else "",
            }
        )
        slots.append(slot)

        operation_end = end_hour + _duration(payload)
        occupied = _vessel_row(record)
        occupied.update(
            {
                "start_hour": end_hour,
                "end_hour": operation_end,
                "source": "planned",
                "detail": "作业时长%s小时" % _duration(payload),
            }
        )
        occupancy.append(occupied)
        berth_free[berth] = max(berth_free.get(berth, end_hour), operation_end)
        channel_cursor = end_hour

    return {"queue": queue, "slots": slots, "occupancy": occupancy}
