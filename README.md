# 港口泊位与航道调度 · 复航重排台

纯Python标准库实现的港口泊位与航道调度原型，使用SQLite持久化，HTTP接口由`http.server`提供。
支持封航/复航场景：封航后泊位作业继续、未开始船舶统一转入候泊并写明原因；复航时按
危险品优先、同风险先到先服务重排，每艘船独占航道时段，吃水不够或缺引航员则顺延。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、靠泊可行性、吃水安全、时间窗冲突、封航与复航参数校验。
- `src/scheduler.py`：复航重排算法（候泊排序、独占航道时段、泊位占用推演）。
- `src/repository.py`：SQLite建表、事务、航道事件和重排批次查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：复航重排台页面（建档、封航/复航、三张排程表、逐船原因）。
- `tests/`：完整流程、规则计算、重排算法、封航复航流程和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8321
```

默认端口为`8321`，默认数据库位于项目目录。服务启动时自动建表。

## 计划字段

建档（`POST /api/records`）除原有字段外包含：

- `vessel`：船名；`captain`：船长（联系人），均必填。
- `draft_m`：吃水（米）；`berth_depth_m`：泊位水深，富余不足0.5米拒绝建档。
- `risk_level`：危险品/风险等级 `low|medium|high`；
  `dangerous_goods=true` 时必须提供 `dangerous_class`。
- `eta_hour`：到港时刻（小时，0-23）。
- `operation_duration_hours`：作业时长（小时，0.1-48）；与 `etd_hour` 至少提供一个。

## 封航 / 复航流程

1. `POST /api/channel/close`，体为 `{"data":{"close_hour":8,"reason":"大雾封航"}}`：
   所有 `draft/confirmed` 船舶整体转为 `waiting`，payload 写入 `waiting`（原因、说明、
   封航时刻）；`berthed` 船舶不动，泊位作业继续。重复封航返回 409。
2. `POST /api/channel/reopen`，体为 `{"data":{...}}`，参数：
   - `reopen_hour`：复航时刻（不得早于封航时刻）；
   - `channel_depth_m`：航道水深，吃水富余不足0.5米的船顺延（`draft_insufficient`）；
   - `channel_transit_hours`：单船过航时长，航道同一时刻只允许一艘船；
   - `pilots`：引航员名单 `[{"pilot_id":"P-01","available_from":12}]`；
     封航前已确认引航员的船舶自动并入名单；名单为空则全部顺延（`no_pilot`）。
3. 排序规则：危险品船优先，其次风险等级 high→medium→low，同等级按 `eta_hour` 先到先服务；
   再考虑航道独占、引航员可派时刻、泊位释放时刻逐项推演。顺延/延迟原因写入每条航道时段
   （如“前序船舶占用航道”“引航员最早14:00可用”“泊位B1被占用至…”）。
4. 重排结果分三张表持久化，并回写到船舶 payload 的 `reschedule`：
   - 候泊顺序 `waiting_queue`（顺位、等待原因）；
   - 航道时段 `channel_slots`（独占起讫、引航员、顺延原因）；
   - 泊位占用 `berth_occupancy`（封航前在泊的 ongoing + 复航进场的 planned）。
5. 候泊船舶可在其航道时段后执行 `berth` 动作靠泊（`waiting → berthed`）。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：复航重排台页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情（含 `waiting` 与 `reschedule`）。
- `GET /api/records/{id}/audit`：审计时间线（含 `close`、`replan` 事件）。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作
  （`confirm/close/berth/depart/cancel`，单船 `close` 亦可），请求体为
  `{"expected_version":1,"data":{...}}`。
- `POST /api/channel/close` / `POST /api/channel/reopen`：封航与复航重排。
- `GET /api/channel/status`：航道状态与最近事件。
- `GET /api/plans/latest` / `GET /api/plans/{batch_id}`：最近/指定批次的三张排程表。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖完整流程、规则计算、复航重排算法（危险品优先、FCFS、航道独占、到港时刻、吃水顺延、
引航员顺延、泊位占用）、封航复航端到端落库、权限拒绝和版本冲突。
