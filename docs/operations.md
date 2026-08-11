# 运行与部署手册

## 1. 进程模型

生产环境至少运行三个进程：

- PostgreSQL：业务任务、django-q2 ORM 队列和 Schedule。
- Backend API：Gunicorn 承载 Django Ninja API。
- Backend Worker：`manage.py qcluster`，同时处理普通任务和 django-q2 Schedule。

MCP Server 通常由 MCP 客户端以 `stdio` 子进程启动，也可以通过 Docker 运行。API 和 worker 必须共享数据库及 Artifact 存储。

## 2. Docker Compose

复制环境模板：

```powershell
Copy-Item .env.example .env
```

至少设置：

```dotenv
POSTGRES_PASSWORD=replace-with-a-strong-password
DJANGO_SECRET_KEY=replace-with-a-long-random-secret
BACKEND_API_TOKEN=replace-with-a-random-api-token
PLANTCARE_EMAIL=your-email@example.com
PLANTCARE_AUTH_CODE=your-imap-auth-code
```

启动后端：

```powershell
docker compose up -d --build postgres api worker
docker compose ps
```

查看日志：

```powershell
docker compose logs -f api worker
```

运行 MCP stdio 容器：

```powershell
docker compose --profile mcp run --rm -T mcp
```

MCP 客户端应以保持标准输入输出连接的方式启动该命令。

## 3. 健康检查

- `GET /api/core/health`：进程存活，不访问数据库。
- `GET /api/core/ready`：验证数据库连接和外部结果 Schedule。

Compose 使用 readiness 端点判断 API 是否可服务。

Compose 在容器网络内使用 HTTP；对公网部署时应由反向代理或负载均衡器终止 TLS，并设置 `DJANGO_TRUST_PROXY_SSL_HEADER=true`。若 Django 直接终止 HTTPS，则启用 `DJANGO_SECURE_SSL_REDIRECT=true`。

## 4. 数据迁移

API 容器入口会在启动前执行幂等的：

```bash
python manage.py migrate --noinput
```

worker 等待 API readiness 成功后启动，并设置 `RUN_MIGRATIONS=false`，避免多个进程并发迁移。发布前仍应在 CI 或预发布环境执行：

```powershell
python backend_service/manage.py makemigrations --check --dry-run
```

## 5. Artifact

Compose 将 `/data/artifacts` 挂载到 `artifact-data` volume。输入和输出 Artifact 共用该卷；数据库中只保存相对路径、SHA-256、MIME、大小和元数据。FASTA 输入保存在 `inputs/<sha256>.fasta`，同内容不会重复占用存储。

备份时必须同时备份：

1. PostgreSQL 数据库。
2. `artifact-data` volume。

只恢复其中一方会造成任务记录与文件不一致。

## 6. django-q2

- ORM 队列名称：`gene_family_backend`。
- 普通任务超时默认 300 秒；ORM 重试锁默认 1890 秒，严格大于 MAFFT 的 1830 秒 django-q2 task timeout，避免长任务被重复领取。
- `max_attempts=1`，业务失败由 `AnalysisJob` 记录并显式处理。
- `MAX_SEQUENCE_LENGTH` 和 `MAX_ACTIVE_JOBS` 提供基础资源保护。
- `MAX_FASTA_INPUT_BYTES`、`MAX_FASTA_RECORDS`、`MAX_FASTA_SEQUENCE_LENGTH`、`MAX_FASTA_TOTAL_RESIDUES` 和 `MAX_FASTA_HEADER_LENGTH` 限制 FASTA 资源。
- `MAFFT_TIMEOUT`、`MAX_TOOL_THREADS` 和 `MAX_ALIGNMENT_OUTPUT_BYTES` 限制多序列比对；django-q2 task timeout 会比 MAFFT subprocess timeout 多 30 秒用于安全收尾。
- `FASTTREE_TIMEOUT`、`FASTTREE_DEFAULT_THREADS` 和 `MAX_TREE_OUTPUT_BYTES` 限制系统发育树推断。
- `Q_CLUSTER_RETRY` 必须严格大于普通与 MAFFT task timeout；配置不安全时后端会拒绝启动。
- PlantCARE 外部等待不占用 worker。
- 迁移自动创建 `gene-family-poll-external-results` Schedule，每分钟批量检查邮箱。
- 迁移自动创建 `gene-family-advance-workflows` Schedule，每分钟推进已满足依赖的工作流父任务。
- 任务与轮询均使用租约，避免多 worker 重复处理；过期运行租约会被标记为 `WORKER_LEASE_EXPIRED`。

## 7. 日志

后端输出 JSON 日志到标准输出，包括：

- UTC 时间。
- 日志级别与 logger。
- `X-Request-ID`。
- HTTP 方法、路径、状态码和耗时。

调用方可以传 `X-Request-ID`；未传时后端自动生成，并在响应头返回。不要在日志中记录邮箱授权码、Bearer Token、完整邮件正文或序列内容。

## 8. 发布检查

```powershell
.\scripts\check.ps1
```

门禁包括：

- Django system check。
- 迁移漂移检查。
- Ruff lint。
- 后端与 MCP 自动化测试。
- 分支覆盖率，最低 75%。
- Python 编译。
- Git diff whitespace 检查。

## 9. 故障排查

### API ready 返回 503

检查 PostgreSQL 连接以及 Schedule 是否存在：

```powershell
docker compose exec api python manage.py shell -c "from django_q.models import Schedule; print(list(Schedule.objects.values()))"
```

### 任务一直 queued

检查 worker：

```powershell
docker compose ps worker
docker compose logs --tail 200 worker
```

### 任务一直 waiting_external

检查邮箱配置、IMAP 网络、Schedule 日志以及 `external_deadline`。临时 IMAP 错误会保留等待状态并在下一轮重试。

### 任务一直 waiting_dependency

查询父任务 `workflow_steps`，再检查对应子任务状态、worker 日志和 `gene-family-advance-workflows` Schedule。子任务成功后最迟在下一次一分钟调度中推进。

### Artifact 下载 404

检查数据库 Artifact 记录和共享 volume 是否同时存在，不要手工修改 `storage_path`。

### MAFFT capability unavailable

生产 Docker 镜像已安装 MAFFT。本机运行时确认 `MAFFT_EXECUTABLE` 指向可执行文件，并访问 `/api/core/capabilities` 查看探测结果。更改可执行文件配置后需要重启 API 和 worker。

### FastTree capability unavailable

生产 Docker 镜像已安装 FastTree。本机运行时确认 `FASTTREE_EXECUTABLE` 指向 FastTree 或 FastTreeMP 可执行文件；OpenMP 线程数由任务参数通过 `OMP_NUM_THREADS` 设置。
