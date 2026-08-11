# MCP Server

这一目录是 Gene Family MCP 的协议适配层。它只负责：

- 注册 MCP tools。
- 校验 MCP 调用的基本参数。
- 调用 `backend_service` 暴露的 HTTP API。
- 将后端响应转换成 MCP 客户端可使用的结构化结果。

它不直接访问数据库、django-q2、邮箱、PlantCARE 或本地分析程序。

## 当前工具

- `backend_health`
- `submit_cis_element_analysis`
- `get_cis_element_task`

## 安装

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r .\mcp_server\requirements.txt
```

## 配置

```powershell
$env:GENE_FAMILY_BACKEND_URL = "http://127.0.0.1:8000/api"
$env:GENE_FAMILY_BACKEND_TIMEOUT = "30"
```

## 启动

先启动后端服务，再运行：

```powershell
.\venv\Scripts\python.exe -m mcp_server.server
```

MCP Server 默认使用 `stdio` transport。
