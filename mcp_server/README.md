# MCP Server

这一目录是 Gene Family MCP 的协议适配层。它只负责：

- 注册 MCP tools。
- 校验 MCP 调用的基本参数。
- 调用 `backend_service` 暴露的 HTTP API。
- 将后端响应转换成 MCP 客户端可使用的结构化结果。

它不直接访问数据库、django-q2、邮箱、PlantCARE 或本地分析程序。

## 当前工具

- `backend_health`
- `get_capabilities`
- `validate_fasta`
- `align_sequences`
- `build_phylogenetic_tree`
- `submit_cis_element_analysis`
- `get_job_status`
- `get_job_result`
- `cancel_job`

## 安装

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r .\mcp_server\requirements.txt
```

## 配置

```powershell
$env:GENE_FAMILY_BACKEND_URL = "http://127.0.0.1:8000/api"
$env:GENE_FAMILY_BACKEND_TIMEOUT = "30"
$env:GENE_FAMILY_BACKEND_TOKEN = "replace-with-a-random-token"
```

## 启动

先启动后端服务，再运行：

```powershell
.\venv\Scripts\python.exe -m mcp_server.server
```

MCP Server 默认使用 `stdio` transport。

`validate_fasta(fasta, alphabet, filename, idempotency_key)` 先通过 API 创建内容寻址的输入 Artifact，再提交 `fasta_validation` django-q2 任务。`alphabet` 支持 `auto`、`dna` 和 `protein`。工具立即返回 `job_id`，结果通过 `get_job_result` 获取。

`align_sequences(artifact_id, strategy, threads, idempotency_key)` 消费校验任务的 `normalized_fasta` Artifact，并提交 MAFFT 异步任务。调用前可通过 `get_capabilities` 确认部署环境中的 MAFFT 状态。

`build_phylogenetic_tree(artifact_id, model, threads, idempotency_key)` 消费 MAFFT 的 `aligned_fasta` Artifact，提交 FastTree 任务并生成 Newick Artifact。
