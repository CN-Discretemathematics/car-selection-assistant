# 解释模块的上下文里不能有内部 id（2026-10-21）

> 分支：`fix/explain-context-no-internal-ids`｜基线：`origin/main`（不依赖任何未合入分支）

## 背景（可追问率那笔的附带发现）

计划 §五「可追问率」那笔的实测里撞到过一次：用户锁定某车系而候选为空时，
模型在用户可见的回答里写了

> 「……并锁定了**车系 ID 905**。」

两处毛病：

1. **泄漏内部 id**：「905」是内部主键，用户看不懂；
2. **问不出处**：画像里确实有这个数，但它是**主键不是事实**——
   「这个 905 来自哪条记录」没有答案（可追问率会把它判成「没有出处」）。

根因：解释模块的上下文直接用了 `profile.model_dump()`，
把 `locked_series_ids` / `brand_ids` / `last_recommended_variant_ids` 这些
**只对内部有意义**的字段一起给了模型。

## 修法（只修 id 泄漏）

新增 `app/agent/schemas.py::llm_profile_view(profile)`：返回**去掉内部 id 字段**的
画像视图，解释模块的上下文改用它。

- **去掉**：`locked_series_ids` · `last_recommended_variant_ids` ·
  `rejected_variant_ids` · `brand_ids` · `brand_exclude_ids`
- **保留**：`brand_labels` · `brand_exclude_labels` 等**可读同义字段**——
  解释模块需要知道「用户要什么品牌」，那说的是名字而不是主键。

字段清单与视图函数**放在同一个 module**（`schemas.py`）——
清单和用法各写一份必然漂移（新增 id 字段时忘了加进清单，泄漏就回来了）。

落盘、内部计算、接口返回**一律仍用完整画像** ✓：锁定、去重、记忆都靠这些 id。

## 守卫（3 条用例 + 变异 3/3）

`backend/tests/test_explain_context_internal_ids.py`

| 约束 | 用例 |
| --- | --- |
| 视图去掉内部 id、**保留可读字段** | `test_llm_profile_view_drops_internal_ids` |
| 解释模块的上下文里确实没有内部 id（用捕获上下文的假 LLM 断言） | `test_explain_context_has_no_internal_ids` |
| **落盘与内部记账不受影响** | `test_profile_storage_still_keeps_internal_ids` |

变异脚本 `.tools/mutate_eci.py`（本地专属，不入库）**3/3**：视图不去 id 字段 /
视图把可读字段也删了 / 上下文仍用完整画像。

## 行为变更

| 场景 | 变更 |
| --- | --- |
| 用户可见回答 | **可能少一句**提到内部 id 的话（那本来就不该出现） |
| 锁定/解锁提示、解锁说明 | **不变**（走确定性模板，不经模型） |
| 画像落盘、内部计算、接口返回 | **不变** |
| 解释模块看到的画像 | 少了 5 个 id 字段，其余原样 |

打分与排序未变。

## 已知边界

- **数字白名单不在本笔**。按产品口径（2026-10-21 拍板）：本笔只修内部 id 泄漏；
  给解释路径加数字白名单（`answer_contract` 覆盖解释路径）是**行为变更**
  （不合规的答案会被改写或回退模板），需另立项。
- 只改了**解释模块**的上下文。闲聊路径（`_chat_reply`）**本来就没有**把画像给模型
  （它给的是库内事实块与检索证据——都是可读名字 ✓），故无需改，也没有第二个漏口。
- 上下文里仍保留**可读的**品牌名等字段——模型可以引用它们（那是有出处的名字）。
