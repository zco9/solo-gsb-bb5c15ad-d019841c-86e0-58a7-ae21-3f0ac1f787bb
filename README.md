# 跑道离场尾流间隔校验 API

基于 **Python + FastAPI + SQLite** 的跑道离场尾流（wake turbulence）间隔校验服务。
客户端按跑道提交航班计划与前机—后机最小间隔矩阵，并可为每条跑道配置**临时关闭窗口**
与**滚动容量约束**，服务端完成两件事：

1. **间隔校验（validate）**：按计划离场时刻、航班号稳定排序，列出同跑道相邻航班的间隔违规、
   计划离场时刻落入关闭窗口的航班及其命中窗口，以及滚动容量约束的超限航班、触发窗口与
   最早合规时刻。
2. **试排（trial）**：保持原航班顺序，逐项把航班推迟到**同时满足尾流间隔、滚动容量约束且
   避开关闭窗口**的最早安全时刻；容量/关闭导致的延误沿队列向后级联，
   容量推迟后若落入关闭窗口会继续迭代避让，**原计划时刻绝不改写**。
   返回调整后时刻、单项延误、触发前机、容量与关闭窗口命中情况。

每次请求（含输入排序结果、关闭窗口、容量配置、违规/冲突/试排结论）都会持久化到 SQLite，
可用批次 ID 回查。

---

## 一条命令启动

```bash
docker compose up -d --build
```

启动后访问 **http://localhost:8000** ：

- Swagger 交互文档：http://localhost:8000/docs
- ReDoc：http://localhost:8000/redoc
- 健康检查：http://localhost:8000/health
- 默认间隔矩阵：http://localhost:8000/matrix/default

停止：`docker compose down`（数据保留在命名卷 `wake-data` 中；加 `-v` 可一并清除）。

## 必要配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WAKE_DB_PATH` | `/data/wake.db` | SQLite 数据库文件路径，容器内挂载在持久卷 `/data` |
| `WAKE_HOST` | `0.0.0.0` | uvicorn 监听地址 |
| `WAKE_PORT` | `8000` | uvicorn 监听端口（容器内固定暴露 8000） |

端口映射在 `docker-compose.yml` 的 `ports` 中配置（默认 `8000:8000`，宿主机端口被占用时改左侧即可）。

---

## 接口

### `POST /api/v1/validate` — 间隔违规校验

请求体：

```json
{
  "runways": [
    {
      "runway": "36L",
      "flights": [
        {"flight_no": "CA101", "scheduled_off": "2026-09-18T08:00:00+08:00", "wake": "J"},
        {"flight_no": "CA102", "scheduled_off": "2026-09-18T08:01:00+08:00", "wake": "M"},
        {"flight_no": "CA103", "scheduled_off": "2026-09-18T08:02:30+08:00", "wake": "M"}
      ],
      "closures": [
        {"start": "2026-09-18T08:02:30+08:00", "end": "2026-09-18T08:03:30+08:00"}
      ],
      "capacity": {"window_seconds": 600, "max_departures": 3}
    }
  ]
}
```

- `flight_no`：航班号，**整个请求内唯一**（跑道内、跑道间均不可重复）。
- `scheduled_off`：计划离场时刻，ISO 8601 且**必须带时区**（`Z` 或 `+08:00` 偏移），服务端统一按 UTC 处理。
- `wake`：尾流等级，四档 —— `L` 轻型 / `M` 中型 / `H` 重型 / `J` 超重型。
- 跑道按其标识独立排序与校验；排序规则为 `(scheduled_off 升序, flight_no 升序)` 的稳定排序。
- 仅检查排序后**相邻**两架航班（单跑道离场的运行惯例）：前机尾流等级 → 后机尾流等级查矩阵，
  实际间隔 < 要求间隔即违规，返回要求秒数、实际秒数与缺口秒数。

#### 跑道临时关闭窗口（可选字段 `closures`）

每条跑道可携带多个关闭窗口，语义为 **`[start, end)` 左闭右开**：

- `start` / `end` 均为 ISO 8601 且**必须带时区**，服务端统一归一化为 UTC；
- `end` 必须**晚于** `start`（相等或倒置都会被拒绝，`422`）；
- 同一跑道内窗口**不得重叠**（`422`）；首尾相接（前一个的 `end` 等于后一个的 `start`）是允许的；
- 响应中 `closures` 按开始时刻升序、以 UTC 原样回显。

validate 额外返回**关闭冲突**：凡计划离场时刻落在某个窗口内（含 `start`、不含 `end`）的航班，
在对应跑道的 `closure_conflicts` 中列出，含 `flight_no`、`position`（排序后位次）、
`window_index`（命中窗口在回显 `closures` 中的 1 基序号）与窗口起止时刻；
跑道级与批次级分别汇总 `closure_conflict_count` / `total_closure_conflicts`。

#### 跑道滚动容量约束（可选字段 `capacity`）

每条跑道可携带至多一个容量约束：

```json
"capacity": {"window_seconds": 600, "max_departures": 3}
```

- 语义为**滚动半开区间**：任意 `[t, t + window_seconds)` 内该跑道的离场数不得超过
  `max_departures`；两个字段都必须是**正整数**（`0`、负数、小数、字符串、布尔均被拒绝，`422`），
  缺省或显式 `null` 表示该跑道不施加容量约束。
- 对稳定排序后的航班序列，判定等价于：航班 `p` 与其前方第 `max_departures` 架航班（锚机）的
  计划时刻之差必须 `>= window_seconds`；否则以锚机时刻为起点的窗口
  `[anchor_off, anchor_off + window_seconds)` 内至少有 `max_departures + 1` 架离场，
  该航班记一次超限（窗口随航班向前滑动，同一航班最多记一次）。
- validate 在对应跑道返回 `capacity`（原样回显配置）、`capacity_violation_count` 与
  `capacity_violations`，每条含：
  `flight_no` / `position` / `scheduled_off`、`window_seconds` / `max_departures`、
  `anchor_flight_no` / `anchor_position`（锚机及其位次）、
  `window_start` / `window_end`（触发窗口，UTC）、`departures_in_window`（该窗口内实际离场数）
  以及 **`earliest_compliant_off`（最早合规时刻 = 锚机时刻 + window_seconds）**；
  批次级在**有任意跑道配置 capacity 时**汇总 `total_capacity_violations`。
  列表按稳定排序位次升序。

### `POST /api/v1/trial` — 试排（级联推迟）

请求体与 validate 完全一致（含 `closures`、`capacity`）。算法：

- 顺序保持为稳定排序结果；航班**只会推迟、绝不提前**；
- 每架航班取满足以下全部条件的最早时刻：
  1. 不早于其原计划时刻；
  2. 不早于 `直接前机调整时刻 + 要求间隔`（首架航班无前机约束）；
  3. 不落在任何关闭窗口内 —— 若落入窗口则推迟到该窗口 `end`；窗口首尾相接时会**继续迭代**，
     直到完全离开关闭时段（尾流推迟后落入后续窗口的情形同样处理）；
  4. 满足滚动容量约束 —— 不早于 `前方第 max_departures 架航班调整时刻 + window_seconds`；
- 容量推迟可能恰好落入（之后的）关闭窗口、关闭推迟也必须重新满足容量，因此两类约束**交替迭代**
  直到同时成立，延误继续向后续航班级联；
- 每项返回 `adjusted_off`（调整后时刻，UTC）、`delay_seconds`（单项延误，非负）、
  `triggered_by`（触发本次推迟的直接前机航班号；未因尾流推迟为 `null`）、
  `closure_hit`（是否因关闭窗口产生了额外推迟）与
  `closure_window`（最后命中并导致推迟的窗口，未命中为 `null`）；
  **配置了 capacity 的跑道**还返回 `capacity_hit`、`capacity_anchor`（容量窗口锚机航班号）、
  `capacity_window_seconds` / `capacity_max_departures` 与
  `capacity_window`（触发容量推迟的半开窗口 `[锚机调整时刻, +window_seconds)`，
  未因容量推迟为 `null`），跑道对象同时原样回显 `capacity`；
- 该接口只返回试排建议，`scheduled_off` 原值原样返回，数据库中也同时保存原计划。

### `POST` 边界示例

| 场景 | 计划 / 配置（窗口 60 秒、容量 2 架） | validate 结论 | trial 调整 |
| --- | --- | --- | --- |
| 恰好装满 | 00:00、00:30、01:00 | 无超限（锚机 00:00 的窗口 `[00:00,01:00)` 右端不含） | 全部不动 |
| 第三架落入 | 00:00、00:30、**00:59** | CA3 超限；触发窗口 `[00:00,01:00)`，`departures_in_window=3`，最早合规 `01:00` | CA3 → **01:00**（`capacity_hit=true`，锚机 CA1） |
| 容量推迟撞关闭 | 00:00、00:30、00:59；关闭 `[01:00,01:40)` | 同上 | CA3 → 01:00 撞窗口 → **01:40**（`capacity_hit` 与 `closure_hit` 均为 `true`） |
| 窗口滑动 | 00:00、00:40、01:20、02:00（窗口 90 秒、2 架） | 第 3 架（锚机第 1 架，跨度 80 < 90）、第 4 架（锚机第 2 架，跨度 80 < 90）各一次 | 逐项推迟到锚机 + 90 秒，向后级联 |

### `GET /api/v1/batches/{batch_id}` — 批次回查

两个 POST 接口的返回都带 `batch_id`，可随时回查当时的输入顺序、生效矩阵、关闭窗口、
容量配置与结论（validate 的违规/关闭冲突/容量超限 / trial 的试排结果），字段与 POST 响应一致：
配置了 capacity 的跑道回显 `capacity` 及容量结论/试排明细，未配置的跑道**不出现**容量字段，
保持旧响应形状。不存在的 ID 返回 `404`。

### 间隔矩阵（可选字段 `matrix`）

请求可携带 `matrix` 覆盖默认矩阵：

```json
"matrix": [
  {"leader": "H", "follower": "M", "seconds": 120},
  {"leader": "J", "follower": "M", "seconds": 180}
]
```

- 省略 `matrix` 时使用内置的 ICAO 风格默认矩阵（可用 `GET /matrix/default` 查看）：

  | 前机 \ 后机 | L | M | H | J |
  | --- | --- | --- | --- | --- |
  | **L** | 100 | 60 | 60 | 60 |
  | **M** | 100 | 60 | 60 | 60 |
  | **H** | 100 | 120 | 100 | 60 |
  | **J** | 180 | 180 | 180 | 120 |

  （单位：秒，示例默认值，实际运行请以当地管制规定通过 `matrix` 覆盖。）

- **提供** `matrix` 时只有列出的组合生效，未列出的组合视为无最小间隔（0 秒）。
- 同一 `(leader, follower)` 组合重复、`seconds` 为负数均会被拒绝（`422`）。

### 错误约定

- `422`：请求体不合法（缺时区、航班号/跑道重复、尾流等级非法、矩阵条目非法、
  关闭窗口结束不晚于开始或彼此重叠、容量字段非正整数等），响应体含中文原因。
- `404`：批次 ID 不存在。

---

## curl 示例

```bash
# 间隔校验
curl -s -X POST http://localhost:8000/api/v1/validate \
  -H 'Content-Type: application/json' \
  -d @sample.json

# 试排
curl -s -X POST http://localhost:8000/api/v1/trial \
  -H 'Content-Type: application/json' \
  -d @sample.json

# 回查批次
curl -s http://localhost:8000/api/v1/batches/<batch_id>
```

`sample.json` 已随仓库提供（36L 跑道含关闭窗口与「10 分钟内最多 3 架」滚动容量约束，
18R 跑道含关闭窗口），可直接使用。

---

## 本地开发（不用 Docker）

需要 Python 3.11+：

```bash
python3 -m pip install -r requirements.txt
export WAKE_DB_PATH=./wake.db            # 可选，默认 /data/wake.db
uvicorn app.main:app --reload --port 8000
```

运行测试（含 55 个领域逻辑与 API 端到端用例，使用临时 SQLite 文件）：

```bash
python3 -m pip install pytest httpx
python3 -m pytest -q
```

## 项目结构

```
app/
  main.py         # FastAPI 路由（validate / trial / batches / 元信息）
  schemas.py      # Pydantic 请求与响应模型、输入校验（含关闭窗口/容量合法性与兼容序列化）
  service.py      # 请求 -> 领域对象 -> 模式计算
  separation.py   # 核心领域逻辑：稳定排序、相邻违规、关闭冲突、滚动容量校验、避窗/容量级联试排、默认矩阵
  timeutils.py    # 时区归一化（统一 UTC，整数秒运算）
  database.py     # SQLite 连接、建表与旧库迁移
  repository.py   # 批次/航班/关闭窗口/容量/冲突/违规/试排结果的持久化与回查
  config.py       # 环境变量配置
tests/            # pytest 领域单测 + TestClient API 测试
Dockerfile
docker-compose.yml
sample.json       # 含关闭窗口与滚动容量约束的示例请求
requirements.txt
```
