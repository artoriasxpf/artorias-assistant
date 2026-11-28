# 本地与云工作空间的 Python 与命令行执行设计

状态为设计提案，调研日期为 2026-11-28。目标是在本地 workspace 中支持两类日常工作：扫描整个项目并分析，以及运行工具直接修改选定目录。执行环境始终施加沙箱策略。是否复制项目、是否允许改原文件，是独立的文件访问决策。

## 一、调研与方案选择

| 编程 agent | 官方实现或行为 | Artorias 可采用的设计 |
| --- | --- | --- |
| OpenAI Codex CLI | macOS 将网络及文件根权限编译为 Seatbelt 策略。workspace-write 可以开放工作树，同时保护 Git 元数据，包括 worktree 的 gitdir 指向。Linux 有独立的策略实现。 | 由设备执行器根据已批准的目录编译规则。目录内的保护路径及后端能否落实规则都是能力判断的一部分。[Codex Core](https://github.com/openai/codex/blob/main/codex-rs/core/README.md) |
| Claude Code | Bash 工具支持文件系统和网络隔离，沙箱和工具许可分别配置。可限制凭据文件和环境变量，也提供严格沙箱模式。 | 批准运行某条命令与决定该进程可访问什么分别处理，工具失败不会触发无沙箱重试。[Claude Code 沙箱](https://code.claude.com/docs/en/sandboxing) |
| Gemini CLI | Docker 后端可以把当前目录绑定到容器内同一路径，直接操作项目；额外挂载可声明 ro/rw，扩大范围时向用户展示请求。 | 原目录可以直接暴露给沙箱，无需先复制整仓。绑定目录必须由持有该 workspace 的设备解析。[Gemini CLI 沙箱](https://geminicli.com/docs/cli/sandbox/) |

三种路径各有适用场景。暂存声明输入适合处理少量明确文件，可精确复现输入。工作副本适合要预览、条件发布或撤回的修改，代价随文件数量和体积增长。原目录访问适合大型仓库，准备成本与仓库总字节数解耦，执行期间能看到外部编辑，也可能留下部分修改。Git worktree 可用于单独分支开发，但默认视图无法反映当前所有未提交及未跟踪文件，也要求项目使用 Git，因此不作为通用目录执行基础。

## 二、当前项目的落点

| 现有位置 | 当前行为 | 需要扩展的边界 |
| --- | --- | --- |
| `src/artorias_shared/execution/models.py` | `PythonRequest` 支持 path/code 二选一、64 KiB 内联源码、isolated/workspace_copy、输入、产物、依赖和超时 | 扩展 mode，增加独立 CommandRequest 和跨字段校验 |
| `src/artorias_server/tools/{registry,policy,executor}.py` | execution 组目前只注册 run_python。WorkspaceToolExecutor 处理审批和结果，另有只接受脚本路径的兼容入口 | 按连接设备能力注册新工具并校验 step 授权。新增能力只走 ExecutionService |
| `src/artorias_server/tools/execution_service.py` | prepare 暂存入口与输入，workspace_copy 枚举文件并建立基线。execute 校验后运行，publish 条件写回，recover 处理未完成执行 | 在枚举、默认输出目标及发布前按 mode 分流。就地执行仅解析目录及入口 |
| `src/artorias_device/tools/{macos_python,python_backend}.py` | macOS 分离 uv 环境准备与脚本执行，限制网络、进程和可执行路径。Docker Python 后端使用暂存数据 | 提取受限进程启动与取消逻辑，分别实现 Seatbelt 目录规则和容器挂载 |
| `src/artorias_server/storage/executions.py` | python_executions 保存调用身份、计划、状态和结果，execution_publications 对发布操作去重 | 复用执行身份及状态转换，直接写入走独立终态 |

### 云端 runtime 与执行位置

runtime 可部署在云端，负责 agent loop、审批和执行记录。每次调用从服务端已绑定的 workspace 取得 kind、location_id、workspace_id，按租户和位置查找执行器。执行器公布工具、模式和命令能力；同一套 run_python/run_command 接口可用于以下部署。

| Runtime | Workspace | 进程在哪里运行 | 文件如何访问 |
| --- | --- | --- | --- |
| 桌面本地 | device_fs | 同机 Device Agent，可用进程内适配器 | 设备沙箱暂存输入或打开授权的真实目录 |
| 云端 | device_fs | 指定 location_id 的用户设备 | Device Agent 主动建立认证连接，在设备上解析根目录、暂存和运行。就地分析无需上传整仓 |
| 云端或桌面 | cloud_storage，有持久工作卷 | 与该卷相连的 Cloud Runner | 在按租户隔离的沙箱内挂载对应卷，能力允许时选择 ro/rw |
| 云端或桌面 | cloud_storage，仅对象存储 | 该存储位置的 Cloud Runner | 暂存声明输入，或物化有版本的目录副本并按对象版本条件发布。此后端只公布可实现的模式；需要就地语义时返回 unsupported_mode |

**代码现状。** ExecutionService 已按 kind 选 device/cloud，并接受注入的 cloud_backend。RemoteDeviceConnector 和 CloudStorageConnector 的 open 仅转交给外部 client。当前本地启动器用 ManagedCloudConnector 包装本地实现；PythonExecutionBackend.run(plan, root: Path) 和 prepare 中的暂存目录仍假设共享文件系统。生产部署还需设备执行通道及实际 Cloud Runner。现有 execution_hint 在新路由中只能与 workspace 绑定位置一致；跨位置传输输入执行需另行授权。

**执行协议。** 统一接口为 capabilities、prepare、start、status、cancel、read_log。进程内适配器也实现此接口。prepare 接收 execution_id、workspace_id、相对目录、请求及脚本内容或受限产物引用，由目标执行器解析、暂存并返回 plan_hash、执行范围及计划有效期。云端保存计划摘要，审批卡显示目标设备或云位置以及将返回的输出。start 携带绑定租户、位置、workspace、execution_id、plan_hash 和过期时间的批准凭据；执行器再次核验本地授权。服务端路径和设备临时文件路径均留在创建它们的进程侧。就地执行默认回传有界输出及结果；详细日志按引用鉴权读取。

**重连与验收。** 执行端在启动前持久领取 execution_id，相同请求返回已有句柄或结果。设备离线且尚未派发时显示等待设备，可取消或到期；start 已发但丢失应答时按原 ID 查询，结果未明时禁止以新 ID 自动重跑。断线期间由执行端执行超时，重连后协调取消或接收终态。用一台云 runtime 和一台独立设备验收：两端创建同名但内容不同的测试目录，切换 workspace 后用两个工具分别读取和写入，核对执行位置、权限、结果和改动归属；覆盖错租户、设备离线、执行后掉线、重复派发、卷只读拒写及对象存储版本冲突。

## 三、执行模式与工具契约

| mode | 进程看到的项目 | 对真实文件的影响 | 工具与典型用途 |
| --- | --- | --- | --- |
| isolated | 只暂存声明输入，临时目录可写 | Python 可按现有 output 策略发布产物 | Python 默认；命令可在临时目录处理声明输入 |
| workspace_copy | 指定目录的合格文件副本 | 执行后按基线发布，可报告冲突及 change_id | 保留现有 Python 批量变更及撤回能力 |
| workspace_ro | 选定真实目录只读，临时目录可写 | 原文件受操作系统写保护 | Python 统计、rg 搜索、git status |
| workspace_rw | 选定真实目录允许直接写，保护路径除外 | 创建、覆盖、移动、删除立即生效 | Python 批量改名、就地运行格式化器 |

**run_python** 保留现有默认值和旧请求语义。新增两种 workspace 模式，使用已有 working_directory 表示**授权根兼初始 cwd**，默认关联 workspace 根。可用相对路径指定更小范围。path 入口在该范围内解析并固定入口字节；code 暂存为只读入口。启动器显式把入口原始父目录（内联则取 cwd）加入 Python 项目导入路径，保留包内模块导入能力，说明暂存入口的 __file__ 指向临时文件。venv 和依赖锁仍由现有环境准备流程管理。直接模式禁止显式 inputs、code_directory、output、allow_delete，校验时区分调用方显式传值和旧模型默认值。workspace_rw 的目录写权限本身包含删除，审批卡必须说明。需要产物发布或单文件撤回时选择副本流程。

**run_command** 首版采用以下契约。单次调用启动一个前台进程并等待退出。工作目录和环境不跨调用保留。

| 字段 | 约定 |
| --- | --- |
| argv | 必填非空字符串数组。首项为设备公布的命令名，后续元素逐个传递。最多 100 项、合计 16 KiB UTF-8，拒绝 NUL |
| mode | isolated、workspace_ro、workspace_rw，默认 workspace_ro |
| working_directory | 就地模式同 Python；isolated 使用执行专属临时 cwd |
| inputs | 仅 isolated 支持，复用 InputResource，只读暂存到 inputs 目录。首期命令结果仅返回 stdout/stderr 及退出状态 |
| timeout_seconds | 与 Python 一致，默认 120，范围 1–300 |

首期设备工具清单支持 rg 搜索、git status，以及管理员配置的格式化器（例如 ruff format）。按可执行文件及允许的子命令/选项模板声明能力，使用设备固定路径或容器镜像内已验证的二进制。支持清单写入工具说明。模型可以传带空格的文件名；管道、重定向、变量和通配符不会经 shell 展开。禁止把 sh -c 或通用解释器作为清单捷径。需要多步过滤时可调用 run_python(code=...)。后续如确需 shell 语法，应独立公布 shell 能力、固定解释器、禁用启动文件并覆盖整个子进程树的策略和测试。

示例，三个请求分别搜索当前项目、统计指定目录，以及在原目录格式化代码：

~~~json
{"argv":["rg","-n","TODO","."],"mode":"workspace_ro"}
~~~

~~~json
{"code":"from pathlib import Path\nprint(sum(1 for p in Path('.').rglob('*.md') if p.is_file()))","mode":"workspace_ro","working_directory":"docs"}
~~~

~~~json
{"argv":["ruff","format","."],"mode":"workspace_rw","working_directory":"src"}
~~~

工具说明提示模型：使用 project_read/search 可完成的单次文件操作优先沿用现有接口；跨文件计算用 Python，只需原生工具输出时用命令。读取整仓默认只读，需要直接落盘才请求 rw。runtime 按通用工具分组与能力选择，无需为某个分析任务修改 agent loop。

## 四、从工具调用到设备进程

1. **解析请求。** ExecutionService 接收带 kind 的 python/command 请求。设备报告 supported_modes、允许的命令模板、网络及子进程能力。服务端和设备端均拒绝不支持的组合。已有 Python 暂存模式继续使用原路由。
2. **绑定范围。** 根据 session 的 workspace_id 和 location_id 找到真实根，在设备侧解析 working_directory，检查规范路径属于授权目录，记录目录身份（设备号、inode）。首版就地范围限关联 workspace 及其子目录；project_authorize 授予的额外文件权限不会自动扩展进程可见范围。工具参数不能提供挂载参数、宿主环境或 sandbox profile。
3. **审批。** 沿用现有 code_execution 中断机制。卡片展示工具、准确 argv 或可展开的 Python 源码、设备、真实目录、只读/读写、网络、依赖与超时。就地读写额外展示“更改立即写入原目录，失败也可能保留”。新增模式首版逐次批准。plan_hash 绑定请求、入口摘要或二进制身份、目录身份、权限、策略版本和后端。审批恢复后重新解析并核验；目录被替换或权限已撤销即结束该调用。
4. **执行。** 设备从固定命令清单解析 argv[0] 后直接启动进程。Python 使用已准备的只读 venv。就地模式跳过 files_under、整仓哈希和 seed，允许项目在执行期间变化。环境变量采用白名单，HOME、TMPDIR、缓存指向独立目录，默认禁网，关闭 stdin。安装 Python 依赖时单独沿用现有审批和联网阶段。
5. **收尾。** 复用有界日志、超时和取消。输出摘要返回模型，完整诊断按既有容量上限保存，结果携带 logs_ref、logs_truncated。工具的非零退出码保留，例如 rg 返回 1 表示无匹配，不能据此推断沙箱拒绝；只有可判定的权限或能力错误才返回对应错误码。

### 两种后端的权限实现

| 项目 | macOS 原生 | 本地 Docker |
| --- | --- | --- |
| 文件视图 | Seatbelt 授予真实根 subpath 读或读写，cwd 为选定目录，单独开放系统运行库、入口、受管程序和临时目录 | 使用受控镜像，将根挂载到 /work，ro 或 rw，cwd=/work；保留只读镜像根与独立临时卷。模型按相对路径工作 |
| 进程 | Python 保持原有禁止 fork 的执行策略。简单命令默认只准一个可执行程序；需 helper 的命令逐项声明并验证子进程继承策略 | 受限身份、禁网、进程数和资源限额、无宿主 socket 或额外 capability。仅开放镜像已提供且经过测试的命令模板 |
| 保护区 | 对 Git 元数据、设备数据与授权配置、凭据目录设置更具体的保护规则；允许 git status 所需的只读元数据。绝对路径、符号链接目标也必须受相同限制 | 使用只读子挂载或遮蔽保护区，同时验证不可被父目录 rename/unlink 替换。无法等价落实时不公布该模式 |

执行前以实际后端探针验证沙箱生效。git status 固定禁用可选索引写锁、外部 fsmonitor 等辅助程序；读取被保护的 worktree 外部元数据时返回明确的范围错误。拒绝项目内指向外部的链接穿越，特别检查可写树的硬链接别名：原生路径规则及 Docker rw 挂载本身不足以保证多链接文件的外部别名不受影响。对用户选定的普通项目目录，按路径授权 rw，审批说明修改多硬链接文件会影响共享同一 inode 的其他名字。若部署要求外部文件对象绝对不受影响，仅在独立文件系统等能落实该保证的范围启用 rw，其余返回 workspace_rw_unavailable 并提示 workspace_copy。一次静态扫描无法覆盖并发建链。

原生后端当前缺少容器级总 CPU、内存和磁盘配额，能力应如实显示。需要派生进程的命令，只有在后端能够继承文件/网络限制并在取消时清理后代后才加入清单。挂载、解析或探针失败时关闭相应能力，绝不改用宿主机裸进程。

## 五、并发、恢复与展示

- **幂等。** ExecutionRepository 沿用 session/run/task/step/call 派生的 execution_id，用请求哈希识别同一次调用参数变化，领取执行用数据库条件更新。先以每个 execution_id 的独占准备区解决并发 prepare 清理同名暂存目录的问题，再允许两类工具并发进入。已结束的调用返回缓存结果。
- **写入竞争。** 设备对同一 workspace 的直接写执行采用独占租约；只读执行可并行，遇到本 agent 的写执行则排队。将 project 写操作和副本发布接入同一个设备写入协调器。外部编辑器不参与锁，就地读可能看到多个时点的内容；需要稳定输入时选暂存。租约在设备确认进程树结束后释放。
- **中断。** rw 一旦启动就标记可能修改原文件，失败、超时、取消和断连都如实返回。重启查询设备执行句柄，无法确认时标记 unknown，禁止自动重跑或发布，UI 提示检查实际文件。恢复逻辑只对旧发布模式的 collected 记录继续 publish。读写模式不自动执行 git reset，也不承诺 change_id 或撤回。
- **数据与事件。** 初版沿用 python_executions 表，在 plan JSON 新增 kind、mode、root_identity、backend 与 policy_version；旧行省略 kind 按 python 解读，旧审批哈希沿用。execution_publications 继续只记录受控发布。复用 tool.call、现有审批事件及唯一 tool.result，以 execution_id、call_id、task_id 关联。结果新增加实际 mode、working_directory、effects（none / workspace_may_have_changed）及明确错误码；就地模式 publish_status=not_requested。输出超限保留头尾、标记省略并通过受权限控制的日志引用查看，避免整段输出回灌模型。
- **前端。** 在现有审批卡与结果气泡区分“副本发布”和“直接修改”，支持查看 argv 分项、脚本、阶段、退出码及部分写入提示。直接执行结束后使 workspace 文件视图及依赖其文件版本的缓存失效。Git diff 只能作为用户查看当前状态的手段，不能归因为本次调用的完整变更清单。

## 六、实施次序与验收

| 阶段 | 实施范围 | 通过条件 |
| --- | --- | --- |
| 1. 契约与共享执行生命周期 | 扩展 shared/execution、ExecutionService、设备能力、tools/policy 和审批 UI，保留旧 Python 请求的序列化及指纹 | 旧 isolated/workspace_copy 的审批、发布、撤回及中断恢复用例通过；重复同一 call 只启动一次 |
| 2. macOS 就地只读与命令 | 原生 Python workspace_ro、run_command isolated/ro、受管 rg/git，前端显示真实范围 | 扫描含未跟踪文件的大仓库，准备阶段零项目文件枚举或复制（指定脚本入口除外）；带空格的路径可用；向 cwd 写入或通过链接越界失败；超时进程结束 |
| 3. 就地读写 | 能落实范围的设备启用 rw，加入格式化器、写租约和中断处理 | 能就地修改指定子目录；父目录与 Git 元数据受保护，验证多硬链接语义和严格策略下的拒绝行为；故意在第一次写入后中断，文件保留、UI 提示部分生效、恢复后执行次数仍为一 |
| 4. 本地 Docker 对齐 | 实现相同工具契约、挂载与命令能力探测，复用后端验收集 | 验证容器只读挂载、保护子树、UID/GID、资源耗尽、取消、禁止联网和 Docker socket 隔离；云 workspace 或远程 daemon 拒绝本地就地执行 |

跨机实施从第 1 阶段起固定位置路由和可序列化执行协议。随后接入设备网关和 Cloud Runner，两端分别通过上文的跨机验收后开放对应能力。

记录两组性能基线：对同一大仓库分别执行 workspace_copy 和 workspace_ro，测量准备耗时、暂存字节与首条输出延迟；对 rw 比较单文件编辑与全项目格式化，确认执行器不会为收集结果额外复制全仓。发布开关按设备、工具和 mode 独立启用。只读阶段可独立交付。读写阶段须分别验收普通目录的路径授权，以及部署启用严格文件对象隔离时的能力判定。

## 七、执行依赖预检和按需安装

本节为后续实现。实际执行任务的 runner 负责检查依赖。本地安装流程负责提供设备 runner 的基础依赖，云端容器通过镜像提供基础依赖。云端 runtime 根据 workspace 绑定获取目标 runner 的能力，供模型规划和选择工具。

### 检查时机

| 时机 | run_command | run_python |
| --- | --- | --- |
| Runner 启动或执行环境变更 | 在实际设备或镜像中检查承诺支持的程序，记录可用性及版本。例如使用执行时相同的程序解析逻辑查找并探测 `rg --version` | 检查解释器版本、执行环境及创建虚拟环境的能力 |
| 模型选择工具前 | runtime 从目标 runner 的 `capabilities` 获取实际可用的命令和缺失项，并在同一运行中复用结果 | 告知模型目标 runner 支持的 Python 环境、依赖声明和按需安装能力 |
| 调用准备阶段 | 先校验命令及参数策略，再确认所选可执行文件仍可用 | 校验 `dependencies` 中的包及版本，检查本次环境是否已满足要求 |

命令白名单用于判断是否允许请求，能力探测用于确认程序是否可运行。现有本地探测依赖进程的 `PATH`，远程 runner 已暴露 `/capabilities`，还需由 runtime 客户端读取。Docker 需以当前执行镜像的探测结果报告能力。切换 runner 或镜像、更新依赖后刷新能力，调用前复查可执行文件及环境身份。Python 由模型显式声明依赖，静态扫描脚本中的 `import` 仅用于提示。

### 缺失依赖的处理

| 预检结果 | Runner 行为 |
| --- | --- |
| 基础命令缺失，例如承诺提供的 `rg` | 将对应能力标记为不可用，返回 `dependency_missing`，提示此执行环境找不到程序。由本地安装流程补齐，或更新并重新探测云端镜像 |
| 命令或参数未开放 | 返回 `command_not_allowed`，由命令策略决定是否开放 |
| 已允许的任务依赖缺失，例如显式声明的 `openpyxl` | 在部署策略已允许包源、安装位置及权限的情况下，由 runner 自动准备任务环境。需要扩大权限时，先走审批 |
| 需要更改宿主机、共享环境或项目依赖清单 | 通过独立维护操作处理。明确目标设备、变更范围和版本，取得授权后安装并重新探测 |

任务依赖由 runner 在准备阶段安装。执行计划记录声明的依赖、解析的版本和运行环境标识，执行阶段使用同一个已准备的环境。依赖准备只能写入受管的任务环境并访问允许的包源。实际脚本和命令继续遵守原有网络及 workspace 权限。`workspace_ro` 允许准备隔离依赖环境，项目目录仍然只读。任务安装的依赖随任务环境清理。跨调用使用同一环境时，由 runner 显式绑定环境标识。

### 实施与验收

1. 先补齐基础依赖、接入本地与远端能力预检，并区分 `dependency_missing` 和 `command_not_allowed`。用故意缺少 `rg` 的 runner 验证模型在执行前能获得缺失原因。补齐依赖并刷新能力后，订单审计的两次搜索都应返回真实的 `rg` stdout。若用户明确要求验收 `rg`，工具不可用时应将该项标为受阻。
2. 复用 `run_python` 的 `dependencies` 入口实现按策略准备任务环境。验收包版本匹配、同一次执行可导入、只读工作目录受保护，以及任务环境清理。
3. 有具体需求时再增加 runner 管理的 CLI 依赖清单，记录可安装命令、包源和版本。验收安装与运行使用同一环境，并验证缺失依赖不会自动触发宿主机安装。

## 已实现的启动与部署方式

桌面启动器会为关联的本机目录和开发用逻辑云目录注册进程内执行器。生产部署中，每个设备或云卷所属位置各运行一个独立 runner。macOS 就地执行使用 Seatbelt，Linux 就地执行使用 bubblewrap，要求可创建非特权用户命名空间。Linux 缺少 bubblewrap 时选择 Docker。可用 ARTORIAS_WORKSPACE_SANDBOX=docker 显式选择本地 Docker daemon，镜像由 ARTORIAS_IMAGE 指定，需要在镜像中预装命令。Linux 暂存 Python 使用原有 Docker 镜像。命令需预装到受信任的 PATH；支持 rg 搜索、git status 和 ruff format。命令行参数逐项传递。

在工作空间所有者机器上配置以下 JSON，保存到工作目录以外。将 workspace ID 和 location ID 换成 runtime 已绑定的实际值。配置文件及 runner 数据目录仅对运行服务的账号开放，每个租户使用独立 token、进程与数据目录。

~~~json
{"kind":"device_fs","location_id":"DEVICE_ID","workspaces":{"WORKSPACE_ID":"/absolute/project"},"data":"/absolute/private/runner-data","token_env":"ARTORIAS_RUNNER_TOKEN"}
~~~

workspaces 的映射值也可填写，例如 `{"root":"/absolute/project","access":"read"}`。设备端在 prepare 和 start 时分别核验该范围，允许值为 read 和 write。

设置 ARTORIAS_RUNNER_TOKEN 为双方共享的高熵随机密钥（至少 32 字符），ARTORIAS_RUNNER_CONFIG 指向上述文件，再启动单 worker：

~~~sh
uv run uvicorn artorias_local_app.runner:create_app --factory --host 127.0.0.1 --port 8778
~~~

云 runtime 可通过既有 HTTPS 私网入口连接，也可由设备主动建立 SSH 反向隧道。例如在 Mac 上运行以下命令后，云 runtime 能通过云机的回环端口 18766 访问该 Mac 的 runner：

~~~sh
ssh -NT -o ExitOnForwardFailure=yes -R 127.0.0.1:18766:127.0.0.1:8778 user@runtime-host
~~~

云卷使用相同的 runner 启动方式，kind 填 cloud_storage，workspaces 的值填该 runner 可见的卷挂载路径。云 runtime 设置 ARTORIAS_RUNNER_ROUTES 指向路由配置文件：

~~~json
[
  {"kind":"device_fs","location_id":"DEVICE_ID","url":"http://127.0.0.1:18766","token_env":"ARTORIAS_MAC_TOKEN"},
  {"kind":"cloud_storage","location_id":"cloud-default","url":"https://cloud-runner.example.internal","token_env":"ARTORIAS_CLOUD_TOKEN"}
]
~~~

runtime 进程的相应 token 变量与各 runner 分别配对。云端单用户 runtime 可执行下列命令启动，沿用回环地址访问 UI。生产部署已有认证与 workspace 文件 connector 时，可在部署工厂中传入 workspace_registry。

~~~sh
uv run artorias-server --factory artorias_server.remote_app:create_app
~~~

runner 根据本地配置的 workspace ID 解析目录；请求参数不能指定宿主根。就地执行只传脚本/参数和有界结果，暂存 Python 在远端复用输入快照、条件发布和执行日志。纯对象存储继续通过部署注入的暂存后端使用 Python，需要直接读写时应注册持久卷。文件工具的远程 connector 与脚本执行通道分别配置。

故障排查从已认证的 GET /capabilities、POST /status 开始。status 请求体为 execution_id；重新投递相同 ID 和已批准的 plan_hash 可查询或领取同一计划。设备端只启动一次。运行记录持久化，设备进程重启后无法核实的运行返回 unknown。Mac 或云 runner 断开时，应先核查原执行再决定是否发起新调用。
