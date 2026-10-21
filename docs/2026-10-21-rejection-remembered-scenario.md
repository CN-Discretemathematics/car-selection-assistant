# 第四类场景：拒绝**跨会话**记住（Phase 2b 的验收题）

> 分支：`feat/multiturn-rejection-remembered-scenario`｜日期：2026-10-21
> 依赖：Phase 2b（反馈表 + 身份）已进主干 ✓

## 一、为什么加这一类

会话内拒绝（`rejected_variant_ids`）本来就生效 ⇒ 第一类场景
（`reject_then_no_recommend`）已经验过 ✓。

真正的新东西是**跨会话**：「拒绝」被写进反馈表、下次会话读回 ⇒
少这一类题，Phase 2b 就只有单测、**没有评测级**的验收 ✓。

## 二、场景形状

`rejection_remembered_across_sessions`（50 题，默认 `enabled: false` ✓）：

| 轮 | 会话 | 内容 |
|---|---|---|
| 1 | `first` | 需求句（该车系在榜 ✓） |
| 2 | `first` | 「甲车系不要，{理由}」 |
| 3 | **`second`** | **同一条需求句** ⇒ 断言「甲车系**不再出现**」 |

同一场里两个会话 ⇒ 由框架**共用一个身份**（`run_scenario` 建一个身份，
本场所有会话共用 ✓）⇒ 这正是「跨会话」的定义 ✓。

它测到的链路是**三段**（缺一段这题就红 ✓）：

```
身份 → 拒绝写反馈表 → 新会话读回 → 并进 rejected_variant_ids → 推荐链排除它
```

## 三、A/B 实测（含**反证**）

| 模式 | 结果 |
|---|---|
| `DURABLE_PREFS_MODE=on` | **50/50 通过** ✓ |
| `DURABLE_PREFS_MODE=off` | **50/50 失败** ✓ |

第二条是**反证**：反馈不写也不读 ⇒ 被拒的车会回来 ⇒ 断言必须红 ✓。
只测「通过」的话，一个「什么都没做但恰好没推」的实现也能蒙混过关 ✓。

整份题库（4 类 × 50 = 200 题）在 mode=on 下 **198/200**（另 2 条是
「该轮引擎确实没东西可推」的如实判无效 ✓）。

## 四、开关

跨会话场景仍默认关，用 `--enable-memory-scenarios` 打开 ✓：

```powershell
python tools/gen_multiturn_scenarios.py --out eval/multiturn.json --enable-memory-scenarios
python tools/eval_multiturn.py --scenarios eval/multiturn.json --report eval/multiturn-report.json
```

⚠️ 跑之前要有 `DURABLE_PREFS_MODE=on` ✓（`off`/`shadow` 模式不回填记忆、
不读写反馈 ⇒ 跨会话场景必然红 ✓ —— 这正是反证要的东西 ✓）。

## 五、用例

`backend/tests/test_gen_multiturn_scenarios.py` 新增 2 个：
场景形状（两个会话 + 拒绝轮 + 第三轮断言 ✓）、默认关 ✓。

pytest：全量通过 ✓；ruff：128（与基线一致 ✓）。
