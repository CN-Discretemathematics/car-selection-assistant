# Skill：数据采集与校验（阶段 3 数据管线）

- 用途：把官方车型数据导入数据库，保证枚举、必填与来源优先级正确。
- 来源：阶段 3 沉淀（对应 `backend/app/sources/importer.py`、`fetcher.py`）。
- 适用阶段：3（车辆数据中心）。
- 最后验证：2026-09（189 用例全绿）。

## 步骤

1. 确认数据来源合法：本项目使用**汽车之家公开数据（门户口径）**，抓取遵守 robots.txt 与站点条款；不得接入未获授权的授权数据源（如乘联会销量数据）。
2. 构造导入载荷（JSON），结构见 `backend/tests/test_importer.py` 的 `_payload`：
   `source / brands / series[].model_years[].variants[].{config_version,powertrain,drivetrain,energy_type,price_cny,facts[]} / sales[]`。
3. 先 dry-run 校验：
   ```powershell
   cd backend; $env:PYTHONPATH='vendor;.'
   python tools/import_data.py payload.json --dry-run
   ```
4. 正式导入：`python tools/import_data.py payload.json`（校验失败整体回滚，不写库）。
5. 检查冲突：`GET /api/v1/admin/data-quality-conflicts`（管理凭据），低优先级来源差异只记录不覆盖。

## 约束

- 每条数据必须带 source_id / url / crawled_at / verified_status / credibility（快照记录走 `fetcher.record_snapshot`）。
- 缺失值写 null，禁止用 0 或占位字符串冒充。
- 抓取原始文件存对象存储（本地开发用 `snapshots/` 目录），正文提取后写入 `SourceDocument.content_text`。
- 同字段冲突：官方来源优先（official_site > official_doc > licensed_data > industry_data > other > user_review）。
