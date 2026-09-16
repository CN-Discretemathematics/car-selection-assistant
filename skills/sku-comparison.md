# Skill：SKU 对比表生成（阶段 6 对比模块）

- 用途：生成 SKU 对比表，隐藏相同参数，支持分享；对比场景不含任何外部跳转入口。
- 来源：阶段 6 沉淀（对应 `backend/app/comparison/router.py`、`web/app/compare/page.tsx`）。
- 适用阶段：6（SKU 对比）。
- 最后验证：2026-09（对比入口口径变更后复跑：`test_api.py::test_comparison_lifecycle_and_common_params`
  + `test_detail_entry_contract.py`）。

## 流程

1. 对比对象必须是具体 SKU（variant_id），最多 5 个；创建时校验在售状态（停售拒绝）。
2. 后端 `POST /api/v1/comparisons` → `GET /api/v1/comparisons/{id}`：
   - `variants[]`：每个 SKU 的配置事实（含官方指导价；**不含**任何外部跳转入口）；
   - `common_params[]`：归一化后所有 SKU 相同的参数（「隐藏相同参数」数据源）。
3. 前端对比页按参数分类渲染，默认隐藏相同参数，可切换显示；提供分享链接复制。
4. 缺失值显示「官方资料未披露」；不同工况不直接比较（页脚提示）。

## 约束

- 游客分享走 URL 无状态（`/compare?variant_ids=`）；登录用户可保存（comparisons 表）。
- **对比场景不展示任何外部跳转入口**（官方车型页与数据来源页都不提）：官方链接
  `official_page_url` 当前为空（唯一来源汽车之家不提供该字段，见 `docs/deployment.md` §8），
  用户要求对比时避免提及这类信息——因此对比接口的 `CompareVariantOut` **不返回**
  `official_page_url`，对比表也不渲染链接。唯一入口在**详情页**：`external_link`
  （`kind=official|source`，官方优先，判定在后端，缺失时回退「查看数据来源」，
  由 `external_series_refs` 的来源名 + 外部 id 确定性拼出，**不得冒充品牌官网**）。

## Agent 侧外部入口（2026-09-16 已删除）

用户口径：「官方链接能轻松收集就保留，不能就删掉，以汽车之家与已入库数据为准」。
实测结论是**不能轻松收集**（唯一来源汽车之家不提供该字段，见 `docs/deployment.md` §8），
因此已删除 Agent 侧全部官方链接面：
`app/agent/tools.py` 的 `official_link_tool`（连同 `TOOL_SCHEMAS` 里的一项与引擎分发）、
`AgentMessageOut.official_links`、`RecommendedVariant.official_page_url`、
`web/app/components/AgentChat.tsx` 推荐卡片的「官方车型页 ↗」链接，
以及 `build_variant_diff_answer` 里「可先到品牌官网查看配置表」与同级别小结里
「或到品牌官网查看具体款型配置表」**两处**指向品牌官网的建议语
（免责声明「具体以品牌官网为准」保留，与全站 footer 一致）。
Agent 现在只用库内事实回答，不提供任何外部跳转。详情页 `external_link` 保留（数据驱动）。

已知但未改（口径不彻底处，非用户可见）：`retrieval_search` 的出参里仍有 `source_url`
（RAG 切片元数据，线上该字段恒空），它会随证据一起进入 LLM 上下文，但系统提示明确
「不要输出来源 id 或链接」，且面向用户的 `Citation` 只有来源名与标签，不渲染 URL。
