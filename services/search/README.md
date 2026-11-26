# Artorias Search Service

Search Service 是独立 Python 项目。该目录包含 HTTP 服务、分词与索引引擎、
SQLite 数据库管理、容器构建配置和独立测试。它可以从本目录完成构建和安装。

## 本地启动（macOS / Linux）

需要 Python 3.11+ 和 uv。首次配置时，在仓库根目录执行以下命令：

```bash
test -f .env || cp .env.example .env
openssl rand -hex 32
```

将生成的随机值填入根目录 `.env` 中的 `ARTORIAS_SEARCH_TOKEN`。如果已经设置了令牌，继续使用原值。Assistant 与 Search 使用相同的令牌。

从仓库根目录进入本目录，启动 Search Service：

```bash
cd services/search
uv run --locked --env-file ../../.env artorias-search --host 127.0.0.1 --port 8766
```

首次运行时 uv 会安装本服务的依赖。只调试 Search Service 时，可以在 `services/search/` 目录设置 `export ARTORIAS_SEARCH_TOKEN='your-service-token'`，然后执行上面的启动命令，省略 `--env-file ../../.env`。Search Service 不需要模型 API 密钥。

与 Assistant 联调时，保持 Search 进程运行，再在另一个终端按[项目根目录 README](../../README.md)启动 Assistant。

## 使用 Docker Compose 启动

配置好仓库根目录的 `.env` 后，从 `services/search/` 目录执行：

```bash
docker compose --env-file ../../.env up --build -d
docker compose --env-file ../../.env logs -f search
```

Compose 将服务映射到 `127.0.0.1:8766`，索引库保存在 `search-data` 数据卷。使用 uv 启动时，索引库默认是 `services/search/search-data/search.sqlite`，可用 `ARTORIAS_SEARCH_DB` 修改。两种启动方式默认都使用 8766 端口，选一种运行。

API 提供 `/v1/search` 查询、`/v1/events` 应用索引变更、
`/v1/status` 查看版本和消费位置，以及 `/healthz` 和 `/readyz`。
服务数据库存储索引和消费游标。Task 原始数据及 outbox 由 Assistant 管理。

## Admin 调试台

Admin 随 Search Service 一起启动。服务监听后，打开 [Admin 调试台](http://127.0.0.1:8766/admin)，输入 `ARTORIAS_SEARCH_TOKEN`，选择「就绪检查」并发送请求。收到 HTTP 200 和 `{"status":"ready"}`，表示服务可读取索引。收到 HTTP 401 时，检查输入的令牌是否与服务启动时读取的值一致。

页面提供 JSON 请求编辑器、示例和完整 HTTP 状态及响应。所有请求沿用服务的 Bearer 认证，令牌只保留在当前页面的输入框中。

默认允许 `tasks` 集合。要运行页面的写入示例，在根目录 `.env` 中设置 `ARTORIAS_SEARCH_COLLECTIONS=tasks,tasks_sandbox`，然后重新运行对应的启动命令。若直接用环境变量启动，在终端导出同一设置后重启。
在「索引事件」接口载入并发送「初始化快照」「新增文档」「更新文档」，然后切到「搜索」
发送示例请求。首次初始化使用序号 1，后续使用集合状态接口返回的 `last_seq + 1`。
调试现有 Task 搜索时，将查询 JSON 的集合、分区及投影版本改为实际值。

在线 Assistant 管理 `tasks` 集合的事件序号。手工变更这个集合会影响 outbox 同步，
而且只更新搜索投影。测试写入请使用测试集合或单独的索引数据库。

## 测试

在本目录运行：

```bash
uv run --locked --extra dev pytest
```
