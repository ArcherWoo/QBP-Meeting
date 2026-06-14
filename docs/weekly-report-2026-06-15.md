# QBP Meeting MGMT 近期工作周报素材

日期：2026-06-15

## 本周主要完成

- 重构会议与议题的业务隔离维度：将硬编码 BP Version 改为数据库维护的 `Plan Version`，并新增归属于 Plan Version 的 `Round`。
- 建立会议与议题的三维匹配规则：`Plan Version + Round + 类别` 必须一致，避免 Q3/Q4、不同轮次、不同会议类型之间串议题。
- 优化会议创建、会议列表、议题池、议题编辑、审批和议程看板的字段展示与筛选，使 Plan Version、Round、类别在关键流程中保持一致。
- 增加 Plan Version 和 Round 的动态新增能力：管理员或 QBP 角色可在表单内快速创建新版本或下一轮次。
- 调整权限与业务组体系：保留 QBP 品牌，将管理团队调整为 `PLN/BP`，并将业务组改为 `MC`、`OP`、`PDC`、`TD`、`PLN/SP`、`PLN/NPP`、`PLN/BE IE`、`PLN/AP CIE`。
- 将 AI Review 提示词管理和知识沉淀从按 BP Version 管理，调整为按业务组和业务分类管理。
- 优化 AI Review 提示词库页面布局：知识来源改为 `通用经验 + 业务部门 + 业务分类` 的清晰结构，提升配置可读性。
- 优化 AI 知识沉淀页面：新增知识默认启用，启用/停用只在知识沉淀池内维护，减少新增时的操作负担。
- 将系统示例数据从采购场景改为 QBP 场景：示例会议改为 POR Review，示例议题包括 `OP Cum Yields`、`NPP New Product CS时间`、`BE IE产能扩建`。
- 做了一轮 UI 密度和对齐优化，包括会议列表、议题池、创建会议页面、提示词管理页面、知识沉淀页面的按钮、下拉框、表格列和表单布局。
- 优化离线依赖发布方式：`wheels/` 不再作为源码逐个提交，改为打包为 `wheels.zip` 放到 GitHub Release，避免仓库过大和 push 超时。
- 补充本地 embedding 模型目录：新增 `embedding_model/sentence-transformers/all-MiniLM-L6-v2/` 占位路径，便于后续部署离线向量检索模型。

## 可写进周报的亮点

- 完成 QBP Meeting 的核心数据模型升级，从“单一版本字段”升级为“版本 + 轮次 + 会议类型”的完整业务边界。
- 解决了不同 BP 周期和不同 Round 之间议题误选、误审、误加入会议的问题。
- AI Review 的知识来源从版本维度切换到业务组织维度，更贴近实际业务分工，也方便不同团队沉淀各自经验。
- 将新增知识的操作路径简化为“输入内容、选择分类、添加”，状态管理集中到知识池，降低录入成本。
- 完成 GitHub 轻量化发布改造，源码仓库保持干净，离线依赖和大模型文件改走独立分发路径。

## 后续建议

- 将 `wheels.zip` 上传到 GitHub Release，README 已说明离线安装时从 Release 下载并解压。
- 准备本地 embedding 模型文件，放入 `embedding_model/sentence-transformers/all-MiniLM-L6-v2/`，用于离线材料向量检索。
- 后续如需要更细权限，可在现有业务组基础上继续设计“业务组 + Plan Version”的授权模型。
