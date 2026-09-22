# -*- coding: utf-8 -*-
"""doc-sync 检查器回归自测：证明「本地跑 == CI 跑」。

背景（2026-09-16 PR #29 事故）：检查器用 `Path.exists()` 判悬空引用，而本地工作区还有
gitignore 的本地专属文件（PROJECT_PLAN.md / reviewer/REVIEWER_AGENT.md / resume/ …），
于是本地「结论：一致」、CI 同一提交 FAIL 3 处——门禁在 push 前无法自证。

本脚本把 **HEAD 的 tracked-only 检出**（= CI 看到的内容）放进 `.tmp/` 临时 worktree，
用**工作区当前版本**的 `skills/doc_sync_check.py` 真跑三种情形：

  1. 基线：tracked-only 检出必须「结论：一致」（gitignore 引用只 WARN 不 FAIL）；
  2. `--fix`：故意写错用例数 → 必须自愈回实测值；
  3. 盲点：引用一个「本地新增未提交」的文件 → 必须 FAIL（旧检查器会漏掉，CI 会红）。

用法（仓库任意位置）：
    python skills/doc_sync_selftest.py     # 全部通过退出码 0，任一不符退出码 1
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / ".tmp" / "doc-sync-selftest"
CHECK_REL = Path("skills") / "doc_sync_check.py"

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    results.append((bool(ok), label))
    print(f"{'PASS' if ok else 'FAIL'}  {label}")


def run_checker(sandbox: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(sandbox / CHECK_REL), *args],
        cwd=sandbox, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    if SANDBOX.exists():  # 上次异常退出的残留：worktree 元数据也要清
        subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(SANDBOX)],
                       capture_output=True, text=True)
        shutil.rmtree(SANDBOX, ignore_errors=True)

    add = subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "--detach", str(SANDBOX), "HEAD"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    if add.returncode != 0:
        print(f"无法创建临时 worktree：{(add.stderr or '').strip()}")
        return 1
    # 测的是工作区当前版本的检查器（HEAD 里可能是旧的），否则自测会「验证过去」
    shutil.copy2(ROOT / CHECK_REL, SANDBOX / CHECK_REL)

    try:
        rc, out = run_checker(SANDBOX)
        check(rc == 0 and "结论：一致" in out,
              f"tracked-only 检出（CI 视角）结论一致（rc={rc}）")
        check(not any(l.startswith("FAIL") for l in out.splitlines()),
              "本地专属（gitignore）引用不再误报 FAIL")

        # CI 视角没有 backend/.venv，用例数算不出来——`--fix` 此时**不许猜**；
        # 迁移数只依赖仓库文件，用它验证自愈路径。
        readme = SANDBOX / "README.md"
        before = readme.read_text(encoding="utf-8")
        versions = SANDBOX / "backend" / "alembic" / "versions"
        actual_mig = len([p for p in versions.glob("*.py") if p.name != "__init__.py"])
        probe = SANDBOX / "skills" / "_selftest_fix_probe.md"
        probe.write_text("- 迁移共 9 个迁移（自测探针）\n", encoding="utf-8")
        rc, out = run_checker(SANDBOX, "--fix")
        healed = f"{actual_mig} 个迁移" in probe.read_text(encoding="utf-8")
        check(rc == 0 and healed and "FIX" in out,
              f"`--fix` 把写错的迁移数自愈为 {actual_mig}（rc={rc}）")
        check(readme.read_text(encoding="utf-8") == before,
              "无 venv 时 `--fix` 不猜用例数（README 未被改写）")
        probe.unlink()

        probe = SANDBOX / "skills" / "_selftest_probe.md"
        scratch = SANDBOX / "backend" / "app" / "selftest_scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / "probe.py").write_text("# 本地新增未提交\n", encoding="utf-8")
        probe.write_text("- 引用 `backend/app/selftest_scratch/probe.py`（未入库）\n"
                         "- 引用 `skills/no_such_file_xyz.py`（真悬空）\n", encoding="utf-8")
        rc, out = run_checker(SANDBOX)
        check(rc == 1 and "引用未入库" in out, "未跟踪且未忽略的引用判 FAIL（旧检查器的盲点）")
        check("悬空路径引用" in out, "真悬空引用仍判 FAIL（未因新规则放松）")
        probe.unlink()
        shutil.rmtree(scratch, ignore_errors=True)  # 探针目录必须清掉，否则下一轮误触 README 模块核对

        # 前瞻豁免：带「计划/待建」标记的行引用**尚未创建**的目标文件，不判悬空
        #（2026-09-17 全方向执行计划：计划文档会点名 tools/verify_mobile_matrix.py 等待建产物）
        probe = SANDBOX / "skills" / "_selftest_planned_probe.md"
        probe.write_text("- 计划新建 `backend/app/planned_scratch/module.py`（待建）\n",
                         encoding="utf-8")
        rc, out = run_checker(SANDBOX)
        check(rc == 0 and "悬空路径引用" not in out,
              f"「计划/待建」标记的前瞻引用不判悬空（rc={rc}）")
        probe.unlink()

        # 2026-09 备案上线事故回归：过期表述黑名单（规则 8）
        probe = SANDBOX / "skills" / "_selftest_stale_probe.md"
        probe.write_text("- 备案通过前对外只能用 IP 访问（过渡期口径）\n", encoding="utf-8")
        rc, out = run_checker(SANDBOX)
        check(rc == 1 and "过期表述" in out, "备案过渡期表述命中过期表述黑名单")
        probe.unlink()

        # 跨源对账（规则 9）：HEAD 检出含 deploy/nginx-https.conf（listen 443 ssl），
        # 文档仍写「HTTPS 待备案完成后配置」必须 FAIL——两个仓库内真相源不许矛盾
        probe = SANDBOX / "skills" / "_selftest_tlspending_probe.md"
        probe.write_text("- 已知待办：HTTPS/HSTS（备案完成后配置）\n", encoding="utf-8")
        rc, out = run_checker(SANDBOX)
        check(rc == 1 and "跨源对账" in out,
              "nginx 实态已含 listen 443 ssl 时，文档 HTTPS 待办口径判 FAIL")
        probe.unlink()
    finally:
        subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(SANDBOX)],
                       capture_output=True, text=True)
        shutil.rmtree(SANDBOX, ignore_errors=True)

    bad = [label for ok, label in results if not ok]
    print(f"自测结论：{len(results) - len(bad)}/{len(results)} 通过")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
