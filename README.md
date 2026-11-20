# Artorias Assistant

基于 Deep Agents 的本地个人助理 Python 项目，Web 输入、聊天、受管文件读写和 Python 编码执行。

## 快速启动（macOS / Linux）

安装 Python 3.11+ 和 uv，解压并进入项目：

```bash
cp .env.example .env
# 编辑 .env：填写 ARTORIAS_MODEL_PROVIDER、ARTORIAS_MODEL 和对应厂商的 API_KEY
uv sync --extra dev
uv run artorias-assistant
```

打开 http://127.0.0.1:8765 。模型必须支持 tool calling；密钥只在后端读取。模型 API 兼容性仍须用你实际的供应商验证。

在左侧选择会话，右侧查看消息和运行事件。输入时按 Enter 发送，按 Shift + Enter 换行，拼音组词期间的 Enter 由输入法处理。输入框下方可以切换本机或云端工作空间。在 macOS 上，点击「浏览…」可通过系统窗口选择或新建本机文件夹。云端工作空间使用逻辑路径，例如 `personal/reports`。

运行代码需要本机安装并启动 Docker Desktop，然后预先下载镜像：

```bash
docker pull python:3.12-slim
```

普通聊天、生成代码和文件操作不需要 Docker。首次代码执行会在网页显示审批。示例输入：

- “你好，介绍一下你的能力。”
- “创建 notes.md，记录今天的学习计划。”
- “把 notes.md 的学习计划补充一项 Python 异步。”
- “创建 hello.py 打印 42，然后运行它。”

## 实现范围

- 真实 `create_deep_agent` 引擎，DeepSeek、OpenAI、Anthropic 原生模型适配。
- FastAPI REST + fetch SSE，事件持久化后发送、断线按游标补读。
- SQLite 会话、消息、Run、Job、Event 和文件变更；请求幂等。
- 一个输入对应一个 Turn ID 和一个 Run，Task 关联/拆分后按依赖顺序执行。
- project_list/read/write/edit/delete 工具；不可变内容 Blob、前后版本、事件关联、冲突安全撤销。
- run_python：默认隔离执行；可选工作副本。声明输入，脚本写入 /outputs，产物经 workspace 授权和版本检查提交；支持二进制、冲突保留与下载。Docker 无网络、非 root，默认 120 秒、上限 300 秒。
- 编码执行使用 LangGraph interrupt 审批，恢复使用同一个 Run/thread；拒绝返回工具结果。网页可批准、拒绝、取消。
- 本地单用户访问令牌、Host/Origin 检查；默认只监听 loopback。不要直接暴露公网。

## 模块边界（v0.7）

| 包 | 职责 |
|---|---|
| `artorias_shared` | 领域消息、审批、工具、workspace 和执行协议 |
| `artorias_device` | 本地文件、Blob、变更日志和 Docker sandbox |
| `artorias_server` | API、SSE、Task、scheduler、runtime、模型、工具分发、workspace 路由和服务端存储 |
| `artorias_web` | HTML、CSS、JavaScript 静态资源 |
| `artorias_local_app` | 本地一体化依赖组装与启动 |

`data/` 保留在项目根目录，存放运行时数据库、Blob 和受管文件，不属于 Python 包，不提交 Git。
详见 [v0.7 模块拆分设计与迁移说明](docs/module_split_v0.7.md)。

本地启动命令保持 `uv run artorias-assistant`。服务端不导入设备实现；远端连接器需要显式注入客户端，无连接器时返回不可用，不会退回服务器本机文件系统。

## 与设计文档的明确裁剪

1. Session 选择默认 Workspace，新 Task 继承提交时的选择；旧 Task 和审批恢复保留绑定。可选择本地目录或单机云端逻辑空间。仅单用户模式，不是多租户隔离。
2. Job 在本地全局串行处理，避免同一状态并发写。等待审批后可以处理其他会话。同会话的新请求等待审批完成或取消。
3. 每 Run 一个 checkpoint thread；后续 Run 从已完成的会话语义消息构建 context，保留最近 30 条。不是设计稿中的 session thread；长历史检索、摘要和完整 context manifest 待扩展。
4. 本项目文件用 project_* 工具持久化；Deep Agents 自带文件后端保留为内部草稿空间，不能把内部草稿误认为用户项目文件。
5. 磁盘工作目录是权威内容；Blob 保存修改前后备份。持久操作日志协调磁盘与数据库，中断后按哈希核对；不确定状态阻止同路径继续写入。详见 workspace_design.md。
6. 等待审批的 Run 可在重启后再次审批恢复；正在执行时崩溃则标记 interrupted，不自动重放不确定的工具动作。后续发新消息继续。尚未提供任意节点自动恢复。
7. Run、Turn 位于 runs 表；Task、执行计划与上下文快照已持久化。Artifact 仍以文件 hash 和 Change ID 表达。
8. 文件访问检查持久目录授权和 Run 授权；外部目录通过 project_authorize 审批，Python 执行逐次审批。邮件、购物、社交、搜索、MCP、长期记忆未接入。
9. 默认不提供任意 shell、依赖安装、网络访问。编码环境只带镜像中的 Python；更换镜像由本机管理员配置。

`project_tree` 用于有上限地浏览目录。默认深度 2、最多 200 条、隐藏项不显示；条目上限可设为 1–1000。只有达到条目上限并省略了条目时，才返回 `truncated: true`。 `project_list` 完整列出单层目录里的文件和文件夹，包含隐藏项。两者都跳过符号链接。可使用工作空间相对路径、本机绝对目录或 `workspace://ID/目录`。未授权的本机目录可通过 `project_authorize(path=..., access="read")` 申请本次运行的只读访问。模型使用 `project_*` 操作真实文件，保留的内置 `read_file` 只读取框架引用的虚拟文件。

运行时工具通过 `WorkspaceToolExecutor` 分发。文件操作由目标工作空间的连接器执行；`run_python` 默认按绑定的工作空间选择 `device` 或 `cloud` Python 后端，并把选定位置写入审批计划。单机版默认注册本机 Docker 执行器。云端部署可注入符合 `PythonExecutionBackend` 接口的云执行器，例如在部署节点注册 `DockerPythonBackend(location="cloud")`，同时配置云端工作空间连接器。缺少对应执行器时会报错。

## 数据和排障

`data/app.sqlite` 保存业务记录；`data/checkpoints.sqlite` 保存引擎状态；`data/blobs/` 保存不可变文件内容。目录不会进入源码包。

查看 [事件清单和字段说明](docs/event_catalog.md)。如需重置本地数据库及 Blob，先停止 Artorias 服务，然后运行：

```bash
uv run python scripts/reset_local_data.py          # 预览清理路径
uv run python scripts/reset_local_data.py --apply  # 确认后永久清理
```

脚本使用 `ARTORIAS_DATA`，也可用 `--data /path/to/data` 指定。它删除业务数据库和检查点数据库（含 SQLite 辅助文件），以及本地和云端逻辑空间的 Blob。重新启动后会创建空库。工作空间里的实际文件与 `executions/` 下的产物文件会保留，但历史会话、变更记录及产物的下载索引会丢失。

模型错误会显示 run.failed；检查模型名、key、base URL 与 tool calling 支持。Docker 错误检查 Docker Desktop 和预下载镜像。依赖直接版本已按生成环境固定；附带 uv.lock 固定传递依赖，可用 uv sync --locked 安装。

`project_read` 按页返回内容。续读时把上一页的 `next_offset` 和 `version` 传给 `offset` 和 `expected_version`。文件内容变更后从第一页重读。一次请求超过 `ARTORIAS_REQUEST_MAX_BYTES` 时，运行时会先压缩发给模型的旧工具记录，再检查预算。会话事件仍保留记录的载荷，可在工具卡片的「详情 JSON」中查看。

执行期间浏览器断线不会取消 Run。明确点击停止会请求取消；已提交文件不会自动回滚，可从变更记录撤销。

## 测试

```bash
uv run pytest -q
```

测试包含文件隔离、路径穿越、版本撤销冲突、请求去重、崩溃标记、真实 Deep Agents 工具循环（模型替身）及 Web 调度闭环。没有附带真实模型密钥；在线供应商调用及 Docker 实机执行需在你的环境验收。没有宣称这些外部集成已通过验证。

参考：[Deep Agents 官方文档](https://docs.langchain.com/oss/python/deepagents/quickstart)。产品与架构背景见 docs/ 中的三份配套设计。


## v0.2 模块化服务与模型配置

这里的服务是进程内的独立职责模块，不是独立部署的微服务。启动方式不变。

```text
src/artorias_assistant/  # v0.2 历史布局；当前布局见 v0.7 文档
  main.py                   CLI 入口
  bootstrap.py              依赖组装
  api/app.py                REST / SSE / HTTP 鉴权
  application/service.py    提交消息、取消和审批用例
  scheduling/service.py     作业调度
  runtime/service.py        Deep Agents 执行
  model_provider/
    settings.py             配置解析与校验
    factory.py              厂商适配器选择
  tools/registry.py          工具注册
  workspace/service.py      文件版本与变更
  storage/sqlite.py          SQLite Repository
  domain/ports.py            基础接口
  web/index.html             网页入口
```

Runtime 依赖 model_provider 工厂，不再直接依赖 ChatOpenAI。API 写用例经过 application；文件及查询接口经 WorkspaceService 路由，拆成独立进程前需进一步替换为 RPC 和 Repository 接口。模块化完成不等于分布式一致性已完成。

| ARTORIAS_MODEL_PROVIDER | 模型适配 | 密钥变量 | 地址变量 |
|---|---|---|---|
| deepseek | ChatDeepSeek | DEEPSEEK_API_KEY | DEEPSEEK_BASE_URL |
| openai | ChatOpenAI | OPENAI_API_KEY | OPENAI_BASE_URL |
| anthropic | ChatAnthropic | ANTHROPIC_API_KEY | ANTHROPIC_BASE_URL |

统一通过 ARTORIAS_MODEL 设置你账号实际可用且支持工具调用的模型 ID。三家的密钥和地址互相隔离；Anthropic 不经过 OpenAI 协议转换。供应商设置修改后重启服务；不要在尚有待审批 Run 时切换模型，应先完成或取消原 Run。聊天和工具调用共用选定模型。Anthropic 文本块转换为统一文本事件，thinking/tool 内容不拼接为用户回答。

模型构造测试不发送网络请求；没有厂商密钥，未完成三家在线 API 验收。

参考：
- https://reference.langchain.com/python/langchain-deepseek/chat_models/ChatDeepSeek
- https://reference.langchain.com/python/integrations/overview

## v0.3 Task 支持

默认消息入口已集成 Task 关联/拆分、顺序消费、独立执行范围与持久上下文快照。详见 [设计文档](docs/task_design.md)。任务交付标为 needs_review，不自动断言目标完成。


## v0.4 历史行为（v0.5 空间绑定规则见下文）

在 `.env` 中指定目录（路径含空格时加引号）：

```dotenv
ARTORIAS_WORKSPACE=/Users/yourname/Projects/my-workspace
```

重启后创建新会话，网页会显示绑定的绝对目录。让 Agent 创建 `notes.md`，会直接生成该目录下的真实文件。已有会话保留原绑定；未设置时使用 `data/workspaces/<session_id>/`。允许相对路径或根目录内的绝对路径，拒绝 `..` 和符号链接。应用数据目录不能位于工作目录内。

已有文件可直接读写。工具覆盖已有文件前要求先读取；每次修改/删除备份磁盘实际内容；网页“变更记录 → 撤销”恢复真实文件。若文件已被外部编辑，则拒绝冲突撤销。文本读写为 UTF-8、单文件上限 1 MB；二进制可在此上限内备份、删除及恢复。Docker 仍运行临时只读副本，输出文件不会自动回写。

旧版本 Blob 数据不会自动写进本地目录。升级前先停止旧服务并备份整个 data，使用新版本命令导出旧会话的最新内容：

```bash
uv run python -m artorias_device.workspace.export_legacy \
  --data ./data --session <旧会话ID> --destination /Users/yourname/Projects/exported-old-session
```

目标目录必须不存在。随后将 ARTORIAS_WORKSPACE 指向导出目录，重启并新建会话。旧变更记录保留，但不能用于撤销新目录中的文件；新操作建立自己的备份历史。

详细设计、恢复办法、并发边界：[workspace_design.md](docs/workspace_design.md)。


## v0.5 多 Workspace 与资源路由

完整规范：[Workspace 产品与工程设计](docs/workspace_design.md)。

- 网页“添加空间”支持本地目录与云端逻辑空间；“切换空间”只影响新任务。
- 默认本地目录改为 data/workspaces/default；旧会话根目录原样保留。
- 默认云端逻辑空间 personal 由本服务的 data/cloud/files 承载，并非自动上传互联网。
- Task 继承输入提交时的空间，继续旧 Task 不跟随 Session 切换。
- 相对文件路径使用 Task 空间；明确本机绝对路径可访问已授权目录；workspace://ID/path 可指定其他空间。
- 缺少权限时 project_authorize 申请当前 Run 的目录访问权限；支持授权列表和撤销 API。
- 本地文件备份放 data/blobs；托管云端文件备份放 data/cloud/blobs。回滚按原位置，不随空间切换。
- ConnectorRegistry 可以接入设备 RPC / 对象存储适配器；当前尚无真实远程设备连接器。

升级前停止旧服务并备份整个 data。用新代码启动时自动增加关系表，旧真实目录与备份不移动。改变 ARTORIAS_DEVICE_ID 不会重命名已经登记的设备。旧待审批 checkpoint 如果含旧工具 schema，建议升级前完成或取消；不保证跨版本 checkpoint 兼容。

本版不支持直接把 data 本身或其祖先/后代授权给模型；请选择独立项目、Downloads 等目录。新增云端空间输入逻辑前缀，例如 reports；其物理路径由服务管理。

## 云端代码演进

- [云端目标架构代码拆分计划](docs/cloud_code_split_plan.md)：基于 v0.5 源码的模块边界、部署拆分、迁移顺序与验收标准。

### v0.5.1 拆分进展

Task 已拆为 models/planner/repository/context/executor；文件操作位于 tools/files.py，Docker 执行位于 tools/sandbox.py，Workspace 适配器按位置分文件。DeepAgentRuntime 支持注入 workspace_factory、model_factory、checkpoint_provider、tool_factory，bootstrap 负责当前本地实现装配。旧 Task 导入路径继续兼容。

当前仍是本地单进程版本；托管云空间仍为单机目录，不是已完成的云端部署。真实远程资源调用、共享数据库与多 Worker 属于后续阶段。


## v0.6 升级

Web 模块拆分、全表时间迁移及 Task 需求版本化已实现。升级前备份数据；实施范围与 Git 恢复/推送步骤见 [版本说明](docs/release_v0.6.md)。


## Python 执行能力设计

[run_python 功能设计方案](docs/run_python_design.md)：职责边界、隔离/工作副本模式、接口、权限、调度恢复和产物提交。v0.8 已落地本地隔离/工作副本与产物提交；实现状态及使用方式见 [v0.8 发布说明](docs/release_v0.8.md)。
