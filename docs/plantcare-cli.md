# PlantCARE 独立调试脚本

这些脚本位于 `backend_service/scripts/`，用于单独诊断 PlantCARE 提交与邮件回收，不属于 MCP 对外接口。

## 文件

- `backend_service/scripts/plantcare_submit.py`：仅提交任务。
- `backend_service/scripts/plantcare_poll.py`：提交任务、轮询邮箱并下载附件。
- `backend_service/tests/fixtures/test.fa`：默认测试输入。

## 仅提交

```powershell
.\venv\Scripts\python.exe .\backend_service\scripts\plantcare_submit.py `
  --email your-email@qq.com `
  --ref myref123 `
  --file .\backend_service\tests\fixtures\test.fa
```

也可以直接传入序列：

```powershell
.\venv\Scripts\python.exe .\backend_service\scripts\plantcare_submit.py `
  --email your-email@qq.com `
  --ref myref123 `
  --sequence ACTGACTGACTG
```

加入 `--dry-run` 可以只检查请求构造，不向 PlantCARE 发送任务。

## 提交并轮询邮箱

```powershell
.\venv\Scripts\python.exe .\backend_service\scripts\plantcare_poll.py `
  --email your-email@qq.com `
  --password your-imap-auth-code `
  --imap-host imap.qq.com `
  --poll-interval 10 `
  --max-polls 360
```

脚本默认读取 `backend_service/tests/fixtures/test.fa`，自动生成随机 `ref`，并将命中的邮件附件保存到 `plantcare_attachments_<ref>`。

## 主要参数

- `--email`：接收结果的邮箱地址。
- `--password`：IMAP 授权码，也可通过 `MAIL_PASSWORD` 环境变量传入。
- `--imap-host`：IMAP 主机，例如 `imap.qq.com`。
- `--port`：IMAP SSL 端口，默认 `993`。
- `--folder`：邮箱文件夹，默认 `INBOX`。
- `--ref`：任务标识；不传时自动生成。
- `--sequence`：直接提交 DNA 序列。
- `--file`：提交 FASTA 文件。
- `--poll-interval`：邮件检查间隔，默认 10 秒。
- `--max-polls`：最多检查次数。
- `--save-attachments-dir`：附件输出目录。

## 安全说明

- 使用邮箱授权码，不要使用网页登录密码。
- 不要把授权码写入脚本、README 或 Git。
- 这些脚本包含长时间轮询，只适合人工诊断；正式后端应由 scheduler 进行单次邮箱检查。
