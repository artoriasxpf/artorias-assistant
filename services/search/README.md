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

## Admin 调试台

打开 `http://127.0.0.1:8766/admin`，输入服务的 `ARTORIAS_SEARCH_TOKEN`。
页面提供 JSON 请求编辑器、示例和完整 HTTP 状态及响应。所有请求沿用服务的 Bearer 认证，
令牌只保留在当前页面的输入框中。

建议先在服务的启动环境设置 `ARTORIAS_SEARCH_COLLECTIONS=tasks,tasks_sandbox` 并重启。
在「索引事件」接口载入并发送「初始化快照」「新增文档」「更新文档」，然后切到「搜索」
发送示例请求。首次初始化使用序号 1，后续使用集合状态接口返回的 `last_seq + 1`。
调试现有 Task 搜索时，将查询 JSON 的集合、分区及投影版本改为实际值。

在线 Assistant 管理 `tasks` 集合的事件序号。手工变更这个集合会影响 outbox 同步，
而且只更新搜索投影。测试写入请使用测试集合或单独的索引数据库。

在本目录运行独立测试：

```bash
uv run --locked --extra dev pytest
```
