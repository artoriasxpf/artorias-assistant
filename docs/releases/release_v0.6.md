# v0.6.0 实施说明

2026-11-19。以 v0.5.1 源码为基线。Git 已初始化 main，并保留基线提交和本次实现提交；远程地址配置为 `https://github.com/artoriasxpf/artorias-assistant.git`。当前环境没有 GitHub 登录凭证，未创建远程仓库、未推送。

## Web 拆分

`web/index.html` 保留语义化页面骨架；`web/static/styles.css` 保存样式；`api.js` 统一 bootstrap、认证请求；`events.js` 管理 SSE 与游标；`workspace.js` 管理空间、文件和撤销；`app.js` 管理会话与消息。通过 FastAPI StaticFiles 提供 ES Modules，不增加构建工具依赖。切换会话中断旧流，游标局限于订阅，延迟到达的文件响应不会覆盖新会话。新增 label 与状态区域，API 保持原 token 检查。

## 数据库迁移

`storage/migrations.py` 为应用 SQLite 中所有业务表、checkpoint SQLite 的 `checkpoints` / `writes` 增加 `created_at`、`updated_at`，类型为 TEXT，UTC ISO 8601，毫秒精度。SQLite 自身的 `sqlite_sequence` 不属于业务表。

迁移可重复运行；旧记录无法恢复真实历史日期，首次迁移时回填时间。应用事务检测 schema_version，新增模块表后自动补齐字段和触发器；checkpoint 适配器在 saver.setup 后迁移。写入使用明确列名，避免新列破坏旧 SQL。数据库触发器处理插入和普通更新，业务不必逐条传时间。ON CONFLICT 更新保留创建时间；框架自身 INSERT OR REPLACE 表示删除重建，创建时间按新的物理记录计算。时间不是并发版本令牌；需求冲突使用 revision。

升级前停止进程，备份数据目录（包括 app.sqlite、checkpoints.sqlite、WAL 及 blobs；建议使用 SQLite backup 或停机后复制）。随后安装新版本并启动。迁移仅追加列，原资源、空间绑定与文件不搬迁。若回滚到旧代码，恢复旧数据库备份：旧代码的无列名 INSERT 不兼容追加后的表。

## Task 实现

执行顺序：候选召回 → 模型 Plan/patch → 归属、拓扑、引用和版本校验 → 同一事务保存 Plan、需求变更、Step → 固定上下文快照 → Runtime 执行 → 幂等交付。

|对象|新增字段|消费方式|
|---|---|---|
|business_tasks|revision、requirements、execution_count、progress|requirements 保存当前长期 goal、带稳定 ID 的 constraints/criteria；revision 用于 CAS；progress 为交付账本|
|task_steps|source_message_id、task_patch、base_revision、applied_revision、baseline_requirements、effective_requirements|原始消息由程序绑定；保存变更前状态和执行有效快照，可回放新版本的需求变化|
|全部业务表|created_at、updated_at|候选按修改时间排序、追踪记录|

永久变更在执行前生效；执行失败不撤销已接受需求。临时 patch 只改变当前 Step。缺失字段不删除，remove 必须指向已有 ID；每条操作必须引用本轮原文 evidence。原文匹配不等于语义正确，仍需要真实模型评测。重复安装同一 Run 不重复应用；旧 revision 被拒绝，当前通过失败事件提示，尚未自动重新规划。

候选包含最近任务、词项相关任务，以及用户明确引用的同 Session Task ID（可越过最近 200 条窗口）。上下文优先注入该 Step 的有效需求和本次范围、明确依赖；Task 成果用 Step.result，不再用混合多任务的 Session 最终回复。保留少量用户消息帮助指代。成果截断部分有 result_ref，运行时 task_result_read 可按页读取当前 Task 成果，不能越权读其他 Session/Task。上下文快照恢复时复用。

交付保存完整 Step.result；进展保留最近 20 项，摘要累积，执行上下文只选最近 5 项摘录。重复交付不增计数、不覆盖结果。交付状态仍为 needs_review，并不等于验收通过或用户确认完成。

读取目录和输出位置先保存语义意图 input_locations/output_target；Runtime 结合用户输入和这些意图调用 Workspace 工具。不会因为模型说 Downloads 就猜出 `/Users/.../Downloads`；路径解析与授权仍在 Workspace 层，尚未增加系统目录别名解析器。

## 请求预算与后续工作

业务上下文保留字符预算；Planner 和每次 Runtime 模型调用增加 UTF-8 序列化字节上限（ARTORIAS_REQUEST_MAX_BYTES，默认 160000）。Runtime 检查系统消息、历史、工具定义和模型设置；超限明确失败，不删除半组工具消息。它是保护阈值，**不是模型 tokenizer 计数或上下文窗口保证**，不声称已完成模型相关输出 token 预留。正式部署仍需 Provider 对应的 token 预算策略。

其他未实现项：结构化 decisions/pending/blockers 语义归并、文件成果索引与自动验收、用户完成确认交互、跨 Session 召回、语义向量检索、分布式 Repository/outbox、多租户、设备 RPC 和真实云对象存储。现有模块边界允许后续替换；本版本仍是单进程 SQLite 部署。

## Git 交付与推送

源码 zip 不含 .git、运行数据或凭证。bundle 包含 main 的完整提交历史。使用 bundle 恢复：

```bash
git clone artorias-assistant.bundle artorias-assistant
cd artorias-assistant
git remote set-url origin https://github.com/artoriasxpf/artorias-assistant.git
gh auth login
# 仅在该仓库尚不存在时执行；默认私有，避免意外公开个人代码。
gh repo create artoriasxpf/artorias-assistant --private
git push -u origin main
```

若远程已有不兼容历史，先 fetch 并检查，不能 force push 覆盖。也可在 GitHub 网页建空私有仓库后使用已配置的 Git 凭证推送。

## 验证记录

44 项 pytest 全部通过，包含实际 Deep Agents 图与模型替身、审批恢复、空间路由、文件备份/撤销、旧库时间迁移、永久/临时 patch、版本冲突、重复交付及成果读取隔离。四个 JavaScript 模块通过 Node 语法检查；静态路由与 API 认证通过 TestClient 验证。未运行真实模型 API、Docker、macOS 实机或浏览器端到端测试。
