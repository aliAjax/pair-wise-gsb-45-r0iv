"""复航重排算法：候泊排序、独占航道时段分配与顺延判定。

纯函数实现，不依赖存储层，便于单测。所有时刻使用同一时间轴上的整数小时。
"""
from typing import Any, Dict, List, Tuple


RISK_RANK = {"high": 0, "medium": 1, "low": 2}
MIN_UNDERKEEL_CLEARANCE_M = 0.5


def queue_sort_key(record: Dict[str, Any]) -> Tuple[int, int, int, int]:
    """危险品优先，同风险先到先服务，再按记录号兜底保证稳定。"""
    payload = record["payload"]
    dangerous = 0 if payload.get("dangerous_goods") else 1
    risk = RISK_RANK.get(payload.get("risk_level"), len(RISK_RANK))
    eta = int(payload.get("eta_hour", 0))
    return (dangerous, risk, eta, int(record["id"]))


def order_waiting(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(records, key=queue_sort_key)


def build_schedule(
    records: List[Dict[str, Any]],
    berth_free_at: Dict[str, int],
    reopen_hour: int,
    channel_depth_m: float,
    transit_hours: int,
    pilot_pool: List[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """为候泊船舶分配复航计划。

    返回 (scheduled, postponed)：
    - scheduled 每项含 record、pilot_id、航道时段 slot_start/slot_end、
      泊位占用窗口 berth_start/berth_end；
    - postponed 每项含 record 与顺延原因 reason。

    每艘船独占一段航道时段；吃水不满足航道富余水深或无可用引航员的
    船舶不占用时段，直接顺延。
    """
    scheduled: List[Dict[str, Any]] = []
    postponed: List[Dict[str, Any]] = []
    pilots = list(pilot_pool)
    cursor = int(reopen_hour)
    free_at = dict(berth_free_at)
    for record in order_waiting(records):
        payload = record["payload"]
        draft = float(payload.get("draft_m", 0))
        if float(channel_depth_m) - draft < MIN_UNDERKEEL_CLEARANCE_M:
            postponed.append({
                "record": record,
                "reason": "吃水%.1f米不满足航道水深%.1f米的富余要求，顺延" % (draft, float(channel_depth_m)),
            })
            continue
        pilot_id = payload.get("pilot_id") or ""
        if not pilot_id:
            if pilots:
                pilot_id = pilots.pop(0)
            else:
                postponed.append({"record": record, "reason": "缺少可用引航员，顺延"})
                continue
        berth = payload.get("berth", "")
        berth_free = int(free_at.get(berth, 0))
        start = max(cursor, berth_free - int(transit_hours))
        slot_end = start + int(transit_hours)
        operation = int(payload.get("operation_hours", 1))
        berth_end = slot_end + operation
        scheduled.append({
            "record": record,
            "pilot_id": pilot_id,
            "slot_start": start,
            "slot_end": slot_end,
            "berth_start": slot_end,
            "berth_end": berth_end,
        })
        free_at[berth] = berth_end
        cursor = slot_end
    return scheduled, postponed
