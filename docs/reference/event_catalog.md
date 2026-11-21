# 本地事件清单

本表以当前代码为准。事件保存在 `app.sqlite` 的 `events` 表，经 `GET /api/sessions/{sid}/events?after=<seq>` 重放和实时发送。每条事件的外层字段为 `seq`（全库递增序号）、`session`、`run`（可为 null，例如空间切换）、`kind`、`payload`（SSE 中为对象）、`created_at`、`updated_at`（UTC 时间）。SSE 的 `id` 等于 `seq`。心跳是 SSE 注释，不写数据库。旧记录以写入时的结构为准。

| Event | 写入时机和来源 | `payload` 字段 |
| --- | --- | --- |
| `message.accepted` | 提交用户消息时，和 Run 入队一起写入 | `text` |
| `run.started` | 调度器开始或恢复运行；同一 Run 可重复出现 | 空对象 |
| `run.completed` | 调度器成功结束 Run | 空对象 |
| `run.failed` | 调度器捕获运行异常 | `error`（最多 1000 字符） |
| `run.cancelled` | 用户取消等待、排队或正在执行的 Run | 空对象 |
| `run.interrupted` | 启动恢复时发现上次仍处于运行中的 Run | `reason` |
| `task.plan` | 首次生成并持久化本轮任务计划 | `kind`（`chat` / `tasks` / `clarify`）、`question`、`items`（见下文） |
| `task.started` | 首次执行某个 Task 步骤 | `task_id`、`key`（计划项的键） |
| `task.resumed` | 恢复某个 Task 步骤（审批后继续或中断后续跑） | `task_id`、`key`、`reason`（`approval` / `interrupted`） |
| `task.delivered` | 保存任务交付结果后 | `task_id`、`text`、`status`（`needs_review`） |
| `task.failed` | 任务步骤执行抛出非取消异常，随后 Run 失败 | `task_id`、`key`、`reason`（最多 1000 字符） |
| `task.interrupted` | 任务步骤因取消而中断，随后 Run 取消 | `task_id`、`key`、`reason`（`cancelled`） |
| `task.revised` | 首次生成计划时，需求补丁实际生效并入库 | `task_id`、`key`、`base_revision`、`applied_revision`、`scope` |
| `assistant.delta` | 运行时接收到模型正文片段 | `text`；任务模式另有 `task_id` |
| `assistant.final` | 本轮最终回复、澄清问题或各任务结果拼接完成 | `text`（可能为空） |
| `model.started` | 开始等待一次规划或 Agent 模型请求 | `request_id`、`stage`（`planner` / `agent`）；任务模式另有 `task_id` |
| `model.finished` | 同一次模型请求结束；如果流提前关闭，由 Run 终态结束加载提示 | 字段与对应的 `model.started` 相同 |
| `tool.call` | 运行时从完整模型消息读取工具调用，每次调用写一条 | `call_id`、`name`、`args`；任务模式另有 `task_id` |
| `tool.result` | 工具消息进入图状态；工具失败也以 `status='error'` 的结果返回，不中断运行 | 普通结果为 `call_id`、`name`、`status`、`content`；任务模式另有 `task_id`。Python 详细结果字段见下文 |
| `approval.requested` | 图暂停等待审批，同步记录等待状态 | `requests`（审批对象列表，见下文）；任务模式另有 `task_id` |
| `approval.decided` | 接受并持久化用户的批准或拒绝 | `decision`（`approve` / `reject`）、`task_id`、`request_seq`、`requests`（每项为 `request_id`、`key`、`kind`；旧字段可为空） |
| `approval.closed` | 等待中的审批随 Run 取消而关闭 | `task_id`、`request_seq`、`request_ids`、`reason`（`cancelled`） |
| `workspace.selected` | 用户切换会话工作空间 | `previous_workspace_id`（可为 null）、`workspace_id`、`kind` |

任务计划的 `items[]` 包含 `key`、`relation`、`task_id`、`goal`、`constraints`、`criteria`、`depends_on`、`task_patch`、`input_locations` 和 `output_target`。`task_patch` 包含 `scope`、`expected_revision` 和 `operations[]`；每项操作包含 `op`、`field`、`id`、`value`、`evidence`。位置对象包含 `intent`、`device_id`、`workspace_id`。计划中还未分配的 Task ID 可以为 null。

`approval.requested.requests[]` 公共字段是 `request_id`、`key`、`kind`、`path`。Python 产物模式还包含 `execution_id`、`plan_hash`、`mode`、`inputs`、`script_sha256`、`execution_location`、`environment`、`network`、`timeout_seconds`、`output`、`working_directory`、`allow_delete`、`notice`。其中 `output` 包含 `target`、`include`、`overwrite`、`expected_files`、`publish`。文件授权包含 `access`、`scope`。旧 Python 适配器仅补充 `notice`。非对象的中断载荷用 `request_id` 和 `value` 表示。一次决定会应用于该批等待的中断。

Python 详细 `tool.result` 包含 `call_id`、`name`（`run_python`）、`task_id`（可为空）。按执行分支还可能包含 `execution_id`、`execution_status`、`publish_status`、`exit_code`、`stdout_tail`、`stderr_tail`、`logs_truncated`、`logs_ref`、`error`、`artifacts`、`published`、`conflicts`，或旧执行器的 `output`。产物字段为 `id`、`path`、`source`、`target`、`expected`、`action`、`sha256`、`size`；发布项字段为 `artifact_id`、`target`、`change_id`；冲突项字段为 `target`、`reason`。审批拒绝走普通工具结果分支，`content` 可包含 `rejected` 及执行状态。

## 设计说明与补充建议

调度器和任务运行器写 Run、Task 与模型输出；模型图提供工具调用、工具结果和中断；应用存储记录用户输入与审批决定。业务表有部分状态没有对应事件，以下为已实施的变更与仍然开放的建议：

| 状态 | 事项 | 说明 |
| --- | --- | --- |
| 已实施 | `execution.*` 与 `file.*` 不再写事件表 | 执行生命周期细节随 `run_python` 的 `tool.result` 返回（`execution_id`、`execution_status`、`publish_status` 等覆盖全部终态）；文件创建、覆盖、删除、撤销通过 `project_*` 工具的 `tool.call` / `tool.result` 表达，`changes` 表仍是撤销与审计的持久账本 |
| 已实施 | Task 失败/中断/恢复无事件 | 新增 `task.failed`、`task.interrupted`、`task.resumed`，恢复步骤不再重复使用 `task.started` |
| 已实施 | 等待中的审批随 Run 取消无标记 | 新增 `approval.closed`，`reason='cancelled'` 区分撤销与超时（超时机制仍未实现） |
| 已实施 | 空间切换会话时间线不可见 | 新增 `workspace.selected`，携带 `previous_workspace_id`、`workspace_id`、`kind` |
| 已实施 | 需求补丁只保留意图，看不到生效版本 | 新增 `task.revised`，携带 `base_revision`、`applied_revision`、`scope` |
| 已实施 | 工具失败会阻断 agent 运行 | `ToolErrorBoundary` 中间件把任何工具异常转换为 `status='error'` 的 `tool.result`，模型可观察并继续；审批中断与取消不受影响 |
| 开放 | 授权跨会话审计 | 授权若需要跨会话审计，另建审计流，记录 `grant_id`、`workspace_id`、`scope`、`access`、`action` |
| 开放 | 审批超时 | 当前没有超时机制；若实现超时关闭，需再写入 `approval.closed` 且 `reason='timeout'` |
