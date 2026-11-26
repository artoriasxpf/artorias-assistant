# Workspace 工具与代码项目操作

## 工具清单

| 变更 | 工具 | 职责 |
|---|---|---|
| 修改 | project_move | 同一次审批列出源、目标缺失的写权限。确认后通过连接器重新校验路径和权限，再执行原生移动，保留 change_id |
| 修改 | project_inventory、project_search、project_tree | 接受 include_hidden，返回过滤条件及扫描范围。普通清点和搜索报告 coverage，快照分页记录排除项，树保留原 truncated 字段并返回 stop_reason |
| 修改 | project_edit | 保留唯一字面匹配，增加 expected_version 和结构化的版本、零匹配及多匹配冲突 |
| 修改 | read_file | 说明只读框架给出的虚拟引用。真实文件使用 project_read |
| 新增 | project_mkdir | 在本机连接器创建目录，接受 path 和可选 parents。重复创建返回 already_exists，同名文件返回 conflict |
| 新增 | project_change_plan | 用显式文件名单生成版本化替换或补丁，预览 diff 并返回 plan_id |
| 新增 | project_change_apply | 应用计划，预检权限和版本，写入时再次比较版本，逐文件记录结果 |
| 删除 | 无 | 保留现有读、写、删、树、清点等工具入口 |

源和目标必须位于支持原生移动的同一本机设备内，目标父目录需要先创建。移动支持现有撤回入口，撤回时仍校验所需权限。mkdir 的 parents 只创建授权边界内缺失的祖先，返回实际创建的目录。空目录创建在当前版本没有变更撤回记录。

搜索保留 name、literal、regex 三种模式。搜索承接普通 inventory_id 时默认继承其隐藏项策略，也可以显式指定。快照读取保持原有清点或字面搜索能力，支持 include_hidden，端点会校验游标和原过滤条件。include_hidden 不取消其他内置排除规则。扫描结果只描述实际遍历的范围。

## 统一变更协议

生成计划所需字段如下。范围使用读取版本中的 Unicode 字符偏移，起点为 0，终点不包含在范围内。

| 请求类型 | 输入 | 行为 |
|---|---|---|
| replace | request.kind、files[{path,expected_matches,expected_version?}]、pattern、replacement、mode | 逐文件验证预期匹配数。literal 为默认，regex 支持捕获组替换且限制匹配时间 |
| patch | request.kind、files[{path,expected_version,edits:[{start,end,text}]}] | 在同一基线版本上处理非重叠编辑，拒绝无效范围及别名重复目标 |
| apply | plan_id | 使用已保存的不可变内容，重新检查实际文件身份及版本 |

预览使用读权限，应用使用写权限。分析 Step 可在 analysis roots 内预览，并通过交付把 plan_id 传给依赖写入 Step。单点读、搜索及证据记录仍走现有 Analysis Memory。

计划保存于 SQLite 的 project_change_plans。每条记录绑定 Session 与 workspace，条目包含规范化的资源身份、基线版本和预览后内容。有效期为 24 小时，新的计划写入时清理过期记录。每份计划最多 8 个既有 UTF-8 文件，每个输入或结果最多 1 MB。预览太大时返回原因，可缩小计划范围。模块使用明确的目标列表，因此无法仅凭一个广泛的搜索条件触发批量写入。

应用先预检整批，若有冲突则暂不启动任何待执行的写入。每个文件使用版本化写入和稳定的操作 ID。并发修改可能导致执行途中部分成功，结果返回逐项状态及已提交条目的 change_id。重跑同一计划会识别已提交条目，撤回走现有的逐文件变更记录。撤回后要再次应用同样的编辑，需要重新生成计划。

## 代码项目扩展

| 能力 | 接口方向 |
|---|---|
| 文本搜索与上下文读取 | 继续使用 project_search 和 project_read_many。文本命中作为候选定位 |
| 语义查询 | 后续接入语言服务，project_reference 可按 markdown_link 或 code_symbol 分派。代码查询输入路径、位置和查询类型，输出资源、范围、关联版本和覆盖限制 |
| 代码检索的分析记忆 | 语义结果可作为可追溯的来源记录。文件内容仍通过当前 workspace 读取和审批入口取得 |
| 语义重构 | 后续将 rename_symbol 等语言服务输出转换为同一套版本化编辑。适配层将服务协商的坐标编码转换为字符范围，校验编辑涉及的资源与版本，再送入统一计划 |
| 文件级变更 | 首版批次只修改既有文件。未来需要在计划中显式区分创建、移动、删除，保存原生变更 ID 和分项结果 |

LSP、Markdown 引用查询以及批量移动和删除属于后续扩展。首版可完成文本检索、单点修改、按范围编辑和跨文件文本替换。
