# Backend Service

Gene Family MCP 的 API 与任务执行后端，基于 Django、Django Ninja 和 django-q2。

后端负责：

- 对外提供 HTTP API。
- 校验分析输入并创建异步任务。
- 调用 PlantCARE 等远程服务或本地生信程序。
- 轮询外部结果并保存分析产物。
- 管理任务状态、错误和结果。

后端不包含 HTML 页面或前端资源。MCP Server 通过 HTTP API 调用本服务。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/core/health` | 健康检查 |
| `GET` | `/api/core/capabilities` | 查询分析能力和队列后端 |
| `POST` | `/api/jobs` | 创建业务任务 |
| `GET` | `/api/jobs/{job_id}` | 查询任务状态 |
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
```

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
