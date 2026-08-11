# Backend Service

Gene Family MCP 的 API 与任务执行后端，基于 Django、Django Ninja 和 django-q2。

后端负责：

- 对外提供 HTTP API。
- 校验分析输入并创建异步任务。
- 按 SHA-256 保存和复用 FASTA 输入，生成规范化 FASTA 与校验摘要。
- 使用 MAFFT 消费规范化 FASTA Artifact，并保存对齐结果与工具溯源。
- 使用 FastTree 消费 aligned FASTA，并生成校验后的 Newick 树。
- 以父子业务任务和 django-q2 Schedule 编排可恢复的序列系统发育工作流。
- 调用 PlantCARE 等远程服务或本地生信程序。
- 轮询外部结果并保存分析产物。
- 管理任务状态、错误和结果。

后端不包含 HTML 页面或前端资源。MCP Server 通过 HTTP API 调用本服务。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/core/health` | 健康检查 |
| `GET` | `/api/core/capabilities` | 查询分析能力和队列后端 |
| `POST` | `/api/inputs/fasta` | 创建内容寻址的 FASTA 输入 Artifact |
| `GET` | `/api/inputs/{input_artifact_id}/download` | 下载输入 Artifact |
| `POST` | `/api/jobs` | 创建业务任务 |
| `GET` | `/api/jobs/{job_id}` | 查询任务状态 |
| `GET` | `/api/jobs/{job_id}/events` | 查询任务事件 |
| `GET` | `/api/jobs/{job_id}/result` | 获取结果和产物清单 |
| `POST` | `/api/jobs/{job_id}/cancel` | 取消任务 |
| `GET` | `/api/artifacts/{artifact_id}/download` | 下载分析产物 |
| `POST` | `/api/cis-elements/submit` | 提交顺式元件预测 |
| `GET` | `/api/cis-elements/tasks/{task_id}` | 查询任务状态或结果 |
| `GET` | `/api/docs` | OpenAPI 文档 |

## 安装

在仓库根目录执行：

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r .\backend_service\requirements.txt
```

## 配置 PlantCARE

```powershell
$env:PLANTCARE_EMAIL = "your-email@qq.com"
$env:PLANTCARE_AUTH_CODE = "your-imap-auth-code"
$env:PLANTCARE_IMAP_HOST = "imap.qq.com"
$env:BACKEND_API_TOKEN = "replace-with-a-random-token"
```

配置 `BACKEND_API_TOKEN` 后，除健康检查和 OpenAPI 文档之外的 `/api/` 接口都要求 `Authorization: Bearer <token>`。未配置时仅适合本地开发。创建任务可携带 `Idempotency-Key` 请求头，避免客户端重试导致重复分析。

## 启动 API

```powershell
Set-Location .\backend_service
..\venv\Scripts\python.exe manage.py migrate
..\venv\Scripts\python.exe manage.py runserver
```

## 启动任务 Worker

另开一个终端：

```powershell
Set-Location .\backend_service
..\venv\Scripts\python.exe manage.py qcluster
```

django-q2 承担执行队列和外部结果调度；对外任务 ID、状态、事件和产物由 `jobs` app 持久化。PlantCARE 提交任务会快速进入 `waiting_external`，由数据库迁移自动创建的 django-q2 Schedule 每分钟批量检查邮箱，不会长期占用提交 worker。

`fasta_validation` 是本地异步任务。API 不把大段 FASTA 写入任务参数，而是先写入共享 Artifact 存储并在数据库保存 SHA-256 清单；worker 在读取时再次检查文件大小和校验和。可通过 `.env.example` 中的 `MAX_FASTA_*` 与 `DATA_UPLOAD_MAX_MEMORY_SIZE` 配置输入上限。

`multiple_sequence_alignment` 只接受成功任务产生的 `normalized_fasta` Artifact。MAFFT 的可执行文件、超时、线程和输出上限由 `MAFFT_*`、`MAX_TOOL_THREADS` 与 `MAX_ALIGNMENT_OUTPUT_BYTES` 配置。能力接口会真实探测可执行文件；缺失时任务以 `CAPABILITY_UNAVAILABLE` 失败。

`phylogenetic_tree` 只接受成功任务产生的 `aligned_fasta` Artifact，且至少需要三条序列。FastTree 输出会检查 Newick 语法、叶数、唯一标签、分支长度和尾随内容；原始服务器路径与 stderr 不会进入公开错误响应。

`sequence_phylogeny` 是高层父任务，依次创建 `fasta_validation`、`multiple_sequence_alignment` 和 `phylogenetic_tree` 子任务。`parent_job + workflow_step` 唯一约束和编排租约防止重复推进；子任务失败和父任务取消都会传播。
