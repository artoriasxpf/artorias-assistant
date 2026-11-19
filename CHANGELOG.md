# Changelog

## 0.6.0 — 2026-11-19

- Web 拆分为 HTML、CSS 和四个 ES Modules，补齐静态资源路由。
- 所有业务表与 checkpoint 表增加时间字段、兼容迁移及自动更新时间。
- Task 增加版本化需求 patch、来源和执行快照、幂等交付及累积进展。
- 上下文采用 Step 成果和分页引用，增加规划及运行时请求大小保护。
- Git 初始化并保存提交历史；远程推送因未登录尚未执行。
- 详细状态与限制见 docs/release_v0.6.md。

## 0.5.1 — 2026-11-18

- 开始云端拆分第一阶段：Task 分模块，保留原导入兼容。
- 提取 FileToolService、LocalDockerExecutor、WorkspaceFactory 和 CheckpointProvider。
- Runtime 支持注入模型、Workspace、工具和 checkpoint；bootstrap 显式组装本地实现。
- 本地与单机托管 Connector 分文件；Workspace 不再依赖 LangGraph interrupt。
- 补充本地/云端/跨端资源与工具路由设计。
- 36 项测试通过；未接入真实云存储、设备 RPC 或分布式调度；未运行真实模型 API / Docker / macOS 实机测试。

## 0.5.0 — 2026-11-17

- 新增 Workspace/Location/Grant、Session/Task/Run 绑定关系与提交时快照。
- Runtime 按当前 Task 选择空间，审批恢复不受 Session 切换影响。
- 文件工具通过 WorkspaceService 路由，支持本地绝对路径及 workspace:// 引用。
- 新增本地与托管云端空间适配器、Connector/FileOperations 和 BlobStore 协议。
- 外部目录通过 project_authorize 审批后获得 Run 范围权限，支持授权撤销 API。
- 变更记录包含实际资源与 Blob 位置；切换空间后仍可按原地址撤销。
- Web 增加空间注册和切换；默认云端空间在单机服务目录实现。
- 兼容 v0.4 本地目录和操作历史，不自动搬迁数据。
- 34 项测试通过，包含真实 Deep Agents 引擎和模型替身，不包含真实 API / Docker / macOS 实机测试。

远端设备 RPC、对象存储、多租户、跨设备备份同步与 Blob GC 尚未实现。
