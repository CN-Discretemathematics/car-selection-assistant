"""跨会话偏好记忆：匿名身份、封闭词表、回填、覆盖式更新。

## 这个 module 解决什么

回访用户不必重讲一遍「预算多少、要什么车、不要什么车」。

## 五个不可让步的约束

1. **回填走 `merge_profile`**——全仓**只有那一个**画像合并器。
   新建第二个合并器 = 两套口径必然漂移（仓库反复记录过的教训：早先的手写版把
   「空间优先」算成 0.20，正则路径算成 0.30，同一句话两种结果）。

2. **词表封闭**——只持久化 6 个键，值必须落在**既有**封闭枚举里
   （`app/common/enums.py` 的 `ENERGY_TYPES` / `BODY_TYPES`、
   `engine._USAGE_HINTS` 的值）。新造维度必须先证明「确定性内核会算它」，
   否则写进去的东西影响不了推荐，只是噪声。

3. **推断通道的禁区**——`body_type` / `energy_preference` / `budget` 可以因
   **用户明说**而持久化（`explicit`），但**不得**由模型推断产生（`inferred`）：
   推断猜错会**静默砍掉整个候选集**，用户看不到原因，只看到「推的都不是我要的」。

4. **矛盾消解**——同一个值不能既被要求又被排除：
   `body_type IN ('suv') AND body_type NOT IN ('suv')` = 候选集恒空，
   而用户看到的只是一句「没有合适的车」。

5. **三模式闸门**——`DURABLE_PREFS_MODE=off` 时**完全不介入**（读 + 写都短路），
   不只「不写库」：否则切回 off 时，之前 on 模式写下的记忆仍会悄悄影响推荐。

## 刻意不做的事

- **不持久化权重**。`merge_profile` 的权重是**累加**语义（`profile.weights[dim] += w`），
  跨会话恢复会与本轮的累加叠加成「一句话叠三轮」。权重记忆的前置条件是先定义
  「恢复的是绝对值还是增量」——那是 Phase 2 的独立决策，不在 Phase 1 里含糊处理。
- **不持久化模型推断的偏好**。`extract_hints` 是确定性正则，抽出来的都是用户**原话**里的；
  LLM 抽取（L1 `soft_prefs`）的产物**不进**记忆。
- **不提供「取消某项偏好」的显式语义**。当前只能通过 `DELETE /agent/preferences/{id}`
  逐条删除（用户自查入口）。「预算不限」这类话解析不出预算线索，记忆里的旧预算仍在。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.schemas import UserProfile
from app.common.enums import BODY_TYPES, ENERGY_TYPES
from app.common.models import AnonymousIdentity, PreferenceMemory

_logger = logging.getLogger("app.agent.preferences")

# ── 模式闸门 ────────────────────────────────────────────────────────────────

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ON = "on"
VALID_MODES = (MODE_OFF, MODE_SHADOW, MODE_ON)

#: 环境变量名（与 L1/L2 的 `AGENT_SOFT_PREF_MODE` 同族）
MODE_ENV = "DURABLE_PREFS_MODE"


def get_mode() -> str:
    """当前模式。取值非法时**回退到 off**（安全侧：不写库、不回填）。"""
    raw = (os.environ.get(MODE_ENV) or MODE_OFF).strip().lower()
    return raw if raw in VALID_MODES else MODE_OFF


def is_enabled() -> bool:
    """是否允许写库并回填（仅 `on`）。

    `off` 与 `shadow` 都**不回填**：回填会改变推荐结果，那已经不是「观察」了。
    """
    return get_mode() == MODE_ON


# ── 身份 ────────────────────────────────────────────────────────────────────


def hash_token(token: str) -> str:
    """token → sha256 十六进制。**库里只存哈希**，明文只交给客户端一次。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    """生成随机不可猜的 token。"""
    return secrets.token_urlsafe(32)


def get_or_create_identity(
    db: Session, token: str | None = None
) -> tuple[AnonymousIdentity, str | None, bool]:
    """按 token 取身份；没有就给一个新的。

    返回 `(identity, plaintext_token, created)`。
    **plaintext_token 仅在新建时非 None**——明文只在那一刻给一次，
    之后库里只有哈希，无法再取回（这是刻意的：明文可重置，泄漏的哈希不可反推）。
    """
    if token:
        found = get_identity_by_token(db, token)
        if found is not None:
            return found, None, False
    plaintext = new_token()
    identity = AnonymousIdentity(token_hash=hash_token(plaintext))
    db.add(identity)
    db.flush()
    return identity, plaintext, True


def get_identity_by_token(db: Session, token: str) -> AnonymousIdentity | None:
    """按 token 找身份；找不到返回 None。"""
    if not token:
        return None
    return db.scalar(
        select(AnonymousIdentity).where(AnonymousIdentity.token_hash == hash_token(token))
    )


# ── 封闭词表：被跟踪的键 ────────────────────────────────────────────────────

CATEGORY_USAGE = "usage"          # 用途
CATEGORY_CONSTRAINT = "constraint"  # 硬约束（车身 / 能源 / 预算 / 人数）
CATEGORY_EXCLUSION = "exclusion"  # 排除项

VALID_CATEGORIES = frozenset({CATEGORY_USAGE, CATEGORY_CONSTRAINT, CATEGORY_EXCLUSION})


@dataclass(frozen=True)
class TrackedKey:
    """一个可持久化的键：它落在哪个类别、值是什么形状、是否禁止推断产生。"""

    key: str
    category: str
    #: "str_list" | "int" | "budget"
    kind: str
    #: True = 只能由用户明说产生（`explicit`），模型推断（`inferred`）一律拒绝
    infer_forbidden: bool = False
    #: True = 值域**不在静态枚举里**，由写库路径查库解析校验（品牌名就是这种：
    #: 品牌注册表是动态的）。
    dynamic_vocabulary: bool = False


#: **被持久化的键就是全部**——不在表里的键一律不写。
#:
#: 取这几个键的依据：它们全部是 `merge_profile` 真正会消费、且 `tools.py` 真会
#: 下推 SQL 的字段。写了不影响推荐的字段不进表。
TRACKED_KEYS: tuple[TrackedKey, ...] = (
    TrackedKey("usage", CATEGORY_USAGE, "str_list"),
    TrackedKey("body_type", CATEGORY_CONSTRAINT, "str_list", infer_forbidden=True),
    TrackedKey("energy_preference", CATEGORY_CONSTRAINT, "str_list", infer_forbidden=True),
    TrackedKey("budget", CATEGORY_CONSTRAINT, "budget", infer_forbidden=True),
    TrackedKey("passengers", CATEGORY_CONSTRAINT, "int"),
    TrackedKey("avoid", CATEGORY_EXCLUSION, "str_list"),
    # 品牌排除项（「不要日系」）：值是**品牌名**而不是 brand_id——
    # 库重导入时 id 会重排（评测锚点 406/410 那个坑），名字与 id 无关 ✓。
    TrackedKey(
        "brand_exclude", CATEGORY_EXCLUSION, "str_list", dynamic_vocabulary=True
    ),
)

TRACKED_BY_KEY: dict[str, TrackedKey] = {t.key: t for t in TRACKED_KEYS}


def _usage_values() -> set[str]:
    """允许的用途值（`engine._USAGE_HINTS` 的值——用户说「家用/通勤」落到的枚举）。"""
    from app.agent.engine import _USAGE_HINTS

    return set(_USAGE_HINTS.values())


def _energy_values() -> set[str]:
    """允许的能源值（`expand_energy_prefs` 认识的全部：5 个具体类型 + 2 个大类）。"""
    return set(ENERGY_TYPES) | {"new_energy", "fuel"}


def _avoid_values() -> set[str]:
    """允许的排除值（`expand_avoid` 认识的全部：能源 + 车身）。"""
    return set(ENERGY_TYPES) | set(BODY_TYPES) | {"new_energy", "fuel"}


def allowed_values(key: str) -> set[str] | None:
    """该键允许的**值**集合；`None` 表示该键不做值域校验（数值型）。"""
    if key == "usage":
        return _usage_values()
    if key == "body_type":
        return set(BODY_TYPES)
    if key == "energy_preference":
        return _energy_values()
    if key == "avoid":
        return _avoid_values()
    return None


# ── 值的编码 / 解码 ─────────────────────────────────────────────────────────


def encode_value(key: str, value: Any) -> str:
    """把值编码成存库字符串（列表/预算 → JSON；人数 → 十进制）。"""
    spec = TRACKED_BY_KEY[key]
    if spec.kind == "int":
        return str(int(value))
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def decode_value(key: str, raw: str) -> Any:
    """把存库字符串解回值。解码失败返回 None（脏数据不炸整条链路）。"""
    spec = TRACKED_BY_KEY[key]
    if spec.kind == "int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


# ── 校验 ────────────────────────────────────────────────────────────────────


def validate_memory(
    *, category: str, key: str, value: str, source_type: str
) -> tuple[bool, str]:
    """校验一条记忆是否符合封闭词表与推断禁区。

    返回 `(ok, reason)`。**不合规的记忆不写库**——
    写进去的东西若影响不了推荐，只是噪声；若是禁区字段被推断污染，
    会静默砍掉候选集。
    """
    if category not in VALID_CATEGORIES:
        return False, f"未知类别 {category!r}（封闭词表）"
    if source_type not in ("explicit", "inferred", "behavioral"):
        return False, f"未知来源类型 {source_type!r}"
    spec = TRACKED_BY_KEY.get(key)
    if spec is None:
        return False, f"键 {key!r} 不在可持久化词表内（封闭词表）"
    if spec.category != category:
        return False, f"键 {key!r} 不属于类别 {category!r}（应为 {spec.category!r}）"
    if source_type == "inferred" and spec.infer_forbidden:
        return False, (
            f"{key!r} 属推断禁区：只能由用户明说产生，"
            f"推断猜错会静默砍掉整个候选集"
        )
    decoded = decode_value(key, value)
    if decoded is None:
        return False, f"值 {value!r} 不是 {key!r} 的合法编码"
    ok, reason = _validate_decoded(key, spec, decoded)
    if not ok:
        return False, reason
    return True, ""


def _validate_decoded(key: str, spec: TrackedKey, decoded: Any) -> tuple[bool, str]:
    """按形状与值域校验解码后的值。"""
    if spec.kind == "int":
        if not isinstance(decoded, int) or isinstance(decoded, bool):
            return False, f"{key!r} 的人数必须是整数"
        if not 1 <= decoded <= 20:
            return False, f"{key!r} 的人数 {decoded} 超出合理范围（1–20）"
        return True, ""
    if spec.kind == "budget":
        if not isinstance(decoded, dict):
            return False, f"{key!r} 的预算必须是 {{min, max}} 对象"
        if set(decoded) - {"min", "max"}:
            return False, f"{key!r} 的预算只接受 min / max 两个键"
        values = [v for v in decoded.values() if v is not None]
        if not values:
            return False, f"{key!r} 的预算为空"
        if any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in values):
            return False, f"{key!r} 的预算必须是数值"
        if any(v < 0 for v in values):
            return False, f"{key!r} 的预算不能为负"
        lo, hi = decoded.get("min"), decoded.get("max")
        if lo is not None and hi is not None and lo > hi:
            return False, f"{key!r} 的预算下限 {lo} 高于上限 {hi}"
        return True, ""
    # str_list
    if not isinstance(decoded, list) or not decoded:
        return False, f"{key!r} 的值必须是非空列表"
    if any(not isinstance(v, str) for v in decoded):
        return False, f"{key!r} 的列表元素必须是字符串"
    if TRACKED_BY_KEY[key].dynamic_vocabulary:
        # 值域是**动态**的（品牌名 → 品牌注册表）：静态枚举查不到，
        # 由写库路径查库解析校验（见 `resolve_brand_names`）。
        return True, ""
    allowed = allowed_values(key) or set()
    bad = sorted({v for v in decoded if v not in allowed})
    if bad:
        return False, f"{key!r} 的值 {bad} 不在封闭词表内"
    return True, ""


# ── 写 ──────────────────────────────────────────────────────────────────────


@dataclass
class WriteOutcome:
    """一次写入的结果。"""

    written: bool
    reason: str
    identity_id: int | None = None
    key: str | None = None


def remember(
    db: Session,
    identity: AnonymousIdentity,
    *,
    category: str,
    key: str,
    value: str,
    source_type: str = "explicit",
    confidence: float = 1.0,
    confirmed: bool = False,
    evidence_span: str | None = None,
    source_message_id: int | None = None,
) -> WriteOutcome:
    """写一条记忆（**每个键只留一条**，新值覆盖旧值）。

    纪律：
    - `confirmed=False` 的**不写**（只有用户明说或确认过的才落库）；
    - 词表不合规的**不写**；
    - 覆盖语义：同一 `(identity, category, key)` 只有一行，写新值即更新那一行
      （行 id 稳定，用户删的是同一条）；重复写同一个值是幂等的；
    - `off` 模式**不写**（只落日志）。

    ⚠️ 这里只 `flush`、**不 commit**——提交由调用方决定（生产路径是
    `persist_hints`，它一次提交全部键）。
    """
    ok, reason = validate_memory(
        category=category, key=key, value=value, source_type=source_type
    )
    if not ok:
        _logger.info("记忆不合规，未写入: %s", reason)
        return WriteOutcome(written=False, reason=reason, identity_id=identity.id, key=key)
    if not confirmed:
        reason = "未确认的记忆不写入（只有用户明说或确认过的才写入）"
        _logger.info("记忆未确认，未写入: %s/%s", category, key)
        return WriteOutcome(written=False, reason=reason, identity_id=identity.id, key=key)
    mode = get_mode()
    if mode != MODE_ON:
        _logger.info("记忆未写入（模式 %s）: %s/%s", mode, category, key)
        return WriteOutcome(
            written=False, reason=f"模式 {mode} 不写库", identity_id=identity.id, key=key
        )

    canonical = encode_value(key, decode_value(key, value))
    existing = db.scalar(
        select(PreferenceMemory).where(
            PreferenceMemory.identity_id == identity.id,
            PreferenceMemory.category == category,
            PreferenceMemory.key == key,
        )
    )
    if existing is not None:
        # 覆盖语义：同一键只留一行。行 id 不变，用户「删掉这条记忆」删的是同一条。
        existing.value = canonical
        existing.source_type = source_type
        existing.confidence = confidence
        existing.confirmed = True
        if evidence_span:
            existing.evidence_span = evidence_span
        if source_message_id is not None:
            existing.source_message_id = source_message_id
        db.flush()
        return WriteOutcome(
            written=True, reason="已更新（覆盖同键旧值）", identity_id=identity.id, key=key
        )

    db.add(
        PreferenceMemory(
            identity_id=identity.id,
            category=category,
            key=key,
            value=canonical,
            source_type=source_type,
            confidence=confidence,
            confirmed=True,
            evidence_span=evidence_span,
            source_message_id=source_message_id,
        )
    )
    db.flush()
    return WriteOutcome(written=True, reason="已写入", identity_id=identity.id, key=key)


def forget(db: Session, identity: AnonymousIdentity, key: str) -> bool:
    """删掉某个键上的记忆（该键的约束已经不成立了）。"""
    spec = TRACKED_BY_KEY.get(key)
    if spec is None:
        return False
    rows = list(
        db.scalars(
            select(PreferenceMemory).where(
                PreferenceMemory.identity_id == identity.id,
                PreferenceMemory.category == spec.category,
                PreferenceMemory.key == key,
            )
        )
    )
    for row in rows:
        db.delete(row)
    if rows:
        db.flush()
    return bool(rows)


# ── 记忆删除（用户说「把之前记的预算忘掉」）────────────────────────────────
#: 删除请求的动词。
_FORGET_VERB_RE = re.compile(
    r"(忘掉|忘记|删掉|删除|清除|清空|别记|不要记|不用记|不再记)"
)
#: **指代记忆**的词。删除动词 + 指代记忆，**两者都要有**才算删除请求——
#: 否则「删除 SUV」这种约束变更会被误判成「忘掉车形记忆」（实测口径：宁可漏判不可错判）。
_FORGET_REF_RE = re.compile(r"(之前|上次|以前|记的|说过|我的|原来|前面)")

#: 主体词 → 被跟踪的键。
_FORGET_SUBJECTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("budget", (r"预算", r"价格", r"价位")),
    ("usage", (r"用途", r"场景", r"用车")),
    ("passengers", (r"人数", r"几个人", r"坐几")),
    ("energy_preference", (r"能源", r"动力类型", r"纯电", r"插混", r"增程", r"燃油")),
    ("body_type", (r"车身", r"轿车", r"suv", r"mpv", r"皮卡")),
    ("avoid", (r"排除项", r"不要的", r"不想要的")),
    ("brand_exclude", (r"品牌", r"牌子")),
)

#: 键 → 用户看得懂的名字（回执用）。
_FORGET_KEY_LABELS: dict[str, str] = {
    "usage": "用途",
    "body_type": "车身类型",
    "energy_preference": "能源偏好",
    "budget": "预算",
    "passengers": "人数",
    "avoid": "排除项",
    "brand_exclude": "品牌排除项",
}


def detect_forget_request(message: str) -> list[str]:
    """用户要忘掉哪些记忆？空列表 = 不是删除请求。

    判据（**两者都要有**）：删除动词 + 指代记忆的词。
    「把之前记的预算忘掉」「别记我的预算了」「删掉上次说的用途」→ 命中 ✓；
    「删除 SUV」「预算 20 万」→ **不命中**（前者是约束变更，后者是普通诉求）✓。
    """
    text = message or ""
    if not _FORGET_VERB_RE.search(text) or not _FORGET_REF_RE.search(text):
        return []
    if re.search(r"(全部|所有|都忘|都删)", text):
        return [spec.key for spec in TRACKED_KEYS]
    keys: list[str] = []
    for key, patterns in _FORGET_SUBJECTS:
        if key in keys:
            continue
        if any(re.search(p, text, re.IGNORECASE) for p in patterns):
            keys.append(key)
    return keys


def forget_keys(
    db: Session,
    identity: AnonymousIdentity | None,
    keys: list[str],
) -> list[str]:
    """执行删除：把**跨会话记忆**里那几个键的行删掉。

    返回真删掉了的键。

    ⚠️ **不动会话画像**（当前对话里已谈定的条件继续用）——与设置页的撤回端点
    （`DELETE /agent/preferences/{id}`）**同一语义**，两处口径不许分叉。
    代价是：用户说完「忘掉」后，这段对话里仍按之前的预算推荐——
    所以回执必须说清范围（见 `forget_reply`），否则就是骗人。
    """
    cleared: list[str] = []
    if identity is None:
        return cleared
    for key in keys:
        if forget(db, identity, key):
            cleared.append(key)
    return cleared


def forget_reply(keys: list[str]) -> str:
    """删除回执（**口径变化必须对用户可见**——与解锁说明同一原则）。

    范围要说清：删掉的是**跨会话记忆**；当前这段对话里已谈定的条件仍在
    （要立刻改就直接说新的预算/条件）。
    """
    if not keys:
        return "你之前记的条件里没有这一项。"
    names = "、".join(_FORGET_KEY_LABELS.get(k, k) for k in keys)
    return (
        f"已忘掉之前记的{names}——之后的对话不再按它筛；"
        "这段对话里要改条件，直接说新的就行。"
    )


# ── 读 ──────────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def list_memories(
    db: Session, identity: AnonymousIdentity, *, include_expired: bool = False
) -> list[PreferenceMemory]:
    """列出某身份的记忆。默认**排除已过期的**（推断类记忆会过期）。

    ⚠️ 当前**没有**推断类记忆（`persist_hints` 只写 `source_type="explicit"`），
    所以 `expires_at` 恒为 NULL ⇒ 这条过滤**永不命中**。
    留着是为了将来放开推断类记忆时 TTL 有兜底（见 models.PreferenceMemory.expires_at
    的注释与计划 §七 的结论）。
    """
    stmt = select(PreferenceMemory).where(
        PreferenceMemory.identity_id == identity.id
    )
    if not include_expired:
        now = _now()
        stmt = stmt.where(
            (PreferenceMemory.expires_at.is_(None))
            | (PreferenceMemory.expires_at > now)
        )
    return list(db.scalars(stmt).all())


def memories_to_hints(db: Session, identity: AnonymousIdentity) -> dict[str, Any]:
    """把记忆翻译成 **hints 形状**（`extract_hints` 的同构输出）。

    **翻译器在这里，合并器不在**——合并一律交给 `merge_profile`。
    这就是「回填复用唯一合并路径」的实现方式：记忆先变成 hints，
    再走那条**已被无数测试钉住**的路径。
    """
    hints: dict[str, Any] = {}
    for mem in list_memories(db, identity):
        spec = TRACKED_BY_KEY.get(mem.key)
        if spec is None:
            continue
        decoded = decode_value(mem.key, mem.value)
        if decoded is None:
            continue
        if spec.kind == "int":
            hints[mem.key] = decoded
        elif spec.kind == "budget":
            hints[mem.key] = {
                "min": decoded.get("min"),
                "max": decoded.get("max"),
            }
        else:
            hints[mem.key] = list(decoded)
    return hints


# ── 矛盾消解 ────────────────────────────────────────────────────────────────


def _turn_level(turn_hints: dict[str, Any] | None) -> tuple[set[str], set[str]]:
    """本轮明说 → 消解的第一层（正面诉求, 排除项）。"""
    hints = turn_hints or {}
    return (
        set(hints.get("body_type") or []) | set(hints.get("energy_preference") or []),
        set(hints.get("avoid") or []),
    )


def _resolve_conflicts(
    levels: list[tuple[set[str], set[str]]],
) -> tuple[set[str], set[str]]:
    """消解「同一个值既被要求又被排除」。

    `levels` 从**新到旧**排列，每项是 (正面诉求, 排除项)：

    | 层 | 含义 |
    | --- | --- |
    | 1 | 本轮用户明说的 |
    | 2 | 本会话已经说出口的 |
    | 3 | 记忆里存的 |

    同一个值出现在多层时，**靠前（更新）的层说了算**——
    用户今天说的话压过上个月记的，本轮说的压过本会话早先说的。

    同一层内既要求又排除 → 以**排除**为准：宁可少推一台车，
    好过推一台用户明确排斥的车（`body_type IN ('suv') AND body_type NOT IN ('suv')`
    会让候选集恒空，而用户只看到一句「没有合适的车」）。

    返回 `(positive, negative)`，两个集合**互斥**。

    ⚠️ 这条规则**只有这一处实现**。此前散在调用点上有两遍（一次按「本轮的排除」
    过滤、一次按「最终排除集」过滤），两遍规则不同、互相掩盖——
    变异测试因此漏掉了两处。规则散落 = 规则漂移。
    """
    positive: set[str] = set()
    negative: set[str] = set()
    for pos, neg in levels:
        free = {v for v in pos | neg if v not in positive and v not in negative}
        both = free & pos & neg
        positive |= (free & pos) - both
        negative |= (free & neg) | both
    return positive, negative


# ── 与会话的接线 ────────────────────────────────────────────────────────────


def _merge_profile(profile: UserProfile, hints: dict[str, Any]) -> UserProfile:
    """把 hints 并进 profile。**合并器不在本 module**——全仓只有那一个。

    本分支的基线上 `merge_profile` 定义在 `app/agent/engine.py`；另一条收拢分支
    （refactor/knowledge-consolidation）把它搬到 `app/agent/profile.py`，并且
    engine 仍对外 re-export 同一个名字。所以这里取 `engine` 的那个名字，
    **两个布局下都指向同一实现**——不会因为布局变化而冒出「两个合并器」。

    import 放在函数体内：`engine` 顶层 import 本 module（本 module 也需要
    `engine` 的合并器），顶层互相 import 会成环——这是收拢前的既成事实，
    收拢落地后应改为直接 import `app/agent/profile`。
    """
    from app.agent.engine import merge_profile

    return merge_profile(profile, hints)


def _profile_tracked(profile: UserProfile) -> dict[str, Any]:
    """从画像里取出被跟踪的键（值 → hints 形状）。"""
    out: dict[str, Any] = {}
    if profile.usage:
        out["usage"] = sorted(set(profile.usage))
    if profile.body_type:
        out["body_type"] = list(profile.body_type)
    if profile.energy_preference:
        out["energy_preference"] = list(profile.energy_preference)
    if profile.budget.min is not None or profile.budget.max is not None:
        out["budget"] = {"min": profile.budget.min, "max": profile.budget.max}
    if profile.passengers is not None:
        out["passengers"] = profile.passengers
    if profile.avoid:
        out["avoid"] = list(profile.avoid)
    return out


def effective_tracked(
    db: Session, identity: AnonymousIdentity, turn_hints: dict[str, Any] | None
) -> dict[str, Any]:
    """「旧记忆 + 本轮明说」→ 有效值。

    合并**走 `merge_profile`**（不自己合并）：先并记忆，再并本轮——
    本轮的覆盖语义因此自动压过记忆（`usage` / `body_type` / `budget` / `passengers`
    是覆盖，`avoid` 是并集）。

    返回**只含被跟踪的键**——权重不在跟踪表里，因此永远不在返回值里。
    """
    mem = memories_to_hints(db, identity)
    profile = UserProfile()
    if mem:
        _merge_profile(profile, mem)
    if turn_hints:
        _merge_profile(profile, turn_hints)
    positives, negatives = _resolve_conflicts(
        [
            _turn_level(turn_hints),
            (
                set(profile.body_type) | set(profile.energy_preference),
                set(profile.avoid),
            ),
        ]
    )
    out = _profile_tracked(profile)
    body = [b for b in profile.body_type if b in positives]
    energy = [e for e in profile.energy_preference if e in positives]
    avoid = [a for a in profile.avoid if a in negatives]
    if body:
        out["body_type"] = body
    else:
        out.pop("body_type", None)
    if energy:
        out["energy_preference"] = energy
    else:
        out.pop("energy_preference", None)
    if avoid:
        out["avoid"] = avoid
    else:
        out.pop("avoid", None)
    return out


# ── 品牌排除项 ──────────────────────────────────────────────────────────────


def resolve_brand_names(db: Session, names: list[str]) -> tuple[list[int], list[str]]:
    """品牌名 → (brand_ids, 规范名)。**解析不到的丢弃**。

    为什么用 `resolve_brand_mentions`：它是全仓唯一的「品牌名 → id」实现
    （含「长名优先」与「车系名消歧」两条实测教训），不另抄一份。

    为什么存**名字**而不是 brand_id：库重导入时 id 会重排
    （评测锚点 406/410 那个坑——题库的 id 指向的车与题面说的不是同一台）。
    名字与 id 无关 ✓，所以记忆里存名字。
    """
    from app.catalog.brands import resolve_brand_mentions

    ids: list[int] = []
    labels: list[str] = []
    for name in names:
        hints = resolve_brand_mentions(db, name, assume_constraint=True)
        for bid, label in zip(
            hints.get("brand_ids") or [],
            hints.get("brand_labels") or [],
            strict=False,
        ):
            if bid not in ids:
                ids.append(int(bid))
                labels.append(label)
    return ids, labels


def _memory_value(
    db: Session, identity: AnonymousIdentity, key: str
) -> list[str]:
    """读某个键上的记忆值（列表形状）。"""
    spec = TRACKED_BY_KEY.get(key)
    if spec is None:
        return []
    for mem in list_memories(db, identity):
        if mem.key != key:
            continue
        decoded = decode_value(key, mem.value)
        if isinstance(decoded, list):
            return [str(v) for v in decoded]
    return []


def effective_brand_exclusions(
    db: Session,
    identity: AnonymousIdentity | None,
    profile: UserProfile,
    turn_brand_hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """记忆里的品牌排除项 → **生效**的排除项（hints 形状）。

    去掉用户**这轮**与**本会话**已经要求的品牌：旧记忆不能把用户明说的正向
    约束删掉——「我就要比亚迪」被记忆里的「不要比亚迪」直接删掉，推荐结果会
    **空掉**，而用户只看到一句「没有合适的车」（计划 §1.2：硬约束排除项错了
    直接毁掉答案）。

    这正是三层消解规则在品牌维度上的落法：**本轮 > 本会话 > 记忆**。
    """
    if identity is None or not is_enabled():
        return {}
    turn = turn_brand_hints or {}
    names = list(_memory_value(db, identity, "brand_exclude"))
    # 本轮的排除项（「这一轮说不要比亚迪」比会话里更早的要求更新）
    for label in turn.get("brand_exclude_labels") or []:
        label = str(label)
        if label not in names:
            names.append(label)
    if not names:
        return {}
    # 被「要求」的品牌不受排除：但**本轮自己说的排除**优先于更早的要求
    # （三层规则：本轮 > 本会话 > 记忆）。
    turn_excluded = {str(excluded) for excluded in turn.get("brand_exclude_labels") or []}
    protected = (
        set(profile.brand_labels or [])
        | {str(brand_label) for brand_label in turn.get("brand_labels") or []}
    ) - turn_excluded
    kept = [n for n in names if n not in protected]
    if not kept:
        return {}
    ids, labels = resolve_brand_names(db, kept)
    if not ids:
        return {}
    return {"brand_exclude_ids": ids, "brand_exclude_labels": labels}


def restore_into_profile(
    db: Session,
    identity: AnonymousIdentity | None,
    profile: UserProfile,
    *,
    turn_hints: dict[str, Any] | None = None,
    brand_exclusions: dict[str, Any] | None = None,
) -> UserProfile:
    """会话开始时把**旧记忆**回填进画像。

    `off` / `shadow` 模式下**什么都不做**——「行为与接入前逐位相同」是三模式
    闸门的底线：`off` 不只是不写库，是**完全不介入**（否则切回 off 时，
    之前 on 模式写下的记忆仍会悄悄影响推荐）。

    ## 回填走 `merge_profile`（唯一合并器）

    只把**会话还没有的**字段交给它——会话内的是更新的事实：
    会话内的画像比记忆新，无条件覆盖会让「今天说 30 万」在下一轮被
    「记忆里的 20 万」悄悄改回去，用户看到的是「我明明说了 30 万，怎么又变了」。

    排除项是**并集**（`merge_profile` 的语义 ✓）：排除不会「过期」。
    ⚠️ 会话自己已有的排除项**原样保留**：会话内的「既要求又排除」是本仓**既有**的
    缺陷（`merge_profile` 从不清除旧的正面诉求），修它属于独立改动——
    在这里顺手改会让「同一个对话在有/无身份时行为不同」。

    ## 回填必须在**本轮线索合并之前**调用

    否则记忆会盖住本轮：本轮说「SUV 也行」，记忆里还挂着「不要 SUV」，
    本轮的正面诉求被旧记忆压掉。

    ## 品牌排除项由调用方算好传进来

    它需要**本轮与本会话的品牌要求**才能消解冲突（见
    `effective_brand_exclusions`）——本 module 拿不到「用户这轮要哪个品牌」。
    """
    if identity is None or not is_enabled():
        return profile
    mem = memories_to_hints(db, identity)
    if not mem and not brand_exclusions:
        return profile

    positives, negatives = _resolve_conflicts(
        [
            _turn_level(turn_hints),
            (
                set(profile.body_type) | set(profile.energy_preference),
                set(profile.avoid),
            ),
            (
                set(mem.get("body_type") or []) | set(mem.get("energy_preference") or []),
                set(mem.get("avoid") or []),
            ),
        ]
    )
    body = [b for b in (mem.get("body_type") or []) if b in positives]
    energy = [e for e in (mem.get("energy_preference") or []) if e in positives]
    avoid = [a for a in (mem.get("avoid") or []) if a in negatives]

    # 只把「会话还没有的」字段交给合并器（会话内的是更新的事实）
    hints: dict[str, Any] = {}
    if not profile.body_type and body:
        hints["body_type"] = body
    if not profile.energy_preference and energy:
        hints["energy_preference"] = energy
    if not profile.usage and mem.get("usage"):
        hints["usage"] = list(mem["usage"])
    if (
        profile.budget.min is None
        and profile.budget.max is None
        and mem.get("budget")
    ):
        hints["budget"] = mem["budget"]
    if profile.passengers is None and mem.get("passengers") is not None:
        hints["passengers"] = mem["passengers"]
    if avoid:
        hints["avoid"] = avoid
    if brand_exclusions:
        hints.update(brand_exclusions)
    if hints:
        _merge_profile(profile, hints)
    return profile


def persist_hints(
    db: Session,
    identity: AnonymousIdentity | None,
    hints: dict[str, Any],
    *,
    brand_exclusions: dict[str, Any] | None = None,
    evidence_span: str | None = None,
) -> list[WriteOutcome]:
    """把**本轮明说**的线索写成记忆。

    `extract_hints` 是**确定性正则**——它抽出来的东西是用户**原话**里的，
    所以 `source_type=explicit`、`confirmed=True`。

    ⚠️ 只写**用户明说**的（explicit），**不写模型推断的**——
    推断猜错会静默砍掉整个候选集（见 `TrackedKey.infer_forbidden`）。
    `off` 模式下不写任何东西；`shadow` 模式只落日志（不写库、不回填）。

    ## 品牌排除项由调用方算好传进来

    品牌约束不在 `extract_hints` 里（要查库解析品牌名），且它的生效值需要
    **本轮与本会话的品牌要求**才能消解冲突——所以由 engine 调
    `effective_brand_exclusions` 算好后传进来（`brand_exclusions`）。

    ## 自己提交

    `app/common/database.py::get_session` **不提交**——请求结束即回滚。
    记忆是「下次还要用」的数据，只 flush 不 commit 等于没写。
    这里提交一次（沿用 `conversation_log.record_turn` 的既有范式），
    失败只记 warning、绝不影响用户这一轮的正常回答。
    """
    if identity is None:
        return []
    mode = get_mode()
    if mode == MODE_OFF:
        return []

    effective = effective_tracked(db, identity, hints)
    if brand_exclusions:
        labels = [str(v) for v in (brand_exclusions.get("brand_exclude_labels") or [])]
        if labels:
            effective["brand_exclude"] = labels
    if mode == MODE_SHADOW:
        _logger.info(
            "偏好记忆（shadow，未写库）: %s",
            json.dumps(effective, ensure_ascii=False, sort_keys=True),
        )
        return []

    outcomes: list[WriteOutcome] = []
    try:
        for key, value in effective.items():
            spec = TRACKED_BY_KEY[key]
            outcomes.append(
                remember(
                    db,
                    identity,
                    category=spec.category,
                    key=key,
                    value=encode_value(key, value),
                    source_type="explicit",
                    confirmed=True,
                    evidence_span=evidence_span,
                )
            )
        # 有效值消失的键 → 删掉旧记忆（约束已经不成立了）。
        for key in TRACKED_BY_KEY:
            if key not in effective:
                forget(db, identity, key)
        db.commit()
    except Exception:  # noqa: BLE001 —— 记忆写失败绝不影响用户这一轮的正常回答
        db.rollback()
        _logger.warning("偏好记忆写入失败（已忽略，不影响响应）", exc_info=True)
        return []
    return outcomes
