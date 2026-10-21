"""密钥扫描器新规则 `raw-db-url-echo`（2026-10-21 代码审查 Blocker 的回归测试）。

## 为什么要有这条规则

`tools/smoke_durable_memory.py` 曾把 `DATABASE_URL` 原样 print 出去
⇒ 生产口令明文进 CI 日志 / 终端历史。

而扫描器**已有的** `db-url-with-credentials` 规则抓不到它：那条扫的是**源码里的
凭据字面量**，而这里回显的是**运行期从环境变量取到的值**——源码里根本没有那串
凭据 ⇒ 字面量规则永远看不到它。

⇒ 补一条判据：**同一行**里同时出现
①**输出口**（print / logger.info / logging.warning…）与
②**连接串来源**（`DATABASE_URL` 环境变量 / `settings.database_url`）
——那就是「把连接串直接打进输出」的那一行。

## 正确写法（本仓认可的唯一形态）

先取值、再把**脱敏结果**打印出去，且**分两行**写：

    url = os.environ.get("DATABASE_URL", "")
    print(f"目标数据库：{describe_database_url(url)}")

⚠️ 本文件**要入库**，所以「反例」一律**运行时拼接**：
若源码里直接出现「print 与 DATABASE_URL 同行」的字面量，
**扫描器会先把这个测试文件自己拦下**（规则对全仓库生效，不给测试开豁免）。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from reviewer.scan_secrets import scan_path  # noqa: E402

#: 运行时拼出来的片段（源码里不出现「输出口 + 连接串来源」同行��组合）
_SINK = "print("
_SOURCE = "DATABASE_URL"


def _scan(tmp_path: Path, content: str) -> list[dict]:
    (tmp_path / "probe.py").write_text(content, encoding="utf-8")
    findings = scan_path(tmp_path, verbose=False, tracked=None)
    return [f for f in findings if f["rule"] == "raw-db-url-echo"]


def test_same_line_sink_and_source_is_flagged(tmp_path: Path):
    """核心判据：输出口与连接串来源**同行** ⇒ 命中。

    这就是修复前那行的形状。
    """
    bad = _SINK + "f'目标数据库：{os.environ.get('" + _SOURCE + "', '')}')"
    findings = _scan(tmp_path, bad)
    assert findings, "「把连接串直接打进输出」这一行没有被规则拦下"
    assert findings[0]["severity"] == "HIGH"


def test_logger_with_settings_attribute_is_flagged(tmp_path: Path):
    """走日志 API 一样算：logger.info 也在把东西写出去。"""
    bad = "logger." + "info('db=%s', settings." + "database_url" + ")"
    findings = _scan(tmp_path, bad)
    assert findings, "logger.info 里的连接串回显没有被拦下"


def test_safe_two_line_form_is_not_flagged(tmp_path: Path):
    """**正确写法**不得被误伤——否则大家会绕过门禁。"""
    safe = (
        "url = os.environ.get('" + _SOURCE + "', '')\n"
        + _SINK + "f'目标数据库：{describe_database_url(url)}')\n"
    )
    findings = _scan(tmp_path, safe)
    assert not findings, f"正确写法被误伤：{findings}"


def test_source_without_sink_is_not_flagged(tmp_path: Path):
    """只取值、不打印（哪怕是赋值）不该被拦。"""
    benign = "url = os.environ.get('" + _SOURCE + "', '')"
    findings = _scan(tmp_path, benign)
    assert not findings, f"仅仅取值被误伤：{findings}"


def test_sink_without_source_is_not_flagged(tmp_path: Path):
    """打印别的东西不该被拦。"""
    benign = _SINK + "('检查通过')"
    findings = _scan(tmp_path, benign)
    assert not findings, f"普通打印被误伤：{findings}"


def test_repo_itself_passes_the_new_rule():
    """仓库自己必须干净：新规则不得把已有代码判红。"""
    findings = scan_path(REPO_ROOT, verbose=False, tracked=None)
    hits = [f for f in findings if f["rule"] == "raw-db-url-echo"]
    assert not hits, f"新规则在仓库里命中了 {len(hits)} 处：{hits[:5]}"
