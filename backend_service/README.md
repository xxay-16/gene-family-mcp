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

当前 PlantCARE 的长轮询与任务超时仍需按架构文档继续重构。
