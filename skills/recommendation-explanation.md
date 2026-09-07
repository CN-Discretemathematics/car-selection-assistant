# Skill：推荐解释话术（阶段 7 Agent）

- 用途：生成选车推荐的解释文本：引用、妥协项、禁编造。
- 来源：阶段 7 沉淀（对应 `backend/app/agent/engine.py` 的 `_explain` / `_template_explanation`）。
- 适用阶段：7（Agent）。
- 最后验证：2026-09。

## 规则

1. 解释只基于工具返回的候选数据（价格/配置/来源），禁止编造价格、销量、优惠、库存。
2. 结构：推荐名单（前 3 名）→ 首选理由（价格、匹配项）→ 妥协项（如实引用
   tradeoffs，如「空间官方资料未披露，未参与评分」）→ 数据来源。
3. 无候选时如实说明并建议放宽条件（「我们不会编造不存在的数据」）。
4. 输出前过 `safety_guard`（拦截优惠/库存/成交价/交易类词），命中则回退确定性模板。
5. LLM 不可用时（`LLMClient.available=False`）回退模板解释，Agent 仍可用（原则 7）。
6. 前端展示必须标注「AI 生成内容，仅供参考」，并附 citations（来源名列表）。

## 验证

```powershell
cd backend; $env:PYTHONPATH='vendor;.'
python -m pytest tests/test_agent.py tests/test_tools.py -q
```
