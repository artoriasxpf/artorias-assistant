# Search 独立部署方案

## 拆分决策（实施前方案）

搜索服务独立进程、独立 SQLite 数据目录，不挂载 assistant 的 app.sqlite。
保留 artorias_search 的分词与 FTS5 实现，增加 FastAPI HTTP 边界；assistant
通过 artorias_search_client 调用，不加载 jieba。进程内实现仅保留为测试和显式兼容模式。

业务数据仍由 business_tasks 管理。原本的跨表本地事务改成：业务写入与 outbox
同事务；同步 worker 提交 HTTP 请求；服务端索引与消费位置同事务；收到确认后删除
本地事件。网络调用不在业务数据库事务内。允许短暂索引延迟，不承诺跨服务强一致。

## 数据与协议

- 业务库 search_sync_state：collection 主键、source_id（数据库初始化生成并持久化）、
  projection_version、next_seq、created_at、updated_at。
- 业务库 search_outbox：collection、seq（集合内单调递增，联合主键）、payload、
  created_at、updated_at。payload 为完整投影快照或单文档 upsert/delete。
- 服务库保留 text_search_*，增加 search_consumers：collection 主键、source_id、last_seq、
  created_at、updated_at。每个集合绑定一个权威生产者，防止不同 assistant 数据库互相覆盖。
- Bearer 服务凭证只授予配置的集合；本版属于可信后端之间的内部 API。session 分区由
  assistant 的授权链路产生，不允许将该接口直接暴露给终端用户。跨网络使用 TLS/反向代理。
- POST /v1/events：source_id、seq、collection、projection_version、operation、文档；
  普通事件必须连续，已消费事件直接确认。快照可以跨过旧序号，因为包含全部权威数据。
- POST /v1/search：collection、partition、query、projection_version、mode、limit。
- GET /healthz：存活；GET /readyz：数据库与 FTS 就绪；POST /v1/status：版本与消费位置。

## 初始化、更新、恢复

首次初始化只在业务库创建 outbox 并入队全量快照，不依赖服务可达。
日常变更仅入队该 Task 的有限检索字段；相同内容由服务端指纹跳过分词。
同步 worker 严格从最小序号投递。重试不越过失败事件，退避有上限；多个 worker
重复投递同一事件也安全。服务端按消费序号拒绝缺口，防止后来的修改被旧事件覆盖。

投影版本变化、服务端分词版本变化或索引丢失时，在业务事务中读取所有 Task，
清除已经被快照覆盖的待发事件，入队新的全量快照；后续修改在该快照之后排队。
服务端在一个事务中替换集合并更新消费位置，失败回滚，读者不会看见半个索引。
本版全量快照一次传输，适合当前 SQLite 规模；大型集合后续扩展为分批暂存、原子切换。

查询在业务事务外执行 HTTP，随后在业务库回表复核 Session 和最新需求。
网络故障、服务不可用、索引版本不符时降级为显式引用与近期任务；认证、非法请求等
配置/协议错误不能伪装成无命中。查询不负责同步，最近任务兜底不等同于 read-your-writes。

## 实施顺序与验证

1. HTTP 服务与持久化消费去重；2. 客户端与 outbox；3. Task 接口及应用生命周期接入；
4. 部署、重建命令与迁移说明；5. 验证重复投递、乱序、断网、回滚、版本恢复和隔离。

旧业务库中的 text_search_* 不再使用，也不自动删除；可保留作回滚备份。
业务库备份恢复后，应同步恢复服务库，或清空独立服务数据后全量重建；不要把历史
业务库恢复到仍保有未来消费游标的服务。源数据库身份变更需管理员明确迁移，不能自动接管集合。

## 实现映射

| 模块 | 职责 |
| --- | --- |
| `artorias_search_service/app.py` | Bearer 鉴权、集合白名单、API 校验、独立数据库事务、消费游标 |
| `artorias_search_service/main.py` | `artorias-search` 启动入口 |
| `artorias_search_client/client.py` | HTTP 连接池、超时、错误分类；不使用环境代理 |
| `artorias_search_client/outbox.py` | 本地事务内的投影事件写入；不执行网络请求 |
| `artorias_server/tasks/search.py` | 生成 Task 投影、快照、远端召回后回表与候选融合 |
| `artorias_server/tasks/search_sync.py` | 按序消费 outbox、服务版本检查、自动重建、退避重试 |
| `artorias_local_app/bootstrap.py` | 默认远端模式，生命周期内启动同步 worker 并关闭连接池 |
| `scripts/sync_task_search.py` | 云端可独立运行的同步进程 |
| `scripts/rebuild_task_search.py` | 入队快照并等待同步完成，失败保留事件且退出非零 |

`search_outbox` 的实际主键为 `(collection, seq)`；`search_sync_state.next_seq` 在同一事务中
递增，提供集合内单调序号（不是 Task.revision，也不是全库共用自增号）。
服务端消费游标与索引写入同事务。outbox 行确认后删除，消费游标长期保留。

同步间隔默认为 1 秒，错误退避最多 60 秒。当前每次投递一个事件，HTTP 各阶段默认超时 3 秒，可用 `ARTORIAS_SEARCH_TIMEOUT` 调整；大型快照应配置更长超时。
停机等待正在运行的同步调用结束，不把 asyncio 取消误当成已取消线程中的 HTTP 请求。
监控至少包含 outbox 条数、最老事件时间、同步错误日志和服务 readyz；持续失败不会自动丢弃事件。

## 部署与运行

推荐独立容器：从仓库根目录运行（密钥需自行生成并在两端一致设置）：

```bash
export ARTORIAS_SEARCH_TOKEN='replace-with-your-service-secret'
docker compose -f services/search/compose.yaml up -d --build
uv sync --locked
export ARTORIAS_SEARCH_MODE=remote
export ARTORIAS_SEARCH_URL=http://127.0.0.1:8766
uv run artorias-assistant
```

镜像只安装 FastAPI、Uvicorn、jieba 并复制 `artorias_search`、`artorias_search_service`，不安装 LLM、runtime 或 device。
容器使用独立持久化卷和非 root 用户，默认仅映射主机 loopback 端口。生产跨主机部署使用
内网服务地址和 TLS，并配置密钥管理。当前 SQLite 服务采用单副本持久化卷，不将多个
不共享数据库的副本挂到负载均衡后；横向扩容需迁移存储实现。

不用 Docker 时，可在独立终端运行：

```bash
uv sync --locked --extra search
export ARTORIAS_SEARCH_TOKEN='replace-with-your-service-secret'
export ARTORIAS_SEARCH_DB=./search-data/search.sqlite
uv run --extra search artorias-search --host 127.0.0.1 --port 8766
```

搜索入口读取环境变量，不隐式读取 assistant 的 `.env`。assistant 仍会读取 `.env`。
`ARTORIAS_SEARCH_COLLECTIONS` 默认 `tasks`；一个集合绑定一个业务数据库的 source_id。
当前 Task 集合固定为 tasks，因此多个独立 assistant 数据库应使用独立服务实例/数据卷。
同一权威业务数据库的多 runtime 可以共用同一服务。

云端自定义 application factory 可复用 `TaskRuntime` 的默认远端适配，同时运行同步进程：

```bash
uv run python scripts/sync_task_search.py --data ./data
# 或仅处理当前积压，失败退出非零
uv run python scripts/sync_task_search.py --data ./data --once
uv run python scripts/rebuild_task_search.py --data ./data
```

本地启动器已自动运行同步 worker，无需再启动脚本。多个 worker 的重复投递由服务端
幂等处理，但没有提高同一集合的写入吞吐，建议一个业务库部署一个同步 worker。

## 迁移、回滚与限制

1. 备份 app.sqlite；先部署空的独立搜索服务并配置共同凭证。
2. 启动新 assistant，自动建立 outbox 并入队全量快照；旧 text_search_* 表保留但不再消费。
3. 等待 outbox 清空，再验证历史 Task 的关键词召回。Task 需求和 Step 历史不迁移、不丢弃。
4. 故障可先恢复服务让 outbox 重放。需要回到嵌入式模式时，安装 search extra，设置
   `ARTORIAS_SEARCH_MODE=embedded`，执行手动重建再启动，避免误用旧表的过时索引。
5. 再切回远端模式时，同样执行手动重建，覆盖嵌入式期间未进入 outbox 的变更。

保持一个 Token 对应一个可信服务调用方/允许集合，当前不提供面向终端用户的多租户 ACL。
不提供分布式事务、同步写后立刻可查、超大集合分片重建或无限 outbox 容量承诺。
索引断网后恢复不需要回滚已成功提交的 Task。全文快照的生成/传输仍有内存开销，
大规模上线前应增加流式暂存与分批提交协议。
