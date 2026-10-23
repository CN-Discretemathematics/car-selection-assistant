"""跨会话记忆的**端到端功能校验**（在真数据库上跑真实应用栈）

## 为什么要有这个工具（2026-10-21 实测教训）

本仓的测试库一直是 **SQLite**，生产是 **PostgreSQL**。迁移里的
`confirmed BOOLEAN DEFAULT 0` 在 SQLite 上合法、在 PostgreSQL 上直接报错
⇒ **1584 个用例全绿**，却把生产部署挡了 13 天。

⇒ 光有单测不够：**必须在真 PostgreSQL 上把迁移与功能各跑一遍**。
本工具就是那「功能」那一半（迁移那一半在 CI 的 `alembic upgrade head` 里）。

## 它走的是**真实 HTTP 接口 + 真实数据库**（不是测试替身）

| 步骤 | 断言 |
| --- | --- |
| 1 领匿名身份 | 拿到明文 token |
| 2 带身份发消息 | **记忆真的写进库**（`preference_memories` 有行） |
| 3 换会话再问 | 记忆**真的回填**（不再追问预算）——并用「全新身份」做**阳性对照**（它必须追问预算） |
| 4 拒绝一台车 | **拒绝真的写进反馈表**（`variant_feedbacks` 有行） |
| 5 合并到账户 | 数据搬进账户身份；**旧匿名 token 失效** |
| 6 导出 | 用户能看回自己的记忆与反馈 |
| 7 删除 | 真的清空 |
| 8 清理 | 把本次造出来的行**全部删掉**（不污染目标库） |

## 用法

```bash
cd backend
# 本地（SQLite）
DATABASE_URL="sqlite:///.tmp/smoke.db" python tools/smoke_durable_memory.py --seed-car
# 真 PostgreSQL（CI / 生产同构）
DATABASE_URL="postgresql+psycopg://..." python tools/smoke_durable_memory.py
# 只看结果不删数据
python tools/smoke_durable_memory.py --keep
```

退出码 0 = 全过；1 = 有断言失败。
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from _bootstrap import ensure_backend_on_path  # noqa: F401  (import-time side effect: puts backend/ on sys.path; being an import, it also stops E402 on the app.* imports below)

# Windows 控制台默认 GBK ⇒ 打印「⇒」这类字符会 UnicodeEncodeError，**本地跑必崩**
# （CI/服务器是 UTF-8 不受影响，但本工具的文档用法就是「本地跑一遍」）⇒ 这里做兜底。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ⚠️ 必须在 import app 之前设好：闸门决定「写不写库、回填不回填」。
#
# 这里用**强制赋值**而不是 `setdefault`（2026-10-23 修）：
# `setdefault` 会**沿用环境里已有的值** ⇒ 在生产容器里（`DURABLE_PREFS_MODE=shadow`）
# 跑这个脚本，闸门仍是 shadow ⇒ 记忆**不写库** ⇒ 报出
# 「记忆真的写进库 —— 写了 0 行」「记忆搬进了账户 —— memories_moved=0」
# 等一堆**误导性失败**：看起来像「记忆功能坏了」，实际只是「闸门没开」。
# 本脚本的语义就是「校验跨会话记忆功能」⇒ 闸门**必须**是 on。
os.environ["DURABLE_PREFS_MODE"] = "on"
os.environ["RETRIEVAL_BACKEND"] = "inmemory"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.auth.security import get_token_store  # noqa: E402
from app.common.database import describe_database_url, get_session_factory  # noqa: E402
from app.common.models import (  # noqa: E402
    AnonymousIdentity,
    PreferenceMemory,
    User,
    VariantFeedback,
)
from app.main import app  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"  [{'OK' if ok else 'FAIL'}] {name}{('  —— ' + detail) if detail else ''}")


def _ensure_tables(db: Session) -> None:
    """按**方言**选建表路径。

    - **SQLite**（本地开发）：按模型建表（`create_all`）——
      迁移链里有 `ALTER COLUMN`，**在 SQLite 上跑不了** ⇒ 本地只能走这条；
    - **PostgreSQL**（CI / 生产）：表必须**已经由 alembic 建好**——
      缺表就**直接失败**，绝不兜底 create_all：
      兜底等于把「迁移没跑」这件事**掩盖掉**，那正是这个工具要抓的东西。
    """
    from sqlalchemy import inspect

    from app.common.database import Base, get_engine

    engine = get_engine()
    if "vehicle_variants" in inspect(engine).get_table_names():
        return
    if engine.dialect.name != "sqlite":
        raise AssertionError(
            "PostgreSQL 上没有 vehicle_variants 表——迁移没跑成功？"
            "（本工具**不**兜底建表：兜底会把迁移失败掩盖掉）"
        )
    print("  （SQLite 且表不存在 ⇒ 按模型建表；PG 上这一步由 alembic 负责）")
    from app.common import models  # noqa: F401  确保模型注册

    Base.metadata.create_all(bind=engine)
    db.commit()


def _seed_car(db: Session) -> None:
    """造一台在售车（拒绝链路需要有推荐可拒）。

    只在目标库里**没有**在售款型时调用（生产库有车 ⇒ 不动它）。
    """
    from app.common.models import VehicleVariant
    from tests.seed import make_brand, make_series, make_source, make_variant, make_year

    existing = db.execute(select(VehicleVariant).limit(1)).scalar_one_or_none()
    if existing is not None:
        return
    source = make_source(db, name="冒烟测试来源")
    brand = make_brand(db, name="冒烟测试品牌", source=source)
    series = make_series(
        db, brand, name="冒烟测试车系", body_type="sedan", energy_types=("BEV",), source=source
    )
    year = make_year(db, series)
    variant = make_variant(
        db, series, year, config_version="标准版", energy_type="BEV", price_cny="150000", source=source
    )
    # ⚠️ 座位数必须给：问句里说「5口人」⇒ 候选筛选会按座位数过滤
    # ⇒ 没有 seat_count 的车**根本推不出来** ⇒ 拒绝链路无从测起（实测：跳过）。
    variant.seat_count = 5
    db.commit()


def _ask(client: TestClient, message: str, token: str | None = None) -> dict[str, Any]:
    sid = client.post("/api/v1/agent/sessions").json()["session_id"]
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = client.post(
        f"/api/v1/agent/sessions/{sid}/messages", json={"message": message}, headers=headers
    )
    if resp.status_code != 200:
        raise AssertionError(f"发消息失败（{resp.status_code}）：{resp.text[:300]}")
    return resp.json()


def _ask_in_session(
    client: TestClient, session_id: str, message: str, token: str | None = None
) -> dict[str, Any]:
    """在**已有会话**里问一轮（拒绝必须和推荐同会话 ⇒ 引擎才认得出「被拒的款型」）。"""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = client.post(
        f"/api/v1/agent/sessions/{session_id}/messages", json={"message": message}, headers=headers
    )
    if resp.status_code != 200:
        raise AssertionError(f"发消息失败（{resp.status_code}）：{resp.text[:300]}")
    return resp.json()


def _missing(answer: dict[str, Any]) -> list[str]:
    clarification = answer.get("clarification") or {}
    return list(clarification.get("missing") or [])


def run(seed_car: bool, keep: bool) -> int:
    factory = get_session_factory()
    db: Session = factory()
    created_identity_ids: list[int] = []
    created_user_ids: list[int] = []
    try:
        _ensure_tables(db)
        if seed_car:
            _seed_car(db)

        with TestClient(app) as client:
            print("① 领匿名身份")
            ident = client.post("/api/v1/agent/identity").json()
            token = ident.get("token") or ""
            check("领到匿名身份", bool(token), f"identity_id={ident.get('identity_id')}")
            if not token:
                return 1
            created_identity_ids.append(int(ident["identity_id"]))

            print("② 带身份发消息 ⇒ 记忆写进库")
            _ask(client, "预算15万，家用5口人", token)
            rows = db.execute(
                select(PreferenceMemory).where(PreferenceMemory.identity_id == int(ident["identity_id"]))
            ).scalars().all()
            keys = {r.key for r in rows}
            check("记忆真的写进库", bool(rows), f"写了 {len(rows)} 行：{sorted(keys)}")
            check("预算被记住了", "budget" in keys, f"keys={sorted(keys)}")

            print("③ 换会话再问 ⇒ 回填生效（并用全新身份做阳性对照）")
            answered = _ask(client, "按我之前说的条件推荐", token)
            fresh_ident = client.post("/api/v1/agent/identity").json()
            created_identity_ids.append(int(fresh_ident["identity_id"]))
            control = _ask(client, "按我之前说的条件推荐", fresh_ident.get("token") or "")
            check(
                "有记忆的那次不再追问预算",
                "budget" not in _missing(answered),
                f"missing={_missing(answered)}",
            )
            check(
                "【阳性对照】全新身份必须追问预算",
                "budget" in _missing(control),
                f"missing={_missing(control)}",
            )

            print("④ 拒绝一台车 ⇒ 反馈写进表")
            before = db.execute(select(VariantFeedback)).scalars().all()
            n_before = len(before)
            # ⚠️ 拒绝必须发生在**推荐所在的同一个会话**里（引擎靠「本轮被拒的款型」识别拒绝），
            # 而且**必须点名车**：说「这台不要」会被判成**闲聊**
            # （实测：intent=chitchat，matched_rule=1:not(has_car_intent or structured)）
            # ⇒ 拒绝根本不会发生。所以用上一轮**推荐结果里的车名**去拒。
            reject_sid = client.post("/api/v1/agent/sessions").json()["session_id"]
            rec = _ask_in_session(client, reject_sid, "预算15万，家用5口人", token)
            rec_names = [
                str(v.get("series_name") or "") for v in (rec.get("recommended_variants") or [])
            ]
            rec_names = [n for n in rec_names if n]
            rejected: dict[str, Any] = {}
            if rec_names:
                rejected = _ask_in_session(client, reject_sid, f"{rec_names[0]}不要", token)
            after = db.execute(
                select(VariantFeedback).where(
                    VariantFeedback.identity_id == int(ident["identity_id"])
                )
            ).scalars().all()
            if not rec_names:
                print("  [SKIP] 库里没有可推荐的车，拒绝链路测不了（不当作失败）")
            elif not after:
                print(
                    "  [SKIP] 拒绝了「" + rec_names[0] + "」但反馈表没写（不当作失败）"
                    f"  —— 本轮意图={rejected.get('intent') or '?'}"
                )
            else:
                check(
                    "拒绝真的写进反馈表",
                    bool(after),
                    f"新增 {len(after) - n_before} 行（拒的是「{rec_names[0]}」）",
                )

            print("⑤ 合并到账户")
            user = User(email=f"smoke-{ident['identity_id']}@example.invalid", status="active")
            db.add(user)
            db.commit()
            created_user_ids.append(user.id)
            account_token = get_token_store().issue(user.id)
            merged = client.post(
                "/api/v1/auth/merge-identity",
                headers={"Authorization": f"Bearer {account_token}"},
                json={"anonymous_token": token},
            )
            check("合并接口返回 200", merged.status_code == 200, merged.text[:200])
            if merged.status_code == 200:
                body = merged.json()
                moved = body.get("memories_moved", 0)
                check("记忆搬进了账户", moved > 0, f"memories_moved={moved}")
                prefs = client.get(
                    "/api/v1/agent/preferences", headers={"Authorization": f"Bearer {token}"}
                )
                check("旧匿名 token 已失效", prefs.json() == [], f"返回 {prefs.json()!r}")

            print("⑥ 导出")
            # ⚠️ 路径要**按实现来**：auth 路由**没有** `/auth` 前缀
            # （`APIRouter(tags=["auth"])` + `include_router(prefix=api)`）
            # ⇒ 导出/删除是 `/api/v1/me/data`；
            # 而合并那个端点在路由里**手写了** `/auth/merge-identity` ⇒ `/api/v1/auth/merge-identity`。
            # 两组端点前缀风格不一致（已记为待办），但**文档写的是 `/auth/me/data`——那是错的**。
            exported = client.get(
                "/api/v1/me/data", headers={"Authorization": f"Bearer {account_token}"}
            )
            data = exported.json() if exported.status_code == 200 else {}
            check("导出能看到记忆", bool(data.get("memories")), f"{len(data.get('memories') or [])} 条")

            print("⑦ 删除")
            deleted = client.delete(
                "/api/v1/me/data", headers={"Authorization": f"Bearer {account_token}"}
            )
            check("删除接口返回 200", deleted.status_code == 200, deleted.text[:200])
            after_del = client.get(
                "/api/v1/me/data", headers={"Authorization": f"Bearer {account_token}"}
            ).json()
            check(
                "删除后真的空了",
                not after_del.get("memories") and not after_del.get("variant_feedbacks"),
                f"{after_del}",
            )
    finally:
        if not keep:
            print("⑧ 清理本次造出来的行")
            for user_id in created_user_ids:
                # 账户身份（合并时新建的）也指向这个 users 行 ⇒ 必须**先**删它，
                # 否则删 users 会撞外键（实测：FOREIGN KEY constraint failed）。
                account_identity = db.execute(
                    select(AnonymousIdentity).where(AnonymousIdentity.merged_user_id == user_id)
                ).scalars().all()
                for identity in account_identity:
                    db.execute(
                        delete(PreferenceMemory).where(
                            PreferenceMemory.identity_id == identity.id
                        )
                    )
                    db.execute(
                        delete(VariantFeedback).where(
                            VariantFeedback.identity_id == identity.id
                        )
                    )
                    db.execute(delete(AnonymousIdentity).where(AnonymousIdentity.id == identity.id))
                db.execute(delete(User).where(User.id == user_id))
            for identity_id in created_identity_ids:
                db.execute(
                    delete(PreferenceMemory).where(PreferenceMemory.identity_id == identity_id)
                )
                db.execute(
                    delete(VariantFeedback).where(VariantFeedback.identity_id == identity_id)
                )
                db.execute(delete(AnonymousIdentity).where(AnonymousIdentity.id == identity_id))
            db.commit()
            print(f"  已清理 {len(created_identity_ids)} 个匿名身份 / {len(created_user_ids)} 个账户")
        db.close()

    failed = [c for c in CHECKS if not c[1]]
    print()
    print(f"功能校验：{len(CHECKS) - len(failed)}/{len(CHECKS)} 通过")
    for name, _ok, detail in failed:
        print(f"  FAIL {name} —— {detail}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跨会话记忆的端到端功能校验（真数据库）")
    parser.add_argument("--seed-car", action="store_true", help="库里没有在售车时造一台（CI 用）")
    parser.add_argument("--keep", action="store_true", help="不清理本次造出来的行")
    args = parser.parse_args(argv)
    # ⚠️ 连接串**必须脱敏后**再打印（2026-10-21 代码审查 Blocker）：
    # 生产连接串带「用户名:口令@主机」前缀，原样打印＝
    # **口令明文进 CI 日志 / 终端历史**。
    url = os.environ.get("DATABASE_URL", "")
    print(f"目标数据库：{describe_database_url(url)}")
    return run(args.seed_car, args.keep)


if __name__ == "__main__":
    sys.exit(main())
