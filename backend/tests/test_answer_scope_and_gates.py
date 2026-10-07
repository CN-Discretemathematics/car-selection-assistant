"""第 4 批：回答声明覆盖范围 / scan_secrets 前导下划线 / AGENTS.md 基线数。"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from app.agent.series_qa import build_series_qa_answer
from app.catalog.series_index import active_series_count, resolve_series
from tests.seed import make_brand, make_series, make_source, make_variant, make_year

# `reviewer/` 在**仓库根**，不在 `backend/` 的 sys.path 上。
# 不用 `sys.path.insert`（那会逼出 3 条 E402，且污染全局导入路径）——
# 改在测试内按文件位置定位并 importlib 加载真模块。
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_gate():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "carsel_scan_secrets", _REPO_ROOT / "reviewer" / "scan_secrets.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed(db: Session) -> None:
    src = make_source(db, name="汽车之家")
    vw = make_brand(db, name="大众", source=src)
    for name in ("朗逸", "轩逸"):
        s = make_series(db, vw, name=name, source=src, positioning="紧凑型车")
        year = make_year(db, s)
        make_variant(db, s, year, config_version="旗舰", energy_type="BEV",
                     price_cny="150000", source=src)
    db.commit()


# ── ① 回答里声明覆盖范围 ────────────────────────────────────────────────────


def test_answer_states_the_library_scope(db_session: Session) -> None:
    """回答里必须说清「只覆盖库内在售的 N 个车系」。

    此前用户问「大众朗逸和明锐哪个好」只得到朗逸的回答、**没有任何提示**说明锐
    库里没有，会以为看全了。说出范围比逐个点名「哪个没有」诚实且零维护成本——
    要说准某台车缺，前提是能认出它是个车型名，而库里没有它就需要一份全量车型名录。
    """
    _seed(db_session)
    msg = "大众朗逸和轩逸哪个好"
    resolved = resolve_series(db_session, msg)
    text = build_series_qa_answer(db_session, resolved, msg)
    assert "库内在售车系" in text, f"回答应声明覆盖范围。实际末尾：{text[-200:]}"
    n = active_series_count(db_session)
    assert f"{n} 个" in text, f"范围里应带上在售车系数 {n}。实际末尾：{text[-200:]}"
    assert "未收录的车型不在此列" in text


def test_active_series_count_matches_db(db_session: Session) -> None:
    """声明的那个数必须与库里真实的在售车系数一致——说错数比不说更糟。"""
    _seed(db_session)
    from sqlalchemy import func, select

    from app.common.models import VehicleSeries

    real = db_session.execute(
        select(func.count(VehicleSeries.id))
        .where(VehicleSeries.active_status == "active")
    ).scalar()
    assert active_series_count(db_session) == real


# ── ② scan_secrets 的私有名豁免 ─────────────────────────────────────────────
#
# ⚠️ 这两条此前把门禁那条正则**硬编码复制**了一份再断言，等于同义反复：
# 上线前审查变异实测——把**真** `_looks_like_identifier_or_path` 弄坏
# （恒返回 False / 退回旧正则），这批测试仍全绿。现改为直接 import 真函数。
#
# ⚠️⚠️ 方向也要说准：`_looks_like_identifier_or_path` 是 `high-entropy-secret` 规则的
# **豁免过滤器**，返回 True 就 `continue`（**不上报**）。所以放宽它 =
# **多豁免 = 少报**，不是「补上检测面」。此前提交信息写成「补覆盖」是**说反了**。

def test_scan_secrets_exempts_leading_underscore_private_names() -> None:
    """≥24 字符、以 `_` 开头、全小写含下划线的私有名，现在被**豁免**（不再误报）。

    `HIGH_ENTROPY = \\b[a-zA-Z0-9+/_-]{24,}={0,2}\\b` 有 24 字符门槛——
    `_private_key`（12 字符）根本到不了这个过滤器，所以「加前导下划线」对它 **no-op**；
    真正受影响的是 ≥24 字符的那一类。
    """
    ss = _load_gate()
    HIGH_ENTROPY, _looks = ss.HIGH_ENTROPY, ss._looks_like_identifier_or_path

    for name in (
        "_private_key_for_admin_token",       # 28 字符，真实长私有名
        "_db_password_for_production_db",     # 30 字符
        "_comparison_analysis_reply",          # 仓库里的真实标识符（28 字符）
    ):
        assert HIGH_ENTROPY.search(name), f"前提：{name} 能过 24 字符门槛"
        assert _looks(name), (
            f"「{name}」应被豁免（否则门禁会把它当成疑似密钥上报）"
        )


def test_scan_secrets_still_exempts_plain_snake_case() -> None:
    """原有的全小写 snake_case 豁免**没有**被这次改动削弱。"""
    _looks = _load_gate()._looks_like_identifier_or_path

    for name in ("test_credentials_include_legacy_token", "some_long_snake_case_name"):
        assert _looks(name)


def test_scan_secrets_did_not_exempt_mixed_case() -> None:
    """混大小写 / 含数字的名字**依旧不进豁免**——噪音面没有扩大。

    这是这次改动唯一该守住的边界：放宽的只有「多一个前导下划线」，
    其它条件一个字都没松。
    """
    _looks = _load_gate()._looks_like_identifier_or_path

    for name in ("ABC_def_ghi_jkl", "Mixed_Case_With_Under_scores",
                 "Has1234Numbers5678And_Under", "3d_render_pipeline_path"):
        assert not _looks(name), (
            f"「{name}」不该被豁免——混大小写/含数字的名字仍要照常上报"
        )


# ── ③ AGENTS.md 的基线数与实测一致 ──────────────────────────────────────────


def test_agentsmd_ruff_baseline_matches_reality() -> None:
    """`AGENTS.md` 写「基线 130 条」，而实测长期是 **129**（连续五轮）。

    文档与现实不符会直接误导下一个人——「基线 130」意味着多出来的 1 条是新增回归，
    而实际上它是文档写错了。2026-10-07 用户拍板改为 129。
    """
    from pathlib import Path

    agents = (Path(__file__).resolve().parents[2] / "AGENTS.md").read_text(encoding="utf-8")
    assert "基线 **130 条**" not in agents, "AGENTS.md 仍写着过期的 130 条基线"
    assert "基线 **129 条**" in agents, "AGENTS.md 应写 129 条基线"
