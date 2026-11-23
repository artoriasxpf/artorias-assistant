# Artorias Search Service

Search Service 是独立 Python 项目。该目录包含 HTTP 服务、分词与索引引擎、
SQLite 数据库管理、容器构建配置和独立测试。它可以从本目录完成构建和安装。

在此目录启动服务：

```bash
export ARTORIAS_SEARCH_TOKEN='your-service-token'
uv sync --locked
uv run --locked artorias-search --host 127.0.0.1 --port 8766
```

也可以运行 `docker compose up --build -d`。容器将索引库保存在独立数据卷，
本地 Python 进程默认使用 `./search-data/search.sqlite`，可用
`ARTORIAS_SEARCH_DB` 修改。允许的集合由 `ARTORIAS_SEARCH_COLLECTIONS` 配置，
默认是 `tasks`。Assistant 与 Search 使用相同的 Bearer 凭证。

API 提供 `/v1/search` 查询、`/v1/events` 应用索引变更、
`/v1/status` 查看版本和消费位置，以及 `/healthz` 和 `/readyz`。
服务数据库存储索引和消费游标。Task 原始数据及 outbox 由 Assistant 管理。

在本目录运行独立测试：

```bash
uv run --locked --extra dev pytest
```
