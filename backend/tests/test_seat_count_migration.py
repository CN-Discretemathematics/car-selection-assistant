"""H2-A 的 schema 侧契约（2026-10-03）。

## 三件事被钉住

1. **迁移链首尾相接、无分叉**——新增迁移必须接在当时的 head 上，否则生产
   `alembic upgrade head` 会开第二条链，schema 与本地 create_all 悄悄分家。
2. **迁移可升可降**——`add_column` / `create_index` / `drop_*` 四个 op 都要
   在 SQLite 上实跑一遍（生产是 PG，本地是 SQLite，两个都得能过）。
3. **`seat_count` 必须可空**——不是风格问题，见下。

## 为什么必须可空

物化前的判定是 `seats is not None and seats < profile.passengers` 才过滤。
也就是说**库里没有座位数事实的款型是被保留的**（缺数据 ≠ 不满足）。
列一旦 NOT NULL 或回填时填 0/2，所有「数据缺失」的款型会被静默丢弃——推荐结果变了，
而且**没有任何东西会报错**。所以「可空」是语义要求，由本测试守住。

## 附带记录一条先于本次改动就存在的事实

既有迁移 `7e9dda19f6df_widen_spec_fact_value` 用了
`ALTER TABLE … ALTER COLUMN … TYPE`（PG 专有），在 SQLite 上直接
`near "ALTER": syntax error`。也就是说 **`alembic upgrade head` 在 SQLite 上
从来跑不通**，本地/快照库走的是 `import_data.py` 的 `create_all` 另一条路。
两条路必须保持 schema 一致——这正是本测试存在的意义。
"""
from __future__ import annotations

import importlib.util

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.common.models import VehicleVariant

NEW_REV = "b7e4c1d90a25"


def _revisions() -> dict[str, str | None]:
    from pathlib import Path

    out: dict[str, str | None] = {}
    for p in sorted((Path(__file__).resolve().parents[1] / "alembic" / "versions").glob("*.py")):
        src = p.read_text(encoding="utf-8")
        rev = next((line.split("=", 1)[1].strip().strip("'\"") for line in src.splitlines()
                    if line.startswith("revision =")), None)
        down = next((line.split("=", 1)[1].strip().strip("'\"") for line in src.splitlines()
                     if line.startswith("down_revision =")), None)
        # `down_revision = None` 被上面的字符串处理留成了字面量 "None"（truthy），
        # 于是 root 被误判成「有父节点」，全局链检查数不出 root（2026-10-05 实测）。
        # 这里显式转回 None。
        if down is not None and down.strip("'\"").lower() in ("none", "null"):
            down = None
        if rev:
            out[rev] = down
    return out


def _heads(vers: dict[str, str | None]) -> list[str]:
    downs = {d for d in vers.values() if d}
    return [r for r in vers if r not in downs]


def _load_migration():
    from pathlib import Path

    p = (Path(__file__).resolve().parents[1] / "alembic" / "versions"
         / f"{NEW_REV}_add_variant_seat_count.py")
    spec = importlib.util.spec_from_file_location("mig_seat_count", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_chain_is_linear_and_ends_at_single_head():
    """整条迁移链必须**线性无分叉**——这是全局不变式，不是某一个迁移的属性。

    2026-10-05 更正：本条原先把 `NEW_REV`（座位数那个）写死在断言里，
    于是**每加一个迁移都会让它变红**——2026-10-05 加 `agent_turn_logs` 时实测如此。
    一个「每加一个正常迁移就失败」的测试，等于半个摆设：大家会习惯性忽略它。
    现改为断言**全局**不变式，且严格度只增不减：

      - 恰好一个 root（down_revision 为 None）
      - 恰好一个 head（没有分叉、没有两个分支并行）
      - 每个非 root 的 down_revision 都真实存在（没有悬空引用）
      - 从 head 能一路走到 root（没有环、没有断链）

    这样任何**新增**迁移都会自动纳入检查，而不需要改这个文件。
    """
    vers = _revisions()
    roots = [r for r, d in vers.items() if not d]
    heads = _heads(vers)
    assert len(roots) == 1, f"应恰好一个 root（down_revision=None），实际 {roots}"
    assert len(heads) == 1, f"应恰好一个 head（分叉？），实际 {heads}"

    # 悬空引用：down_revision 指向不存在的 revision
    dangling = {r: d for r, d in vers.items() if d and d not in vers}
    assert not dangling, f"down_revision 指向不存在的迁移：{dangling}"

    # 从 head 一路走回 root；走不到说明有环或断链
    seen: list[str] = []
    cur = heads[0]
    while cur:
        seen.append(cur)
        cur = vers[cur]
        if len(seen) > len(vers):
            raise AssertionError(f"迁移链成环，未能到达 root：{seen}")
    assert len(seen) == len(vers), (
        f"从 head 只能走到 {len(seen)} 个，但链上有 {len(vers)} 个——"
        f"有分支没被遍历到（可达 {seen}）"
    )
    assert seen[-1] == roots[0], f"链路终点应是 root {roots[0]}，实为 {seen[-1]}"

    # 座位数那次迁移自身仍必须挂在链上（它的专属断言保留）
    assert NEW_REV in vers, f"{NEW_REV} 不在迁移集合里"
    assert vers[NEW_REV] in vers, f"{NEW_REV} 的 down_revision 悬空：{vers[NEW_REV]}"


def test_migration_upgrades_and_downgrades_on_sqlite(tmp_path):
    """upgrade / downgrade 的 op 在 SQLite 上实跑（生产是 PG，两个都得能过）。"""
    mig = _load_migration()
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with eng.begin() as c:
        c.execute(sa.text(
            "CREATE TABLE vehicle_variants (id INTEGER PRIMARY KEY, display_name TEXT)"))

    def state() -> tuple[list[str], list[str]]:
        insp = sa.inspect(eng)
        return ([c["name"] for c in insp.get_columns("vehicle_variants")],
                [i["name"] for i in insp.get_indexes("vehicle_variants")])

    with eng.begin() as c, Operations.context(MigrationContext.configure(c)):
        mig.upgrade()
    cols, idx = state()
    assert "seat_count" in cols, "upgrade 未加列"
    assert any("seat" in i for i in idx), "upgrade 未建索引"

    with eng.begin() as c, Operations.context(MigrationContext.configure(c)):
        mig.downgrade()
    cols2, idx2 = state()
    assert "seat_count" not in cols2, "downgrade 未删列"
    assert not any("seat" in i for i in idx2), "downgrade 未删索引"


def test_seat_count_column_is_nullable_in_model():
    """模型侧同样必须可空——回填只覆盖有事实的款型，其余靠 NULL 表示「无数据」。"""
    col = VehicleVariant.__table__.c.seat_count
    assert col.nullable is True, (
        "seat_count 必须可空：库里没有座位数事实的款型必须被**保留**（缺数据 ≠ 不满足），"
        "NOT NULL 会让它们在回填后被静默丢弃"
    )
    assert "seat_count" in VehicleVariant.__table__.c, "模型缺 seat_count 列"
