# v0.8：可提交产物的本地 Python 执行

本次实现 run_python 设计的本地隔离与工作副本链路。版本从 0.7.0 升至 0.8.0，现有启动命令不变。自动迁移增加两张执行表；不移动旧数据库、Blob 或用户文件。

## 运行前准备

```bash
uv sync --extra dev
docker pull python:3.12-slim
uv run artorias-assistant
```

Docker 必须已启动，镜像必须预先存在；镜像缺失时工具明确报错，不自动联网拉取。可通过 ARTORIAS_IMAGE 指向预装了 pandas、openpyxl 等依赖的自建 Python 镜像；审批固定镜像内容 ID。默认镜像仅含 Python 标准库。

## 工具请求示例

先通过 project_write 保存脚本，或指定已有 Python 文件。默认不复制整个 workspace；访问数据/代码依赖需要显式声明。

```json
{
  "path": "scripts/report.py",
  "args": ["/inputs/sales.csv", "/outputs/report.csv"],
  "inputs": [{"resource": "data/sales.csv", "mount_path": "sales.csv"}],
  "output": {"target": "reports", "overwrite": "deny"},
  "timeout_seconds": 120
}
```

脚本 argv 按数组传入，不经过 shell。只读数据在 /inputs、代码在 /app、临时计算在 /work，交付文件必须写到 /outputs。支持 workspace://ID/path 和已授权本机绝对资源引用。最终输出由 workspace 路由，容器不能直接写入宿主机目录。

已有文件覆盖示例：

```json
{
  "path": "report.py",
  "output": {
    "target": "reports",
    "overwrite": "if_unchanged",
    "expected_files": ["report.xlsx"]
  }
}
```

expected_files 在执行前捕获目标版本；未知名称只允许新建。审批后输入改变返回 stale_plan，执行期间目标改变返回 conflict/partial。不会静默另存或覆盖用户改动。

工程修改示例：

```json
{
  "path": "tools/update.py",
  "mode": "workspace_copy",
  "working_directory": "project",
  "code_directory": "tools",
  "allow_delete": false,
  "output": {"target": "reports"}
}
```

project 副本放到 /work，代码依赖目录放到 /app。结束后提交副本的内容新增/修改；删除必须显式 allow_delete 并通过审批。自动排除 .git、.venv、__pycache__、.ssh、.aws、.env 和 .env.*；被排除的文件不会被判定为删除。权限位变更不提交，只处理文件内容变化。

output.publish=stage_only 时不写用户资源，只保留可下载产物。默认 output.target=artifacts/<execution_id>/；如果用户指定目标，遵循指定位置。

## 结果、审批与恢复

网页审批展示脚本哈希、输入资源、执行位置、固定镜像、网络、时限、目标及删除权限。仍逐次审批，没有自动复用 Task 执行授权。

execution_status 与 publish_status 分开；脚本成功不代表文件已提交。网页显示结果并提供二进制产物下载。工具响应只返回日志末尾和前 30 项清单，完整结果通过认证 API 获取。

- `GET /api/sessions/{sid}/executions/{eid}`：状态和完整结果。
- `GET /api/sessions/{sid}/executions/{eid}/artifacts/{aid}`：下载暂存产物。
- `GET /api/sessions/{sid}/executions/{eid}/logs`：有界日志。

这些接口沿用本地 token/Host/Origin 保护，并校验执行所属 Session；不是云端多租户认证。

持久记录位于 app.sqlite 的 python_executions、execution_publications；输入快照、候选产物、日志位于 data/executions。正常完成后清理输入和工作目录，交付产物保留。用户目标文件提交后仍由原 workspace Blob 机制备份和撤销。

启动时尝试清理中断执行对应的容器，将未知执行标记 unknown，不重跑脚本；已经 collected 的产物可以继续幂等提交。容器名为 artorias-<execution_id>，清理失败时结果中标记 container_stopped=false。原 Run 继续沿用现有 interrupted 策略，用户需核对结果后继续。

## 边界与资源限制

- 本地单 worker；执行期间以异步等待让出事件循环，但不会释放该业务 worker 去调度另一任务。
- 无远端 Python 后端、长任务持久挂起唤醒或 direct_mount。
- 默认 120 秒，可请求 1–300 秒；256MB 内存、1 CPU、64 PID、无网络、非 root、只读容器根文件系统。
- 输入/最终候选产物分别最多 500 文件、96MB；单文件 32MB；容器 /work 128MiB、/outputs 64MiB、/tmp 32MiB tmpfs，实际受总内存限制。
- stdout/stderr 各保留最后 64KiB并标注截断。超时不提交仍在变化的产物。
- 候选产物拒绝符号链接、硬链接、特殊文件和越界路径；Python 不获得宿主机可写挂载。
- 多文件提交不是原子事务；返回成功清单与冲突清单。外部编辑器不遵循 Agent 锁时，现有本地文件操作仍存在极窄检查到替换窗口，不宣称 OS 级 CAS。
- 暂无自动 TTL 清理、冲突另存 UI、自动依赖安装。产物会占用 data 空间，请在确认不再需要后自行清理对应执行目录。

## 验证

```bash
uv run pytest -q
ARTORIAS_TEST_DOCKER=1 uv run pytest tests/test_python_execution.py::test_local_docker_integration -q
```

测试覆盖二进制与撤销、输入映射、审批后输入变化、覆盖冲突、工作副本增删改、拒绝删除、stage_only、失败不发布、端点幂等、启动核对、产物下载的 Session 隔离、真实 DeepAgents 审批恢复、Docker 命令隔离约束与 tar 校验。

当前交付环境没有 Docker，真实容器测试默认跳过；其余测试使用可控后端与真实文件/数据库/Agent 图。请在本机启动 Docker 并拉取镜像后运行上面的可选测试。

本次自动验证结果：78 项通过，1 项真实 Docker 集成测试因环境条件跳过；wheel 构建及独立安装验证通过。
