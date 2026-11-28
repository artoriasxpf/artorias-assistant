# Task 关键词检索与召回方案

> 部署版本更新：默认已切换到独立 HTTP 搜索服务。当前跨进程协议、outbox 同步、
> 重建和部署方式以 [Search 独立部署方案](search_service_deployment.md) 为准。
> 下文中的“同 SQLite 事务”“启动直接构建”描述保留用于 embedded 兼容实现；
> 远端模式仅保证 Task 与 outbox 同事务，索引最终一致，启动不要求服务在线。


本方案依赖 [通用分词与关键词匹配服务](text_search_service.md)，替换现有 Task 候选选择中的中文双字切分与词项交集计数。召回只决定把哪些 Task 交给规划器。它不会替规划器决定 `continue`、`refine`、`followup`，也不改变已关联 Task 的 Step 历史召回。

## Task 索引投影

- 集合使用 `tasks`，分区使用当前 `session_id`，文档 ID 为 Task ID。索引覆盖该 Session 所有纳入召回的 Task；不先截最近 200 条。
- `title` 存 `tasks.goal`；`keywords` 存 `constraints` 与 `criterias` 的值；`body` 存有限长度的 `summary`。如未来有结构化交付归纳，可额外索引已验证的产物名称与路径。
- `body` 最多 12,000 字符，超长文本保留头尾各 5,999 字符，中间用两个换行隔开，避免拼接成不存在的词；不改变 Task/Step 的原始交付。原始 Step 全文、工具输出、审批数据、重复的 `progress` 索引和用户私有文件原文不纳入投影。早期交付细节未进入当前 Task 摘要时，关键词召回可能找不到它；后续可建独立的 Step 成果索引。
- `tasks` 仍是权威来源。查询命中后回表，取最新的目标、状态、长期需求及 `revision`，并再次校验 Session。索引不用于授权和版本校验。

## 索引构建与更新

| 时机 | 操作 |
| --- | --- |
| 首次启用 | 创建 Task 索引，按批扫描 `tasks`，生成投影并写 FTS5。成功后才标记当前索引版本可用。 |
| Task 新建 | 与 Task 和执行计划的写入同事务入队投影事件，worker 异步建立远端索引。 |
| 永久需求变更 | 更新目标、约束、验收标准及索引。临时变更只影响当前 Step。 |
| Step 交付 | Task 的 `summary` 更新后重算投影。用内容指纹跳过相同内容；只改交付计数或时间戳时不用分词。 |
| 删除、归属变化 | 当前产品没有对应业务入口；通用服务已提供分区删除能力。未来新增入口时必须在业务事务内删除旧分区文档，迁移时再写入新文档。若支持归档或隐藏，业务方应定义是否继续参与召回。 |
| 词典或投影规则升级 | 修改版本并从业务数据库重建。支持管理员手动重建和故障恢复。 |

默认远端模式下，Task 与 outbox 同事务提交；服务不可用不阻止业务提交。
后台 worker 按集合内单调序号同步，服务端在同一事务中更新索引与消费位置，支持幂等重试。
不能只用 `revision` 排序，因为交付更新摘要而不修改需求版本。
本地 embedded 兼容模式仍使用同库事务。完整恢复及快照重建协议见
[独立部署方案](search_service_deployment.md)。

## 查询与融合流程

1. **提取显式引用。** 用户输入有完整 Task ID 时，在当前 Session 验证并按出现顺序加入候选。显式 ID 扫描独立于全文搜索。
2. **召回关键词。** 把用户输入前 30,000 字符交给分词服务，在当前 Session 分区运行 BM25，取前 10 条，按相关度排列；显式 ID 仍从完整输入提取。仅有停用词或无命中时跳过这一路。首版不额外调用 LLM 做搜索改写。
3. **补近期任务。** 按 `updated_at` 降序补入最多 8 个 Task。这样「继续刚才那个」一类缺少关键词的输入也有候选。
4. **合并与回表。** 优先顺序为显式引用、关键词命中、近期候选。按 Task ID 去重，候选总数最多 20。远端关键词查询在业务事务外完成，随后开启业务事务回表，过滤已删除或不属于当前 Session 的 Task。候选摘要沿用末尾 2,000 字符预览，长期需求保持完整，Planner 保留原有整体字节预算检查。
5. **交给规划器。** 传最新需求与 `recall_sources` 列表（`explicit`、`keyword`、`recent`），重复命中时保留多个来源。分数只负责候选排序。规划器可以返回新任务、继续、细化、后续任务或澄清；提示词明确候选命中不等于语义关联，安装计划时保留当前的候选与 `revision` 校验。

**远端降级范围：** 网络故障、服务 5xx/限流及索引不可用时降级为显式引用和近期任务；
认证、授权、参数和协议错误直接抛出。业务 Task 通过 outbox 持续提交，后台恢复后重放。
回表仍校验 Session 和需求版本。embedded 模式仅对已知 `IndexUnavailable` 降级，
数据库锁超时、磁盘错误等继续抛出。生产监控需区分“无命中”和“搜索降级”。

## 代码与运行方式

| 模块 | 已实现职责 |
| --- | --- |
| `services/search/src/artorias_search_service/engine/` | 通用分词、协议、SQLite 索引、版本与批量增删改。 |
| `src/artorias_server/integrations/search/tasks/` | Task 文档投影、分批全量重建、候选融合和回表。 |
| `tasks/repository.py` | 初始化索引，在新建/永久需求变更/交付事务内更新；提供 `rebuild_search()`。 |
| `tasks/executor.py` | `TaskRuntime(..., text_index=...)` 注入通用依赖；Step 上下文不变。 |
| `src/artorias_local_app/bootstrap.py` | 默认组装 HTTP/outbox 适配器并启动同步 worker；embedded 需显式配置。 |
| `scripts/rebuild_task_search.py` | 从指定数据目录的 `app.sqlite` 重建全部 Task 索引。 |

依赖已加入 `pyproject.toml` 与 `uv.lock`。先按独立部署方案启动搜索服务并配置 URL/Token。assistant 启动后入队首次快照，由同步 worker 完成远端构建，不需要清空业务数据：

```bash
uv sync --locked
uv run artorias-assistant
```

手动重建（只更新检索投影，不调用模型、不扫描用户文件）：

```bash
uv run python scripts/rebuild_task_search.py --data ./data
```

`--data` 可省略，此时读取 `.env`/环境变量中的 `ARTORIAS_DATA`，默认 `./data`。命令要求数据库已经存在；重建针对整个 `tasks` 集合，其他检索集合不受影响。全量快照使用 200 条一批读取权威 Task，当前仍组装成一次请求；远端原子重建成功后才更新版本与消费位置，失败保留 outbox 事件供重试。大量数据建议在低负载时运行，以避免写锁等待。

## 落地改动与验收

- 已接入并删除旧双字切分/交集计数模块。`tests/test_task_search.py` 覆盖 200 条以外旧 Task、引用顺序、渠道融合、跨 Session、临时/永久需求变更、摘要更新、失败回滚、启动构建、版本升级、手动重建与降级恢复；原有 Task/Step 测试继续验证上下文与需求修订行为。
- 后续可用带人工关联标签的真实任务集比较 Recall@10、近期兜底占比和规划器误关联率，再调整字段权重和渠道配额。词级匹配仍不保证同义改写或语义等价查询能命中。
