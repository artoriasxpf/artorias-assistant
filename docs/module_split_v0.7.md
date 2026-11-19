# v0.7 五模块拆分设计与迁移说明

日期：2026-11-19。基线：v0.6；本次为代码包边界重构，保留本地单用户运行模式和已有 Task 语义。

## 1. 产品与部署边界

采用单仓库、五个 Python 顶层包，当前仍一起发布一个 distribution。包边界不等于进程边界，更不代表已经完成微服务部署。

| 顶层目录 | 职责 | 可依赖的项目包 |
|---|---|---|
| src/artorias_shared | events、approvals、tools、workspace 的消息与接口，ID 和执行协议 | 无 |
| src/artorias_device | tools/sandbox.py；workspace 的本地 Blob、connector、local_workspace；storage/journal.py | shared |
| src/artorias_server | application、api、sse、scheduler、runtime、tasks、model_provider、tools、workspace、storage、main | shared |
| src/artorias_web | index.html、css/、js/ | 无 Python 业务依赖 |
| src/artorias_local_app | bootstrap、本地 main、单机托管空间适配器 | server、device、web、shared |
| data/ | 本地数据库、Blob、工作目录 | 运行时目录，不是源码模块 |

本地启动器为唯一组装 server 与 device 的位置。服务端逻辑通过协议调用设备能力，不导入本地文件实现。设备文件操作可独立使用自己的 SQLite journal，不需要导入 Store、DeepAgents 或 FastAPI。

## 2. 代码结构与调用链

### 2.1 组装和启动

`artorias_local_app.bootstrap.create_app` 创建 Store、设备 journal、连接器注册表、runtime 和 scheduler，挂载 `artorias_web`，通过 FastAPI lifespan 启停本地 worker。

`artorias_server.api.app.create_http_app` 接受注入的 scheduler 控制对象、workspace registry、Web 资源路径和 lifespan。它不会自行启动 worker。SSE 游标回放与轮询已提取到 `artorias_server.sse.events`。

`artorias_server.main` 是可配置的 Uvicorn 入口：`artorias-server --factory module:callable`。工厂负责身份认证、Repository、连接器与生命周期；当前不提供预配置生产云端工厂。默认仍监听 loopback。本地使用 `artorias-assistant`，兼容原命令。

### 2.2 文件工具

调用链：Runtime → tools/registry.py 的 LLM 工具适配 → files.py → workspace/routing.py → connectors/registry.py → Connector.open → FileOperations。

- `catalog.py` 管理 workspace 元数据、Session/Task/Run 绑定与授权。
- `routing.py` 根据目标资源解析位置、检查授权，选择 `(kind, location_id)` 的连接器。
- `Connector.open(workspace, WorkspaceContext)` 只接收空间描述和 Session/Run/Task 标识，不再传递服务端 Store。
- `device/workspace/connector.py` 实现本地 endpoint；`local_workspace.py` 完成文件操作；`blobs.py` 存取不可变备份；`storage/journal.py` 负责操作日志与恢复记录。
- 服务端 `RemoteDeviceConnector`、`CloudStorageConnector` 委托注入客户端的 `open_workspace` 返回文件操作代理，并验证目标类型与 location_id。它们不读取服务端本机文件。
- 无注册连接器时抛出 ConnectorUnavailable；API 返回 503，不更换输出位置。

两个远端适配器目前是客户端接入边界，尚未实现 HTTP/RPC、设备网关、对象存储客户端、序列化及断线重连。接口目前同步；真正远程 I/O 接入时应统一异步接口或使用有界线程适配，避免阻塞事件循环。

### 2.3 工具执行器

`tools/registry.py` 负责模型可见的工具定义和审批适配；`tools/executor.py` 提供 ExecutorRegistry 和 ToolExecutor，通过 shared.ToolRequest/ToolResult 分发代码执行。默认执行器由部署组装选择，无配置则失败，不自动启用本机 Docker。

文件写入由 workspace 路由选择目标；代码在哪个 sandbox 执行是另一个维度。云端空间并不自动意味着云端执行器。本地启动器显式注入 device.tools.sandbox.LocalDockerExecutor。该执行器将授权空间文件导出到只读容器，返回日志；容器产物自动回写尚未实现。

shared 中的 ResourceRef/Event 等为稳定的数据契约起点；旧业务层部分字典接口仍保留，本次没有强制替换全部持久化和模型输入结构。

## 3. Session、Task、Run 的绑定和消费

沿用 v0.6 的绑定表与数据，无重命名或清空：

| 表 | 主要键和关联 | 用途 |
|---|---|---|
| workspace_locations | id、kind、root、blob_root | 标识设备/存储位置 |
| workspaces | id、location_id、root | 一个位置内的 workspace |
| session_workspaces | session → workspace_id | 会话当前选择 |
| task_workspaces | task → workspace_id | Task 默认空间 |
| run_workspace_bindings | (run,task) → workspace_id | 本次执行稳定快照 |
| workspace_grants | location_id、root、access、scope_run | 持久或 Run 范围授权 |

新 Task 默认承接 Session 当前选择；既有 Task 使用其绑定，执行中使用 Run 快照，Session 切换不会改写正在执行的默认目标。明确指定 `workspace://ID/path` 或本机绝对路径时，按授权解析该资源；否则相对路径在 Task/Run 空间内解析。模型理解位置意图，程序解析地址并选择连接器。

当前 catalog 仍包含单机默认设备、cloud-default 和本机路径规则；多设备注册、跨操作系统路径解释、租户隔离与云端默认空间策略是后续改造项。不能仅将 current catalog 原样部署公网就认为已支持多租户云端。

## 4. Blob 与变更日志

Blob 跟随资源执行端保存，而非跟随对话窗口或 worker 所在位置。

- 本地真实文件 → device Blob 实现，默认 data/blobs。
- 本地演示的 cloud-default → data/cloud/files 与 data/cloud/blobs，由 local_app.ManagedCloudConnector 显式组装。这是单机托管逻辑空间，不是远端对象存储。
- 未来真实云端 → 云端 connector 自己实现对象存储、版本检查、变更日志和恢复语义。

设备端依赖 WorkspaceJournal 协议；SQLiteWorkspaceJournal 自己创建和维护 workspace_bindings、workspace_operations、changes、resource_changes、events 及时间字段，不依赖服务端数据库类。

为了兼容已有本地数据库和 SSE，all-in-one 中 journal 与 Store 使用同一个 data/app.sqlite；这是有意保留的物理共库。独立设备可以使用 device.sqlite，但其 events/changes 尚不会自动同步到服务端。生产环境需要事件 outbox、幂等同步、变更索引与远端 ChangeLookup。

写入流程仍是备份前后内容 → 记录 pending → 二次版本检查 → 原子替换/删除 → journal 提交变更与事件。finish 在同一事务中检查 pending 状态，防止重复提交同一变更事件。恢复只根据文件内容与 mode 的证据判定 committed/aborted；不确定操作保持 pending。

撤销通过注入的 ChangeLookup 查找原始资源元数据，再路由到原连接器，不能按 Session 当前空间重解释历史变更。保留读取版本冲突保护、路径越界检查及历史 Blob 校验。外部编辑器不遵守 agent 锁，因此这仍不是操作系统级的通用 CAS。

## 5. 迁移与兼容

| 旧路径 | 新路径 |
|---|---|
| artorias_assistant.bootstrap / main | artorias_local_app.bootstrap / main |
| domain.ids、ports、domain.ports | artorias_shared.ids、ports、ports.core |
| workspace.service / blobs / export_legacy | artorias_device.workspace.local_workspace / blobs / export_legacy |
| tools.sandbox | artorias_device.tools.sandbox |
| workspace.connectors.managed | artorias_local_app.managed_workspace |
| scheduling | artorias_server.scheduler |
| web/index.html、web/static | artorias_web/index.html、css、js |
| 其他业务目录 | artorias_server 下同名目录 |

旧 Python 包已删除，不保留导入兼容壳；外部脚本需更新 import。命令行入口、API 路径、数据根目录和 Task 表结构不变。静态文件 URL 变为 `/static/js/*`、`/static/css/*`，index.html 已同步更新。

升级前停止旧进程并备份 data；替换源码后执行 `uv sync --extra dev`，再运行 `uv run artorias-assistant`。不移动已有 app.sqlite、checkpoints.sqlite、blobs 或用户工作目录。历史导出命令改为 `python -m artorias_device.workspace.export_legacy`。

历史设计文档中的旧路径属于对应版本，当前包布局以本文为准；Task 规则仍以 task_design.md 为准。本次没有实现混合检索或修改 Task 的业务变更算法。

## 6. 验收与后续部署

验证范围：原有本地行为测试、AST 包依赖约束、禁用服务端/LLM 导入后的设备独立读写与撤销、无连接器不回退、远端客户端委托与位置校验、执行器选择、API 不自行启动 worker、wheel 静态资源及安装后运行入口。

后续顺序：

1. 将 catalog 的本机默认策略改为可注入的部署策略；补齐租户/设备身份及授权契约，设备端独立验证请求范围。
2. 实现设备网关和 RPC、云端对象存储 connector，以及异步传输、幂等操作 ID、版本前置条件和断线结果核对。
3. 将共库的设备事件/变更改为 outbox 同步；服务端仅持有变更索引及 BlobRef，按需下载备份。
4. Repository 从 SQLite 演进到共享数据库；scheduler 引入租约/fencing 与取消控制协议，再提供独立多 worker 启动器。当前单 worker 的 recover 不能直接用于多个进程。
5. 根据依赖体积拆成可独立安装的 distributions。本次仅拆源码包，安装依赖仍为项目统一依赖。

本地 token/Host/Origin 策略保持原行为，不是互联网用户认证。实际模型供应商调用与 Docker 执行需在用户环境验证；自动测试使用可控模型和执行器。

本次验证结果：51 项 pytest 测试通过；wheel 构建成功，并在独立安装目录验证五包导入、应用 lifespan、页面与静态资源、两个启动入口的参数解析。未调用真实外部模型或运行 Docker 容器。
