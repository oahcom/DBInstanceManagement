# 数据库实例管理系统

企业内部数据库实例管理服务：实例/集群/部门建模，增删改查接口，TCP 端口可达性探测，
每 12 小时随机轮换实例密码并加密记录，每天 00:00 按部门和集群维度统计实例数量，
以及统计每个请求耗时的中间件。

技术栈：Python 3.13 / Django 6.1 / Django REST Framework / Celery / Redis / cryptography。

## 题目要求与实现对照

| # | 题目要求 | 实现 |
|---|----------|------|
| 1 | 实例、集群、部门模型 | `inventory/models.py`：`Department` / `Cluster` / `DBInstance` |
| 2 | 增删改查接口 + TCP 端口探测 API | DRF ViewSet 全量 CRUD；`GET/POST /api/instances/{id}/probe/` |
| 3 | 每 12 小时随机轮换密码并加密记录 | Celery beat `0 */12 * * *` + Fernet 加密的 `Credential` |
| 4 | 每天 00:00 按部门和集群统计实例数并写库 | Celery beat `0 0 * * *` + `InstanceDailyStat` |
| 5 | 中间件统计请求耗时 | `inventory/middleware.py`：`RequestTimingMiddleware` |

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser   # 可选，用于 /admin/
```

需要 Redis 作为 Celery broker。本机没有的话用容器起一个：

```bash
docker run -d --name dbi-redis -p 6379:6379 redis:7-alpine
```

三个进程分别启动：

```bash
.venv/bin/python manage.py runserver          # Web
.venv/bin/celery -A config worker -l info     # 执行任务的 worker
.venv/bin/celery -A config beat -l info       # 触发定时任务的调度器
```

跑测试：

```bash
.venv/bin/python manage.py test inventory
```

## 数据模型

| 模型 | 说明 | 关键约束 |
|------|------|----------|
| `Department` | 部门 | `name` / `code` 唯一 |
| `Cluster` | 集群，归属一个部门 | (部门, 集群名) 联合唯一；对部门 `PROTECT` |
| `DBInstance` | 数据库实例，归属一个集群 | `name` 唯一；端口限制 1-65535；对集群 `PROTECT` |
| `Credential` | 实例账号密码，与实例一对一 | 只存 Fernet 密文；实例创建时自动生成 |
| `InstanceDailyStat` | 按日/部门/集群的实例数快照 | (日期, 部门, 集群) 联合唯一，可重复执行 |

`PROTECT` 是刻意的：删除部门不该静默带走它下面的集群和实例。被子资源引用时接口返回
**409** 而不是 500，调用方能知道删不掉的原因。

## 接口

统一前缀 `/api/`。**所有接口需要认证**（管理实例凭据属于运维后台场景）：先
`manage.py createsuperuser`，再用 Session 登录 `/admin/` 后调用，或直接用 Basic Auth。

```bash
curl -u ops:你的密码 http://127.0.0.1:8000/api/instances/
```

| 路径 | 说明 |
|------|------|
| `/api/departments/` | 部门增删改查 |
| `/api/clusters/` | 集群增删改查 |
| `/api/instances/` | 实例增删改查 |
| `/api/credentials/` | 凭据查询（**只读**，密码只由服务端生成） |
| `/api/daily-stats/?date=YYYY-MM-DD` | 日统计查询（只读） |

凭据接口刻意只读：密码由服务端生成，允许客户端写入就能 POST 出
`password_encrypted=''` 的空密码凭据，让实例处于"有账号但登不上"的状态。

两个自定义动作：

```bash
# 探测实例 TCP 端口可达性。可达 200，不可达 503（探测结果是运维信号，用状态码区分）
# 连接超时 3 秒，不可达的实例不会挂住请求线程
curl -u ops:你的密码 http://127.0.0.1:8000/api/instances/1/probe/

# 立即轮换密码，响应中的明文只出现这一次
curl -u ops:你的密码 -X POST http://127.0.0.1:8000/api/instances/1/rotate-password/
```

密码默认不回显明文。列表接口返回掩码 `********`，需要明文时显式加 `?reveal=1`，
避免密码被列表接口批量拖走：

```bash
curl http://127.0.0.1:8000/api/credentials/            # password: "********"
curl "http://127.0.0.1:8000/api/credentials/?reveal=1" # 明文
```

每个响应都带 `X-Request-Duration-Ms` 头，耗时同时写入 `inventory.request` 日志。计时中间件
放在 `MIDDLEWARE` 最外层：内层任一中间件短路（`APPEND_SLASH` 的 301、`SecurityMiddleware`
的跳转）都会让更靠后的中间件拿不到这次请求，放外层才真的覆盖"每个请求"。

## 密码轮换

- 实例创建时通过 `post_save` 信号自动生成账号密码，不存在"无密码的实例"。放在信号而非
  视图，是为了让 admin、shell、数据迁移创建的实例同样受约束。
- 密码用 `secrets.token_urlsafe(24)` 生成，Fernet 对称加密后落库，明文不落盘、不进日志。
  解密失败抛 `ValueError` 而不是静默返回空串。
- Celery beat 按 `0 */12 * * *` 触发轮换；`is_due` 判断距上次轮换是否已满 12 小时，
  未到期的实例跳过，因此手动重跑或调度器补跑都不会提前换密码。
- 主密钥取自 `DBI_MASTER_KEY` 环境变量（生产环境必须注入至少 32 字节随机值），
  未设置时回退到 `SECRET_KEY` 的 SHA256 派生值，仅便于本地开箱运行。密钥丢失 =
  存量密码全部无法解密。

## 日统计

`collect_daily_stats` 按 `(stat_date, department, cluster)` 用 `update_or_create` 落库，
联合唯一约束保证重复执行不产生重复行。默认统计**昨天**：任务在 00:00 触发，此刻当天的
实例数还没稳定，刚结束的那一天才是准确的口径。可传 ISO 日期补算历史：

```bash
.venv/bin/python manage.py shell -c "from inventory.tasks import collect_daily_stats; collect_daily_stats.delay('2026-10-15')"
```

## 安全说明

- **接口需认证**。DRF 默认 `AllowAny`，会让任何人匿名拉到实例清单，甚至用 `?reveal=1`
  取到明文密码。已改为 `IsAuthenticated` + Session/Basic 认证。
- **密码默认不回显**，需要明文时显式 `?reveal=1`，且该接口同样需要认证。
- **凭据接口只读**，杜绝客户端写入空密码。
- 主密钥未设 `DBI_MASTER_KEY` 时回退到 `SECRET_KEY` 派生值，仅为本地开箱方便；
  **生产必须注入**独立随机密钥。

## 测试

25 个测试覆盖题目全部 5 个考点，包括加密往返、轮换后新旧明文不同、未到期实例跳过轮换、
重复统计不产生重复行、中间件耗时头、区分可达/不可达的探测结果（用真实 `listen()` 的
socket 验证可达分支，而不是只测失败路径），以及匿名访问被拒、凭据不可写入。

```bash
.venv/bin/python manage.py test inventory
```

各要求的实际运行验证过程与结果见 [VERIFICATION.md](VERIFICATION.md)，其中也如实列出了
未覆盖项（真实 crontab 墙上时钟到点触发、密码真实下发到数据库）。

---

## 附：原始笔试题

### 方案 A：数据库实例管理系统

使用技术栈：python, django, celery

1. 设计一个数据库实例管理系统，用于管理企业内部的数据库实例，包含实例、集群、部门等模型
2. 提供对应模型的增删改查接口；提供一个 API，用于探测某个实例是否 TCP 端口可达
3. 需维护每个实例的账号密码，每隔 12 小时随机轮换每个实例的密码并加密记录
4. 每天 00:00 按部门和集群维度统计实例数量，并把统计数据写入数据库
5. 实现一个中间件，统计每个请求的请求耗时

提交方式：使用 git 提交代码到 github 或者 gitee，提交对应代码仓库的地址。
