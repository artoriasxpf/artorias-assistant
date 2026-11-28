# 长任务执行与 Task Work Plan 设计

状态：第一期和第二期已实现，实际接口和存储映射见第 7 节。适用于本地和云端 workspace。首期沿用 SQLite、现有单 worker 和 DeepAgent，交付范围为可恢复的只读分析。

## 1. 评估与取舍

调研提出的业务 step、执行单元、单次尝试三层边界可行。最值得先做的是让预算耗尽成为可恢复的执行状态，并给未完成工作一个稳定地址。单纯提高递归上限或每次重新规划，均无法解决进度归属和副作用重放。

| 调研涉及的问题 | 当前代码行为 | 设计决定 |
|---|---|---|
| 多个 step 的上下文和预算 | TaskRuntime 按顺序取 step，每步构建 task_context，thread 为 run:key。analysis segment 还会追加线程后缀。递归上限 200 作用于一次图调用；请求字节默认上限 160000，作用于单次模型请求。 | 保留这两道保护。隔离需要改在累计预算、执行归属和调度层。 |
| 同 run 多个 analysis step | analysis_executions 的主键仅有 run_id。begin_analysis 遇到不同 step_key 会抛错；正常 step 交付时 TaskRepository.deliver 已删除该行，run finish 也会清理。当前串行执行的两个正常 step 可以连续完成；若需交错挂起多个 step，则当前主键不足。 | 预算归属显式绑定 plan/unit/attempt。同一 run 可先后拥有多个独立 plan。 |
| 预算与交付 | ExhaustedAnalysis 会合成 partial FinishStep，后续 assess、deliver 会记录交付。DeferredAnalysis 或图递归异常在安全的 analysis 路径上会挂起；segment 达上限则 scheduler 抛错，可能结束整个 run。普通 step 的图异常进入失败路径。 | 区分可自动换段、需用户决定、业务部分交付和永久失败。只有业务交付才能写 task 结果。 |
| 跨轮继续 | 同 run 优先使用已有 task_plan 和 (run,key) 的 analysis 链接；新 run 会重新召回并规划。跨 run 复用 analysis 还要满足同 session、task、workspace、规范化 plan 完全相同、open 且无指定活动 run。 | 为继续操作提供 plan_id，保留原 step 的需求快照和依赖。 |
| 分批读取 | 当前已经有受控 project 读取、pending batch 重放、record_analysis_note、来源与 coverage。分析批次 payload 32 KiB、单条 note 12 KiB，回读约 16000 字节；快照中笔记和续读引用分别限 4800/2400 字节。 | 复用这些协议，先不新建另一套笔记工具。库存、搜索和抽样不能充当全文覆盖。 |

代码入口：[业务执行](../../src/artorias_server/tasks/executor.py)、[图执行与异常分流](../../src/artorias_server/runtime/service.py)、[预算与上下文裁剪](../../src/artorias_server/runtime/budget.py)、[队列与恢复](../../src/artorias_server/storage/sqlite.py)、[analysis 绑定与笔记](../../src/artorias_server/tools/analysis/repository.py)、[analysis 批次与预算](../../src/artorias_server/tools/analysis/service.py)。

## 2. 身份与存储

一个业务 step 的稳定地址是 task_steps 的复合主键 (origin_run_id, step_key)。key 可以跨 run 重名。每个长 step 至多一个 work plan；普通短 step 保持原路径。一个 plan 包含依赖有向无环图，一个 unit 可有多次 attempt。用户看到的 run 仍承载整轮业务，scheduler 可在同一 run 下多次领取 job；换 attempt 无需新建用户消息或调用 TaskPlanner。

| 新表 | 核心字段与约束 |
|---|---|
| work_plans | id、origin_run_id、step_key、task_id、session_id、workspace_id、applied_revision、strategy、status、pause_reason、budget、usage、version、created_at、updated_at。唯一 (origin_run_id,step_key)，外键指向原 step；索引 (session_id,status)。 |
| work_units | id、plan_id、unit_key、kind、goal、required、input_ref、parent_id、tool_policy、budget、status、progress_ref、next_ready_at、version。唯一 (plan_id,unit_key)；unit_key 根据分区范围和输入版本生成，重复扩展得到同一组 unit。 |
| work_unit_edges | plan_id、unit_id、depends_on_id，组合唯一；两个端点须属于本 plan。追加时校验环、重复边和最大节点数。 |
| work_unit_attempts | id、unit_id、attempt_no、execution_run_id、thread_id、status、started_at、ended_at、usage、error_code、side_effect_ref。唯一 (unit_id,attempt_no)，每个 unit 至多一个活动 attempt。 |
| work_memory | id、unit_id、kind、revision、summary、findings、decisions、open_questions、coverage_ref、source_refs、artifact_refs、analysis_id/note_refs。唯一 (unit_id,kind,revision)。正文、原始工具结果、大清单只存一次，其他位置存可校验引用。 |

首期直接引用已有 artifact、blob 和 analysis 记录，不另建 work_artifacts。analysis 通过绑定记录从 unit 找到自己的 analysis_id；首期一个只读 unit 可直接沿用原 step 链接。启用多组分片前，把 pending batch 的唯一归属切换到各 read_group 的 analysis_id，原 step 级链接仅用于兼容旧执行。inventory 快照供各组引用，禁止多个组共享同一个可变 pending 游标。表和索引走现有 storage migration；已交付历史保持原样，升级前已启动的 run 走旧路径到终态。

## 3. 调度协议

1. TaskPlanner 在 StepSpec 可选声明 execution_strategy，默认 direct。显式要求全量覆盖可标为 work_plan，只有目录参数不足以触发。首期也可直接由现有 scope.analysis 选择执行器。TaskPlanner 负责目标与验收；WorkPlanner 根据真实 inventory、预算和既有进度决定分区。
2. TaskRuntime 检查原需求 revision 和 step 依赖，通过 ensure_plan 和 append_units 幂等安装节点。scheduler 继续领取 run job。每次派发至多一个 ready unit，在事务中校验 plan 状态、依赖、版本和剩余额度，再创建 attempt。一次轮转后将还有可运行节点的 job 放回队尾；须给现有按 jobs.rowid 排序的 claim 增加可更新的就绪顺序，避免旧 job 长期占先。
3. 仅给 attempt 注入本 unit 的上下文及工具。普通 unit 用结构化 finish_work_unit 提交成果。运行时校验必需字段、来源和覆盖后，在一个事务中记录 memory、结算预算、结束 attempt、推进 unit、发布状态事件及唤醒后继。状态更新携带 attempt_id/version，重复或过期回报无效。
4. 只有最终综合节点能调用 finish_step。所有必需分区达到范围要求、无 pending batch 且通过原有 assess/deliver 后，才允许推进依赖 step。将最终提交保存为可重放记录，按 (origin_run_id,step_key) 幂等交付，保证崩溃重启不会重复更新 task memory 或创建 review。业务允许部分交付时写明实际覆盖、缺口和原因，由原业务验收决定结论。
5. 工作等待与审批、业务审阅分开路由。新增 work_waiting 状态及原因，调整 TaskRuntime 当前遇到 pending 一律发 task.awaiting_review 的路径。只有所有剩余步骤均无可运行工作时才停放 run；独立且就绪的业务 step 可继续执行。work 等待不能走 scheduler 正常返回即 finish(completed) 的分支。

| 触发 | attempt / unit 的处理 | run 与业务 step |
|---|---|---|
| 软预算或递归上限，已落盘进度且可安全重放 | 尝试结束为 budget_exhausted；按剩余范围重新排队或拆分 | step 保持执行中；自动换段 |
| 达总预算、连续无进展、来源版本冲突 | unit 挂起，保存原因、范围和需要的决策 | plan 等待；向用户显示进度及可选动作 |
| 审批 interrupt | 保存原 attempt、thread、request_id；决定仅投递给原 checkpoint | 沿用 waiting 和现有审批通道 |
| 超时或进程重启 | 关停遗留 attempt；已提交成果跳过，只读从最近已提交边界恢复 | 合并到 Store.recover，按 plan 状态重排 |
| 写入或脚本返回前失联 | 有可信回执则对账；无法判定效果则 blocked | 提示核验，禁止自动重复执行 |
| 用户取消、永久错误 | 取消未开始节点或记录失败，拒收迟到结果 | 关闭等待、审批和 job，保留可审计进度 |

拆分与新增子节点、调整依赖须同一事务提交。被拆节点作为汇合点，所有必需子节点完成后才满足原下游依赖。子节点仅负责尚未验收的范围。首期单 worker 串行；将来并行只读组前需实现跨进程租约、过期回收及原子预算预留。

## 4. 继续和权限

首选显式命令 resume_work_plan(plan_id, expected_version, request_id)，用于界面按钮与明确的“继续”。服务端按当前 session、workspace、原 task revision、plan 状态及权限重新校验。文本入口仅将无新增目标的继续意图路由到此命令：唯一可恢复 plan 时恢复；多个候选展示目标、范围和进度供选择；无候选走原 planner。带新约束的指令走需求变更和重规划。如果等待的是审批、审阅或预算追加，返回对应动作，普通“继续”不能代替决定或提高额度。

首次实现通过恢复原 run job 实现跨**用户轮次**续跑：保留继续消息及其 turn，记录与原 run/plan 的控制关联，界面可跳转原执行；用户消息入口在创建普通 run/job 前分流。原 run 的后续 step、交付、验收和事件继续使用原主键。同一 request_id 重放返回相同结果，多个恢复请求只唤醒一次。若以后需要新 run 接管执行，用 execution_run_id 标记执行载体，原业务主键不变；首期无需引入双 run 交付协议。

实际可调用工具取 workspace 与 session 授权、step 工具组、unit 策略三者交集，同时过滤模型工具列表并在调用端校验。只读 analysis unit 排除写入及 run_python。恢复时重新检查授权与输入版本；授权撤销、工作区切换、task revision 变化时暂停并显示原因。写入阶段优先使用既有变更计划、版本前置条件和回执，审批与工作进度分别存储。

## 5. 上下文、覆盖与成本

目录分析走 inventory → read_group → synthesis。inventory 以快照记录目标集合；按估计字节数、目录及文件范围做确定性分组，超大文件按带版本的范围拆分。每次提交批次笔记时保留来源范围和原始版本。综合前用目标集合与已验证范围计算覆盖，分别列出全文、抽样、跳过及失败项。文件中途变化，只使相应范围及派生成果失效。分组多时按上下文字节预算分层归纳，每层保留到叶子证据的引用。最终综合按需核实争议范围，无需再次遍历全部正文。

跨 attempt 使用新 thread，由 ContextBuilder 组装有效需求快照、unit 目标、有界依赖摘要、未决问题、来源与产物引用、权限和剩余预算。仅同一审批中断使用原 thread 恢复。已提交笔记直接产生进度索引，不额外请求模型总结每次 attempt。突发断流以最后一次已提交记录为准。涉及代码改动时，可复用“调查 → 变更计划 → 执行 → 验证”节点；执行结果包含变更清单和测试回执，覆盖规则由对应策略提供。

| 层级 | 计量及执行方式 |
|---|---|
| 单请求 / attempt | 保留 RequestBudget 按字节检查与图递归硬上限。模型调用前预留调用额度；按 attempt 记录模型次数、最大请求字节、工具次数、耗时与图步数。软阈值提前保存进度；单次请求过大先缩小输入。 |
| unit | 限制累计尝试、连续无进展次数和退避时间。以新提交范围、笔记或已验收产物衡量进展。相同 pending 项重复失败时停放并展示原因。 |
| step / task | step 限制总调用、运行时长、总节点数与拆分深度；task 在多个 run 间累计成本。自动换段、重试、拆分均继承剩余额度；达到总上限需显式追加或结束。等待人工决定的时间单列。 |

现有 analysis 在同一 run 的执行记录上限制 80 次模型调用及一小时，自动 suspend 另受 segment < 32 约束。首期保留保护力度，将对应用量持久化到明确的预算所有者；观察实际开销后配置默认值。运行中 direct 升级 work_plan 只在可描述剩余范围、可验证进度、无未确认副作用的边界进行。不具备条件时先停放，避免从空白状态重做。

## 6. 上线与验收

| 阶段 | 改动位置和交付 | 放行条件 |
|---|---|---|
| 1. 单 unit 可继续 | storage migration、TaskRuntime、runtime/service、Scheduler、消息与事件界面。沿用 analysis 工具，新增 plan、attempt、等待原因、显式恢复入口。 | 同一 run 连续两个 analysis step 成功；人为调低预算后无 task.delivered，重启及重复点击继续均恢复原 step，已提交批次不重读；只有一次最终业务交付。 |
| 2. 多 unit 全量分析 | 增加 WorkPlanner、独立范围/批次绑定、work memory、分组与分层综合。 | 多页清单和超大文件达到指定覆盖；文件变更只重验受影响范围；超总预算停止；没读过的文件不会标为已读。 |
| 3. 通用长任务 | 开放 planner 策略提示、安全动态拆分与受控写入 unit。 | 批量改名或文档编辑时，模拟写成功后连接中断，无重复副作用；权限撤销立即生效；短任务调用次数无额外规划开销。 |

新增事件 work.plan.created、work.unit.started、work.unit.completed、work.unit.suspended、work.plan.waiting、work.plan.resumed，携带 origin_run_id、step_key、task_id、plan_id，适用时附 unit_id、attempt_id、原因、已完成范围和预算余量。事件正文存引用，前端在原 step 下聚合，等待时停止 loading。数据库状态为恢复依据，事件用于展示；状态转移与对应事件同事务落库。

使用可控假模型和故障注入覆盖：提交前后断电、同一结束回报两次、两次继续竞争、旧 attempt 迟到、同 session 多 plan、审批与预算等待并存、暂停期间用户取消、恢复时需求过期。灰度首期开关仅控制新 plan 创建，已创建的 plan 按原协议恢复。比较每个已完成 step 的模型调用、重复读取字节、覆盖缺口、停放原因、恢复成功率和最终验收次数，再决定是否开放自动拆分与并行。

## 7. 一、二期实现与使用

只读 analysis 默认使用 WorkPlan。完整目录或仓库执行清点、文件分组、分层综合；完整单文件直接进入范围读取。抽样、优先级分析保留单单元策略。已建立旧 analysis 链接的执行沿用原协议。第三期的批量写入调度和并发 worker 尚未启用。

执行状态位于 work_plans、work_units、work_attempts。依赖列表存于单元的 dependencies；分组结论存于 summary，evidence 指向独立的 analysis 笔记和回执。工作单元只依赖已插入的前驱，调度器逐组提交、校验和唤醒，无需复制笔记正文。work_task_budgets 在同一业务 task 的多次执行之间累计已发出的工作模型请求。work_commands 对继续命令去重。

- 每组最多 8 个文件，分组目标为 32 KiB；单文件读取达到约 32 KiB 后可以从已提交的 UTF-8 字节边界和版本进入下一单元。每层综合最多接收 6 份依赖摘要，必要时用 read_work_notes 查阅来源。覆盖以清点快照为基准，跳过项和不可读文件单独计数。
- 配置项 ARTORIAS_WORK_ATTEMPT_CALLS、ARTORIAS_WORK_MODEL_CALLS、ARTORIAS_WORK_TASK_MODEL_CALLS 的默认值分别为 24、80、300。单次尝试达到上限且已提交新进度时自动轮转。总额度耗尽时暂停，页面显示已用额度、单元进度和追加按钮。ARTORIAS_WORK_PLANS=0 关闭新计划创建，已创建计划仍可恢复。
- GET /api/sessions/{sid}/work-plans 返回等待继续的计划。POST /api/sessions/{sid}/work-plans/{pid}/resume 接收 generation、request_key 和可选 additional_calls。恢复命令在原 run 的时间线上留痕；待办唯一且额度足够时也可以直接发送“继续”。
- 本地和托管云工作区在综合前用文件元数据核对版本。仅提供版本化分页读取的远端连接器可读取最多 4 字节复核。发现版本变化时，旧尝试保留用于审计，重新生成受影响的读取和综合结果。

读取分组保存的限长预览与综合输入独立。综合加载完整批次笔记及其来源，包括 findings 和 open_questions。直接依赖的 JSON 超过 24 KiB 时，按完整笔记拆分综合单元，先逐批语义归纳再汇总。模型可通过 read_work_notes 追溯原始笔记。进行中的计划若已存在旧版综合结果，将从原笔记重新综合。
