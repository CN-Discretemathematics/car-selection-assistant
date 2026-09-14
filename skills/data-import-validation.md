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

## 榜单类数据：首页 ≠ 全量（2026-09 事故沉淀）

**教训**：榜单页面的首屏 SSR 只是分页的第一页。汽车之家销量榜页面只 SSR **20 行**，
而 `listRes` 里同时给出 `pagecount=33 / pagesize=20`——历史实现只解析首屏，导致
每月入库 20 行、覆盖 20/908 个在售车系（**2.2%**），用户侧表现为「很多车型没有销量」。

**正确做法**：

1. 先用页面 `__NEXT_DATA__.initialValues.date` 确认门户**当前公布月份**（接口对未发布月份会
   静默返回上一月，调度脚本靠这个信号做「未就绪则次日重试」）；
2. 完整榜单走站点自身的接口并按 `pageindex/pagesize` 翻页：
   `https://www.autohome.com.cn/web-main/car/rank/getList?typeid=1&subranktypeid=1&levelid=0&price=0-9000&date=YYYY-MM&pageindex=N&pagesize=200`
   （实测 `pagesize=1000` 可一次返回全量 650 行；实现取 200/页、上限 12 页）；
3. 接口异常时**回退首屏并在报告里标记来源**（`source=page-fallback`），不要静默降级——
   覆盖率会从 71% 掉回 2%，日志必须能看出来；
4. 导入后做**覆盖度体检**（每次数据操作后都跑）：
   ```sql
   select month, count(*), count(distinct series_id) from monthly_sales group by month order by month desc limit 6;
   select count(*) from vehicle_series where active_status='active';   -- 分母
   ```
   覆盖率（当月有销量车系 / 在售车系）应稳定在 60% 以上；个别月份异常低即为抓取缺陷。

**榜单会带入新品牌/新车系**：榜单只提供名称与 brandid，导入会先建「待分类（汽车之家销量榜）」
占位品牌的车系。必须紧接着跑车系详情抓取补齐真实品牌/级别/价格/缩略图，否则浏览页会出现
「待分类 + 暂无价格」的空卡片：

```powershell
python tools/fetch_autohome_series.py --ids <榜单带入的 seriesid 列表> --do-import   # 礼貌限频 1 秒/页
```

查占位车系（应为 0）：`select count(*) from vehicle_series s join brands b on b.id=s.brand_id where b.name like '待分类%';`

**补了车系资料 ≠ 有款型（2026-09-14 用户实测「风云A9 有销量但没款型」）**：
`fetch_autohome_series.py` 只写车系级字段（品牌/级别/价格区间/能源/车身），**SKU 与参数要靠
`fetch_autohome_sku.py` 单独抓**。只跑前者 → 详情页「暂无在售款型数据」、配置表全空，
但销量正常，很容易被误判为「汽车之家也没有数据」（实际有，实测风云A9 4 款、风云A9L 16 款）。

```sql
-- 每次大批导入后都查一遍：无在售款型的在售车系（应趋近 0）
select count(*) from vehicle_series s
where s.active_status='active'
  and not exists (select 1 from vehicle_variants v where v.series_id=s.id and v.status='on_sale');
```

补法见 `docs/deployment.md` §7（导出缺口 id → `carsel-sku-backfill.sh` 分批抓 → 重建索引）。
2026-09-14 实测缺口 201 个车系（凯美瑞/途观L/海豹06 等在列），全部有汽车之家 id 可直接抓。

**补齐数据会放大下游**：首页榜单接口原先整体返回命中列表，数据补齐后单月 650 个车系会把
首页 HTML 撑到 4.7MB。列表类接口一律带 `limit`（首页 `limit=20`，总数走 `X-Total-Count` 响应头），
完整榜单交给带分页的 `/vehicles`。

**导入销量后必须重建向量索引**（否则回答里的销量是旧的）：

车系摘要切片文本含「YYYY-MM 月销量 N 辆」一句话（`app/rag/ingest.py`），所以销量数据变化后：

```powershell
# 稠密（Zilliz）：全量重灌，写入 .tmp/dense-build-meta.json 水位
python tools/build_retrieval_index.py --target dense --smoke
# 稀疏（BM25）：生产模式（RETRIEVAL_BACKEND=milvus）**不会**按数据量自动重建，
# 必须显式重建，否则一直是进程启动时的那份索引
curl -X POST -H "Authorization: Bearer $ADMIN_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"target":"sparse"}' localhost:8000/api/v1/admin/rag/reindex
```

**水位判断要同时看月份与规模**（2026-09 事故）：只比 `sales_month` 会漏掉「同月内补齐数据」
——本次月销量行数由 20 补到 650、车系 908→1078，水位仍显示 `stale=false`。因此
`dense-build-meta.json` 现在同时记录构建时的库内规模（`db_counts`），
`/admin/rag/status` 会在规模漂移时给出 `车系 908→1078` 之类的 stale_reason。

重建后按 `/admin/rag/status` 核对：`dense.chunks == sparse.chunks`，且摘要切片能被检索到
（用 `get_dense_backend().search("秦PLUS 月销量")` 抽查命中文本是否含「月销量」）。
