# 三档能力表：把「档位」翻译成「能做什么」（2026-10-21）

> 来源：2026-10-21 代码审查的 **Major M2**（结构问题，非当前缺陷）。
> 分支：`chore/structure-and-doc-truth`（与 M1、文档事实对齐同批）

## 一句话

三档（off / shadow / on）的语义此前散在**两个布尔门**里（`is_enabled()` /
`is_observed()`），于是「选错了门」就会**静默失效**——2026-10-21 生产实测就是这么
出的事。现在收敛成**一张能力表**，调用点按**能力**取，按名字猜不了。

## 事故形状回顾

2026-10-21 生产实测发现：路由层用 `is_enabled()` 决定「是否解析身份」，
而 `shadow` 档的 `is_enabled()` 为假 ⇒ **影子日志一条都不落**
⇒ `shadow` 成了**空档**（不写库、也不观察）
⇒ 「先观察一两天再切 on」那道**观察期是假的**。

那次修复的方式是「**再加一个门**」（`is_observed()`）。
⇒ 选错门的触发面**变大**而不是变小：现在有 4 个布尔可组合，
下一个人只要再挑错一个，就又是一次静默失效。

## 改了什么

`app/agent/preferences.py` 新增：

| 能力 | 含义 | 谁需要它 |
| --- | --- | --- |
| **observe** | 解析匿名身份 + 落观察日志 | 路由层（解析身份）、`persist_hints`、`record_feedback` |
| **write** | 写库（偏好记忆 / 拒绝反馈） | `remember`、`persist_hints`、`record_feedback` |
| **refill** | 回填进画像——**会改变推荐结果** | `restore_into_profile` |

> ⚠️ 2026-10-22 更正：`effective_brand_exclusions` 原本被算作 `refill` 的使用者，
> 那道门让**影子档看不见品牌排除项**（观察期少了一个键，判据是残的）。
> 它做的是「**算出**排除项」——给日志看不需要回填 ⇒ 改用 `observe`。
> 回填那道门仍在 `restore_into_profile` 自己身上 ⇒ `shadow` 的推荐结果逐位不变。
> 详见 [影子档看不见品牌排除项](2026-10-22-shadow-brand-exclude-observation.md)。

一张表说清每档能做什么：

| 档位 | observe | write | refill |
| --- | --- | --- | --- |
| `off` | ✗（连查都不查） | ✗ | ✗ |
| `shadow` | ✓ | ✗ | ✗ |
| `on` | ✓ | ✓ | ✓ |

调用点全部改成**按能力取**：

- `router.py`：`if mode_capabilities().observe else None`（解析身份）
- `preferences.py`：`not mode_capabilities().refill` ⇒ 不回填
- `preferences.py` / `feedback.py`：`not mode_capabilities().write` ⇒ 只落日志

**`is_enabled()` 与 `is_observed()` 已删除**——留着它们，选错门的可能就还在。

## 验证

新增 `backend/tests/test_mode_capabilities.py`（5 条），其中两条是**结构契约**：

| 断言 | 抓什么 |
| --- | --- |
| 能力矩阵逐格一致 | 三档语义没漂 |
| `shadow` 观察但**不**写、**不**回填 | 那次事故的反面 |
| 未知档位回退到最保守档 | 非法值不会意外放开写库 |
| **档位常量不得出现在 `preferences.py` 之外** | 语义只有一个真值点 |
| **不得手写「拿 `get_mode()` 和档位常量比较」** | 语义只许由能力表给 |

**变异测试**：在 `feedback.py` 里手写一句 `if get_mode() == MODE_SHADOW:` ⇒
两条结构契约**同时变红** ⇒ 契约不是摆设。

## 边界

- 行为**逐字不变**：三档的判定结果与改前完全一致（本笔只改「怎么表达」，
  不改「是什么」）。
- 本笔**不新增档位**：三档不变，只是把它们的能力写进一张表。
