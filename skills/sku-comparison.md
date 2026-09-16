# Skill：SKU 对比表生成（阶段 6 对比模块）

- 用途：生成 SKU 对比表，隐藏相同参数，支持分享与官方链接。
- 来源：阶段 6 沉淀（对应 `backend/app/comparison/router.py`、`web/app/compare/page.tsx`）。
- 适用阶段：6（SKU 对比）。
- 最后验证：2026-09。

## 流程

1. 对比对象必须是具体 SKU（variant_id），最多 5 个；创建时校验在售状态（停售拒绝）。
2. 后端 `POST /api/v1/comparisons` → `GET /api/v1/comparisons/{id}`：
   - `variants[]`：每个 SKU 的配置事实（含官方指导价与 `official_page_url`）；
   - `common_params[]`：归一化后所有 SKU 相同的参数（「隐藏相同参数」数据源）。
3. 前端对比页按参数分类渲染，默认隐藏相同参数，可切换显示；提供分享链接复制。
4. 缺失值显示「官方资料未披露」；不同工况不直接比较（页脚提示）。

## 约束

- 游客分享走 URL 无状态（`/compare?variant_ids=`）；登录用户可保存（comparisons 表）。
- 官方车型页链接（`official_page_url`）**当前数据为空**（唯一来源汽车之家不提供该字段，
  见 `docs/deployment.md` §8）：详情页统一用 `external_link`（`kind=official|source`，官方优先，
  判定在后端）——有官方链接时新窗口进入官方车型页（rel="noopener"）；
  缺失时回退「查看数据来源」入口（由 `external_series_refs` 的来源名 + 外部 id 确定性拼出，
  **不得冒充品牌官网**）。对比页与 Agent 回复目前仍只显示官方链接。
