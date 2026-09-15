# Skills 注册表

采用「搜寻 + 沉淀」双机制：社区 skill 经验收后安装进本目录；每个阶段结束时由开发
Agent 沉淀可复用工作流。每条 skill 记录用途、来源、适用阶段和最后验证时间。

约束（§14.1）：用于用户侧 Agent 的 skill 必须通过 citation_verifier 与 safety_guard
校验；skill 内不得内嵌车辆事实（事实一律来自数据库与工具返回值）。
交付流程类 skill（如 git-branch-sync）属于开发侧，不进入用户侧 Agent 的 skill 注入集。

| 名称 | 用途 | 来源 | 适用阶段 | 最后验证 |
|---|---|---|---|---|
| [data-import-validation](data-import-validation.md) | 数据采集与校验（枚举/必填/来源冲突） | 阶段 3 沉淀 | 3（数据管线） | 2026-09 |
| [param-normalization](param-normalization.md) | 参数归一化（单位/工况/缺失值） | 阶段 6 沉淀 | 5/6（详情/对比） | 2026-09 |
| [sku-comparison](sku-comparison.md) | SKU 对比表生成与隐藏相同参数 | 阶段 6 沉淀 | 6（对比） | 2026-09 |
| [recommendation-explanation](recommendation-explanation.md) | 推荐解释话术（引用/妥协项/禁编造） | 阶段 7 沉淀 | 7（Agent） | 2026-09 |
| [constraint-integrity](constraint-integrity.md) | 约束完整性：用户声明的硬约束不得丢、盘点类问题必须读库 | 2026-09-14 品牌约束事故复盘 | 7（Agent）/ 9（迭代） | 2026-09 |
| [e2e-verification](e2e-verification.md) | 端到端验证流程（冒烟/压测/审查） | 阶段 8 沉淀 | 8（评测上线） | 2026-09 |
| [git-branch-sync](git-branch-sync.md) | 分支同步 / 叠放分支变基 + 命令与密钥卫生 | 2026-09 分支拆分与事故复盘 | 全阶段（交付流程） | 2026-09 |
| [doc-sync](doc-sync.md) | 代码更新后文档自动核对与对齐（含可执行检查 doc_sync_check.py） | 2026-09-15 文档对齐轮（计数漂移/悬空引用/口径冲突/UTF-16 事故） | 全阶段（交付流程） | 2026-09 |
