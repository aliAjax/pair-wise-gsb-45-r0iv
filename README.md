# 港口泊位与航道调度（复航重排台）

纯Python标准库实现的港口泊位与航道调度服务，使用SQLite持久化，HTTP接口由`http.server`提供。
在原有靠泊流程之上扩展了封航/复航重排能力：封航时未开始的计划自动转入候泊并写明原因，
复航时按危险品优先、同风险先到先服务重排，每艘船独占航道时段，吃水不足或缺引航员则顺延。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、靠泊可行性、吃水安全、时间窗冲突和冲突检查。
- `src/reschedule.py`：复航重排算法（候泊排序、独占航道时段分配、顺延判定），纯函数实现。
- `src/repository.py`：SQLite建表、事务和查询；候泊顺序、航道时段、泊位占用分表保存。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：复航重排台页面。
- `tests/`：完整流程、规则计算、复航重排和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8321
```

默认端口为`8321`，默认数据库位于项目目录。服务启动时自动建表。

## 计划字段

创建计划（`POST /api/records`）的`data`需包含：

- `vessel` 船名、`captain_name` 船长、`berth` 泊位；
- `vessel_length_m`/`berth_length_m` 船长与泊位长度；
- `draft_m` 吃水、`berth_depth_m` 泊位水深（富余水深需≥0.5米）；
- `eta_hour` 到港时刻、`operation_hours` 作业时长（离港时刻自动推算）；
- `risk_level` 风险等级（low/medium/high）、`dangerous_goods` 是否危险品、
  `dangerous_class` 危险品等级（危险品必填）。

## 封航与复航

- `POST /api/port/close`：`{"reason":"台风预警","closed_at_hour":8}`。
  未开始的计划（草稿/已确认/已排期）转入`waiting`候泊并写入等待原因；
  已靠泊船舶继续作业，其泊位占用保留。
- `POST /api/port/reopen`：`{"reopen_hour":18,"channel_depth_m":12.0,"channel_transit_hours":2,"available_pilots":["P-01"]}`。
  按危险品优先、同风险先到先服务排序，结合泊位占用推算每船独占的航道时段；
  吃水不满足航道富余水深或无可用引航员的船舶顺延，留在候泊队列并更新原因。
- `GET /api/port/status`：港口当前状态与封航/复航事件。
- `GET /api/waiting`：候泊顺序（含每艘船的等待原因）。
- `GET /api/channel-slots`：航道时段分配（次序、时段、引航员）。
- `GET /api/berth-occupancy`：泊位占用（来源：靠泊/封航在泊/重排计划）。

## 其他接口

- `GET /health`：健康检查。
- `GET /`：复航重排台页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records/{id}/actions/{action}`：执行业务动作（confirm/berth/depart/cancel），
  请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、封航转候泊、复航排序与顺延、泊位占用约束、
重复引用、权限拒绝和版本冲突。
