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

## 待产品确认项（Agent 侧外部入口）

Agent 协议里仍有 `AgentMessageOut.official_links` 与 `RecommendedVariant.official_page_url`
（`backend/app/agent/engine.py` 推荐 / 同车系版本罗列 / 车系问答三个分支在填），
前端只有 `web/app/components/AgentChat.tsx` 的推荐卡片会渲染 `official_page_url`
（顶层 `official_links` 前端从未渲染）。当前数据恒空 → 实际不显示；
**一旦上游补上官方 URL，推荐卡片会自动重新出现「官方车型页 ↗」**。
是否把「对比时避免提及」也扩展到 Agent 侧，属产品口径问题，未擅自改动——
要改的话，删除面是：AgentChat 卡片链接 + 后端三处填充 + 工具 `official_link_tool`。
