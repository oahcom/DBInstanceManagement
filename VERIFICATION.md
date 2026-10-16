# 验证报告

本文件记录对本题 5 项要求的实际验证过程与结果。所有数据来自真实执行的命令输出，非推断。
报告对应提交 `64a402b`。

## 环境

```
python       3.13.13
django       6.1.2
drf          3.18.3
celery       5.6.3
redis-py     8.1.0
cryptography 50.0.2
platform     Linux 6.18.40.1-microsoft-standard-WSL2
```

Redis broker 以容器提供（`docker run -d --name dbi-redis -p 6379:6379 redis:7-alpine`）。

## 一、自动化测试

```
$ .venv/bin/python manage.py test inventory
Found 25 test(s).
System check identified no issues (0 silenced).
Ran 25 tests in 8.045s

OK
```

25 个用例分布：

| 测试类 | 覆盖点 | 用例数 |
|--------|--------|--------|
| `AuthTests` | 匿名访问被拒（列表、明文密码） | 2 |
| `DepartmentCRUDTests` | 部门增删改查、编码唯一、空部门可删 | 4 |
| `ClusterCRUDTests` | 集群创建、被引用时删除返回 409、同部门重名 | 3 |
| `InstanceProbeTests` | 不可达端口 503、真实 `listen()` socket 可达 | 2 |
| `CredentialRotationTests` | 加密往返、轮换换新、未到期跳过、掩码/明文、只读 | 8 |
| `DailyStatTests` | 按部门+集群计数、重跑幂等、默认昨天、接口过滤 | 4 |
| `RequestTimingMiddlewareTests` | 耗时头存在且合理 | 2 |

## 二、真实全栈运行

四进程同时运行：Web（`runserver`）、Celery worker、Celery beat、Redis。

```bash
.venv/bin/python manage.py runserver 127.0.0.1:8920 --noreload
.venv/bin/celery -A config worker -l info -c 1
.venv/bin/celery -A config beat -l info
```

### 要求 1：模型

预置数据为 2 个部门 / 3 个集群 / 6 个实例，实例创建后凭据自动生成数与实例数一致：

```
凭据自动生成数: 6 / 实例数: 6
```

（后续 HTTP 冒烟测试又经接口 POST 创建了 1 个部门，故结束时库中共 3 个部门。）

### 要求 2：CRUD 与 TCP 探测

```
POST /api/departments/          -> 201
GET  /api/departments/ (匿名)   -> 403
GET  /api/instances/1/probe/    -> 200 {"reachable":true,"latency_ms":0.33,"error":""}
GET  /api/instances/1/probe/    -> 503 {"reachable":false,"latency_ms":3003.23,
                                        "error":"TimeoutError: timed out"}
```

可达与不可达两条分支都实测覆盖。不可达时耗时约 3 秒，说明 3 秒连接超时约束生效，
探测不会无限挂住请求线程。

### 要求 3：密码轮换

将 6 个实例的 `rotated_at` 拨回 13 小时（超过 12 小时周期），经 worker 实际执行轮换任务：

```
Task inventory.rotate_instance_passwords[4181e594-...] succeeded
ROT| order-db-1: changed=True pw_len=32 cipher_prefix=gAAAAABq
ROT| user-db-0: changed=True pw_len=32 cipher_prefix=gAAAAABq
```

轮换后明文与轮换前不同，落库内容为 Fernet 密文（`gAAAAABq` 前缀）。

### 要求 4：日统计

生产配置的 crontab 要到 00:00 / 12:00 才触发，为验证整条链路，用一个 10 秒周期的
临时 schedule entry 复用同一个任务函数，实测 beat → broker → worker → DB 全链路：

```
celery.beat: Scheduler: Sending due task e2e-probe (inventory.collect_daily_stats)
Task inventory.collect_daily_stats[4e2f6fd3-...] succeeded: {'stat_date': '2026-10-15', 'rows': 3}
```

数据库落库结果，与预置数据（订单库主集群 3 个实例、用户库集群 2 个、数仓集群 1 个）一致：

```
基础架构部 / 用户库集群     = 2
基础架构部 / 订单库主集群   = 3
数据平台部 / 数仓集群       = 1
```

重复执行同一日期不产生重复行（由 `uniq_daily_stat_scope` 唯一约束 + `update_or_create` 保证，
`DailyStatTests.test_rerun_same_date_does_not_duplicate_rows` 覆盖）。

调度语义经 crontab 字段断言确认：轮换 `hour={0,12}`、统计 `hour={0}, minute={0}`。

### 要求 5：请求耗时中间件

对四类响应路径逐一验证，均带 `X-Request-Duration-Ms`：

| 路径 | 状态码 | 耗时头 |
|------|--------|--------|
| `/api/departments` | 301 | 1.98 |
| `/api/departments/` | 403 | 0.32 |
| `/api/nope/` | 404 | 1.77 |
| `/admin/login/` | 200 | 2.08 |

## 三、验证过程中发现并修复的缺陷

审查阶段发现三处不符合要求的问题，均已修复（提交 `64a402b`）。

### 1. 计时中间件漏请求（要求 5 的字面违背）

中间件原本排在 `MIDDLEWARE` 最末位。`CommonMiddleware` 的 `APPEND_SLASH` 重定向会在它
之前短路返回，该请求因此不会被计时。实测：

```
修复前: GET /api/departments -> 301, 无 X-Request-Duration-Ms
修复后: GET /api/departments -> 301, X-Request-Duration-Ms: 3.53
```

将中间件移到 `MIDDLEWARE` 最外层后，它包住整条链，才真正覆盖"每个请求"。

### 2. 接口无鉴权

DRF 默认 `AllowAny`，任何人可匿名拉取实例清单，并用 `?reveal=1` 取到明文密码：

```
GET /api/credentials/?reveal=1 (匿名)
-> {"password":"VnTkJL7T2c4E60HG8oxQOkVIc83Vfvew", ...}
```

改为 `IsAuthenticated` + Session/Basic 认证后：

```
GET /api/credentials/?reveal=1 (匿名) -> 403
```

### 3. 凭据接口可写

`CredentialViewSet` 原为 `ModelViewSet`，认证用户可 POST 出空密码凭据，使实例处于
"有账号但登不上"的状态：

```
POST /api/credentials/ {"instance":1,"username":"hacked"}
-> 201, password_encrypted='' (len=0), rotated_at=null
```

改为 `ReadOnlyModelViewSet` 后返回 405，密码只由服务端生成。

### 附：Celery 配置链赋值陷阱

验证要求 4 时踩到：直接给 `app.conf.beat_schedule` 赋新字典会被 Celery 的配置链
**静默忽略**——不报错，但 scheduler 仍只读到 settings 里的原始条目：

```python
app.conf.beat_schedule = {"e2e-probe": {...}}   # 无效，探针任务不在 scheduler 里
```

必须用下标赋值才会生效：

```python
app.conf.beat_schedule["e2e-probe"] = {...}     # 有效
```

排查方式是打印 scheduler 实际持有的条目：`s.schedule.keys()`。之所以记录在此，是因为
该失败模式无声无息——第一次运行"成功"但零发包，容易被误读为"任务未到期所以不触发"。

## 四、未覆盖项

以下内容未在本轮验证范围内，如实列出：

1. **真实 crontab 的墙上时钟到点触发**未验证。00:00 / 12:00 的实际触发需要等待真实时间点，
   本轮仅验证了调度配置语义正确（crontab 字段断言）与链路可通（10 秒探针）。
2. **密码未真实下发到数据库**。`rotate()` 只更新本地加密记录，未连接目标数据库执行
   改密语句（`inventory/tasks.py` 内有 `ponytail:` 注释标注此边界与升级路径）。
3. 未做并发压测与长时间稳定性观测。
