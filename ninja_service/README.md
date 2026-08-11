# ninja_service

基于 Django + Django Ninja + django-q2 的服务骨架，默认使用 SQLite。

## 运行

```bash
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\python.exe manage.py migrate
venv\Scripts\python.exe manage.py runserver
```

## 启动队列

```bash
venv\Scripts\python.exe manage.py qcluster
```

## API

- `GET /api/core/health`
- `POST /api/core/tasks`
- `POST /api/cis-elements/submit`
- `GET /api/cis-elements/tasks/{task_id}`

## 顺式作用元件预测页面

- 页面地址：`/cis-elements/`
- 只需提交序列，前端会自动轮询任务状态直到返回结果
- 任务依赖 `django-q2`，请确保已启动 `qcluster`

## PlantCARE 邮箱配置

在启动前设置环境变量：

```bash
set PLANTCARE_EMAIL=你的测试邮箱
set PLANTCARE_AUTH_CODE=你的邮箱授权码
```
