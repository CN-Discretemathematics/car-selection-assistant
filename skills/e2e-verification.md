# Skill：端到端验证（阶段 8 评测与上线）

- 用途：每轮交付前完成验证闭环：测试 → 冒烟 → 压测 → 密钥扫描 → 独立审查。
- 来源：阶段 8 沉淀（对应 `backend/tools/e2e_smoke.py`、`load_test.py`）。
- 适用阶段：8（评测和上线）；每个代码任务收尾时都适用。
- 最后验证：2026-09（179 用例 + 16 项冒烟 + 压测全绿）。

## 流程

```powershell
# 1. 后端单测（预期全绿）
cd backend; $env:PYTHONPATH='vendor;.'; python -m pytest -q

# 2. 前端类型与构建
cd ..\web; npx tsc --noEmit; npx next build

# 3. 双服务端到端冒烟（16 项；认证/管理后台项在未配置对应 env 时自动 SKIP）
#    后端：python -m uvicorn app.main:app --port 8000
#    前端：npx next start --port 3000
cd ..\backend; python tools/e2e_smoke.py

# 4. 并发压测（本机容量验证）
python tools/load_test.py --concurrency 20 --requests 500

# 5. 密钥扫描（维护者本地脚本，不入库；任何命中即 BLOCKED）

# 6. 独立 Reviewer 审查（APPROVED / APPROVED_WITH_COMMENTS / BLOCKED）
```

## 注意

- 部分执行环境对回环 http 客户端有限制（httpx 502），压测与冒烟脚本一律使用标准库
  （http.client / urllib），不依赖第三方 HTTP 库。
- 密钥红线：任何命中即 BLOCKED，修复后必须重新扫描确认干净。
- 提交前确认 `git status` 无未跟踪的密钥/临时文件；`.env*`、`vendor/`、`.tmp/`
  等均在 .gitignore。
