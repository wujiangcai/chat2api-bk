# 部署与运维手册

本手册面向单机生产部署。完整 Compose 模板位于 [`deploy/production`](../deploy/production/README.md)，包含 PostgreSQL、Redis、MinIO、API 与 Caddy。

## 1. 上线架构

```text
客户端 / 管理后台
       │ HTTPS
     Caddy
       │
  FastAPI + 静态 Web
   ├─ PostgreSQL：账号、用户、订单、任务、审计
   ├─ Redis：图片任务队列、分布式锁、集中限流
   ├─ MinIO/S3/R2：生成图片和工单附件
   └─ ChatGPT Web：图片生成上游（按全局代理出站）
```

生产环境不要使用 JSON 存储、内存队列或内存限流。Compose 模板已固定 PostgreSQL 与 Redis，并提供健康检查、迁移、备份和上线预检。

## 2. 最短上线流程

```bash
git clone https://github.com/wujiangcai/chat2api-bk.git
cd chat2api-bk/deploy/production
cp .env.production.example .env.production
# 编辑 .env.production，替换所有密码、域名、密钥和对象存储参数
docker compose --env-file .env.production config >/dev/null
docker compose --env-file .env.production up -d --build
docker compose --env-file .env.production exec api sh -lc \
  'python scripts/migrate_database.py --database-url "$DATABASE_URL"'
docker compose --env-file .env.production exec api sh -lc \
  'python scripts/check_production_ready.py'
```

正式开放前执行严格远程验收：

```bash
python ../../scripts/verify_production_deployment.py \
  --base-url https://img.example.com \
  --admin-key '<admin-key>' \
  --image-job \
  --strict-launch \
  --output launch-evidence.json \
  --upload-evidence
```

## 3. 上游账号和代理

1. 登录管理后台。
2. 在设置页配置全局代理，支持 HTTP、HTTPS、SOCKS5、SOCKS5H。
3. 在账号池导入 `access_token`、本地 CPA JSON、远程 CPA 或 `sub2api` 账号。
4. 批量刷新账号，确认状态、账号类型和图片额度正常。
5. 先以单张 `gpt-image-2` 请求做真实冒烟，再开放异步任务。

容器中的 `127.0.0.1` 指向容器自身。如果代理运行在宿主机，应使用 Docker 网桥可达地址、`host.docker.internal`（平台支持时），或把代理放进同一 Compose 网络。代理出口应能同时访问 `chatgpt.com`、OpenAI 静态资源和图片资产地址。

检查代理连通性：

```bash
curl -x http://proxy-host:7897 -I https://chatgpt.com/
docker compose --env-file deploy/production/.env.production exec api \
  python -c "import socket; print(socket.getaddrinfo('chatgpt.com', 443))"
```

## 4. 图片任务关键参数

| 参数 | 建议值 | 说明 |
|---|---:|---|
| `IMAGE_JOB_QUEUE_BACKEND` | `redis` | 多实例共享队列 |
| `IMAGE_JOB_MAX_ATTEMPTS` | `3` | Worker 最大尝试次数 |
| `IMAGE_JOB_RETRY_DELAY_SECONDS` | `10` | 重试间隔 |
| `IMAGE_JOB_STALE_RUNNING_SECONDS` | `900` | 回收卡死任务 |
| `IMAGE_SSE_TIMEOUT_SECONDS` | `180` | ChatGPT Web SSE 总等待上限 |

`IMAGE_SSE_TIMEOUT_SECONDS` 防止上游建立连接后一直不返回 SSE 事件。弱网络可提高至 `240` 或 `300`；不建议设得无限大。超时会中止流并让任务进入既有失败/重试路径。

## 5. 测试门禁

后端：

```bash
python -m unittest discover -v
```

不要使用 `-s test`，否则 Python 可能把 `test/utils.py` 当作顶层 `utils`，遮蔽项目的 `utils` 包。

前端：

```bash
cd web
npm ci
npx eslint .
npx tsc --noEmit
npm run build
```

Compose 配置：

```bash
docker compose --env-file deploy/production/.env.production config >/dev/null
```

## 6. 监控与告警

至少监控：

- `/health/live`、`/health/ready`
- 图片任务成功率、队列积压、dead-letter、运行超时
- 上游账号可用数、失败次数、剩余额度
- PostgreSQL/Redis/对象存储连通性
- 磁盘剩余、备份年龄、对象存储容量
- 邮件投递失败、支付 webhook 失败、客服 SLA 逾期

Prometheus 格式指标：

```bash
curl -H 'Authorization: Bearer <admin-key>' \
  'https://img.example.com/api/admin/metrics?format=prometheus'
```

## 7. 备份与恢复演练

```bash
docker compose --env-file .env.production exec api sh -lc \
  'python scripts/backup_data.py create --database-url "$DATABASE_URL" --json'
docker compose --env-file .env.production exec api sh -lc \
  'python scripts/backup_data.py verify /app/data/backups/<backup>.zip --json'
```

每日备份，复制到异机或独立存储桶；每月至少做一次临时环境恢复演练。备份需覆盖数据库、对象存储清单、关键配置和迁移版本，但不要把明文管理员密钥写进日志或 Git。

## 8. 升级与回滚

升级前：

```bash
git fetch --all --prune
git log --oneline HEAD..origin/main
# 先完成备份和 verify
git pull --ff-only
docker compose --env-file .env.production build --pull api
docker compose --env-file .env.production run --rm api \
  python scripts/migrate_database.py --database-url "$DATABASE_URL"
docker compose --env-file .env.production up -d
```

随后检查健康、迁移状态、管理端和一笔真实图片任务。回滚应用时 checkout 到已知稳定提交并重新构建；数据库发生不兼容迁移时按备份恢复，不要只回滚容器镜像。

## 9. 常见故障

- 图片请求一直运行：检查代理、上游账号和 `IMAGE_SSE_TIMEOUT_SECONDS`；查看 Worker 日志和 dead-letter。
- 全部账号不可用：批量刷新，检查 AT 是否过期、账号是否被自动禁用、代理出口是否变化。
- 资产 URL 404：检查 `OBJECT_STORAGE_PUBLIC_BASE_URL`、bucket 读权限、Caddy/对象存储域名。
- `/health/ready` unhealthy：按响应中的 PostgreSQL、Redis、对象存储或迁移项逐个修复。
- 多副本限流不一致：确认 `RATE_LIMIT_BACKEND=redis` 且所有实例使用同一 Redis。
- 容器连不上宿主机代理：不要使用容器内 `127.0.0.1`，改为宿主机可达地址。
