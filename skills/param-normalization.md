# Skill：参数归一化（阶段 5/6 详情与对比）

- 用途：把不同来源的参数值统一到可比口径（单位、精度、工况、缺失值）。
- 来源：阶段 6 沉淀（对应 `backend/app/variants/normalization.py`）。
- 适用阶段：5（详情）、6（SKU 对比）。
- 最后验证：2026-09。

## 规则

1. 单位别名统一：千瓦→kW、千瓦时→kWh、公里→km、牛米→N·m、升→L、马力→hp 等
   （`UNIT_ALIASES`）。
2. 数值精度统一：`150 kW`、`150千瓦`、`150 千瓦` 归一化为 `("150", "kW")`。
3. 工况必须保留并参与相等性判断：CLTC / NEDC / WLTC 不同工况不直接比较，展示时标注
   `500 km (CLTC)`。
4. 缺失值统一显示「官方资料未披露」，不得显示 0，不得由模型补全。
5. 展示与比较分离：展示用 `display_fact_value`（单位去重），比较用 `fact_identity`
   （含工况的归一化身份）。

## 验证

```powershell
cd backend; $env:PYTHONPATH='vendor;.'
python -m pytest tests/test_normalization.py tests/test_rag.py -q
```
