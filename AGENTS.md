# 项目常驻约定（Agent 每次会话自动加载）

中文车企选型平台：FastAPI + SQLAlchemy 2.0 + LangGraph RAG（后端）+ Next.js 15（前端），
生产部署在阿里云轻量服务器（Docker Compose + 宿主 nginx）。**产品原则：不编造数据**——
所有价格/销量/配置必须有来源，缺失统一显示「官方资料未披露」，不做猜测补全。

## 动手前先读

| 要做的事 | 先读 |
|---|---|
| 部署 / 自动更新 / 管理入口 | `docs/deployment.md` |
| 阿里云控制台运维（续费/防火墙/备份/RDS/OSS/RAM/备案） | `docs/aliyun-ops.md` |
| 凭据轮换 | `docs/credential-rotation.md` |
| 数据采集/导入与校验 | `skills/data-import-validation.md` |
| Agent/推荐相关改动 | `skills/constraint-integrity.md`、`skills/recommendation-explanation.md` |
| 分支、提交与命令卫生 | `skills/git-branch-sync.md`（通用部分见全局 `~/.dsh/AGENTS.md`） |

## 硬性约束

1. **不推送 main**：改动走分支 + PR，由用户合并；只有 main 会被自动部署（每日 05:00 拉取）。
2. **事实只来自数据库**：Agent 不得凭记忆列举车型/价格；盘点类问题必须读库
   （见 `skills/constraint-integrity.md`）。
3. **用户可见文案不出现内部术语**：不写 SKU/§章节号/实现说明（前端与后端模板都算），
   自检脚本 `.tools/scan_ui_jargon.py`。
4. **远程运维走脚本文件**（`scp` + `sh`/`python3`），不要在 `ssh` 里内联引号/`$()`。
5. **改文件用编辑工具**，不要用 PowerShell 的字符串管道（会毁 UTF-8 与行尾）。

## 常用验证命令

```powershell
# 后端（Windows 本地）
cd backend; $env:PYTHONPATH='vendor;.'; $env:RETRIEVAL_BACKEND='inmemory'; python -m pytest -q
# 前端
cd web; npx tsc --noEmit; npm run build      # 注意：先停掉 next dev，二者共用 .next
# 密钥扫描（退出码必须为 0）
python reviewer/scan_secrets.py
```

## 事实源

- 数据库：PostgreSQL（RDS）`monthly_sales` / `official_prices` / `spec_facts` / `vehicle_series` …
- 向量库：Zilliz（稠密）+ 进程内 BM25（稀疏）；**销量导入后必须重建两者**（见 deployment.md §5）
- 抓取快照：`backend/snapshots/`（OSS 仅归档，冷归档不可直读）
