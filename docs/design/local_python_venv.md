# macOS 本地 Python 执行

本地启动器默认在 macOS 选择 uv + venv + Seatbelt，在其他系统选择现有 Docker 后端。设置 `ARTORIAS_PYTHON_BACKEND=docker` 可继续使用镜像。云端执行由服务启动时注入的后端承担。原生执行依赖可用的 `uv`、本机 Python 和 `/usr/bin/sandbox-exec`。

## 执行与依赖

| 层 | 职责 |
| --- | --- |
| Runtime 工具 | 接收脚本、参数、声明输入、产物策略与可选 `dependencies` |
| ExecutionService | 解析 workspace 与执行位置，固定输入快照与目标基线，记录审批，收集和发布产物 |
| macOS 后端 | 准备依赖环境，生成 Seatbelt 策略，执行代码并等待子进程退出 |
| Workspace | 条件写入或删除、保存旧内容到设备 Blob、记录 change ID、处理撤回 |

声明依赖可用 `["openpyxl==3.1.5"]`，也可填写包名。首次批准后由 uv 从 PyPI 解析并保存含哈希的传递依赖锁，通过 wheel 安装至设备数据目录 `python-envs`。环境按 Python、uv、架构及依赖声明指纹缓存，发布标记只在安装成功后写入。包解析与安装在独立权限策略下联网，仅能写入环境准备目录。无依赖执行的环境准备与脚本执行均关闭网络。源码构建、项目自定义安装命令与包源暂缓支持。

执行前重新核对环境配置和输入版本。脚本执行时用本次临时目录作为 HOME，使用固定 PATH，并以只读方式访问 venv 和暂存输入。Seatbelt 限制可执行文件、文件访问及网络，脚本启动前验证代码目录写入和本地网络连接均受限。执行具有超时、单文件大小限制和有界日志。当前运行策略限制创建子进程，原生后端尚无与 Docker 等效的总磁盘、内存或 CPU 配额。

## 文件读写与回退

默认 `isolated` 只暂存入口、声明输入和可选代码目录。对于依赖相对路径的批量修改，`workspace_copy` 只暂存指定工作目录，运行后计算差异。脚本使用 `ARTORIAS_INPUT_DIR`、`ARTORIAS_OUTPUT_DIR` 和 `ARTORIAS_WORK_DIR` 访问这些路径。独立 argv 中的 `/inputs/…`、`/outputs/…`、`/work/…` 和 `/app/…` 会映射到当前执行目录。源码硬编码绝对路径需要调整。

例如要覆盖原有表格，声明其为输入，将新表格写入输出目录，在 `output` 设置目标目录、`overwrite=if_unchanged` 和 `expected_files=["sales.xlsx"]`。发布时校验用户文件仍是准备阶段的版本。冲突时保留候选产物；校验通过后由 Workspace 原子写入并在本机 Blob 保存前一版本。运行结果 `published` 包含每个目标的 `change_id`。从现有会话变更界面撤回，或调用 `POST /api/sessions/{sid}/changes/{cid}/revert`。如果目标后来又被修改，撤回会报告冲突。多文件执行按文件提交和撤回，可能部分成功。

首版沿用现有快照输入与单文件撤回，真实路径直接只读、按执行批量撤回及 Linux 原生隔离留待后续。进程重启后不会自动重跑状态不确定的脚本。安装器与脚本通过进程组在超时或取消时清理。运行超时后不会发布候选变更。

## macOS 验收

关闭 Docker 并启动 Assistant。执行声明了 openpyxl 的脚本，观察首次安装、再次执行复用和候选文件发布。验证未授权文件访问、联网和启动其他进程受到限制。在运行期间修改目标文件，确认发布报告版本冲突。再分别验证正常覆盖、新建和删除的撤回。Linux CI 可以验证编排和发布，Seatbelt 行为及 PyPI 安装需在 macOS 真机验证。
