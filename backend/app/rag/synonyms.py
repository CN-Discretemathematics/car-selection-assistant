"""查询同义扩展（优化⑥）：领域同义词表驱动的确定性查询改写。

- 词面：口语/别称 → 索引用语（「纯电续航」↔「CLTC纯电续航里程」「续航里程」）；
- 诉求：意图词 → 参数证据词（「省油」→「馈电油耗/综合油耗」，「能装」→「后备厢容积」）；
- 只做**追加**不改写原查询（BM25 词袋：扩展词只可能增加命中，IDF 自动压低通用词）；
- 匹配基于 jieba 分词后的整词 + 子串包含两级，确定性、无 LLM 调用。
"""
from __future__ import annotations

import re

from app.retrieval.backends import _get_jieba

# 口语/别称 → 证据词（追加进查询；顺序即追加顺序）
DOMAIN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "纯电续航": ("纯电续航里程", "CLTC纯电续航里程", "WLTC纯电续航里程"),
    "续航": ("纯电续航里程", "综合续航"),
    "综合续航": ("CLTC综合续航",),
    "油耗": ("馈电油耗", "综合油耗", "WLTC综合油耗"),
    "省油": ("馈电油耗", "综合油耗", "油电混动"),
    "费油": ("馈电油耗", "综合油耗"),
    "耗电": ("百公里耗电量",),
    "电耗": ("百公里耗电量",),
    "电池": ("电池能量", "电池类型", "电池快充时间"),
    "充电": ("快充时间", "慢充时间", "对外放电"),
    "快充": ("电池快充时间", "充电接口"),
    "轴距": ("车身尺寸",),
    "尺寸": ("长*宽*高", "轴距"),
    "空间": ("长*宽*高", "轴距", "后备厢容积"),
    "能装": ("后备厢容积", "行李厢容积"),
    "座位": ("座位数",),
    "几座": ("座位数",),
    "马力": ("最大马力", "最大功率"),
    "动力": ("最大功率", "最大扭矩", "官方0-100km/h加速"),
    "加速": ("官方0-100km/h加速",),
    "扭矩": ("最大扭矩",),
    "价格": ("官方指导价", "经销商报价"),
    "多少钱": ("官方指导价",),
    "价位": ("官方指导价",),
    "配置": ("舒适配置", "智能座舱"),
    "智能": ("智能座舱", "辅助驾驶"),
    "智驾": ("辅助驾驶", "自适应巡航", "车道保持", "自动泊车"),
    "雷达": ("激光雷达", "毫米波雷达"),
    "天窗": ("全景天窗", "可开启全景天窗"),
    "冰箱": ("车载冰箱",),
    "加热": ("座椅加热", "方向盘加热"),
    "通风": ("座椅通风",),
    "安全": ("安全气囊", "主动安全"),
    "气囊": ("安全气囊",),
    "四驱": ("驱动形式", "电动四驱"),
    "增程": ("增程式", "EREV"),
    "插混": ("插电混动", "PHEV"),
    "油混": ("油电混动", "HEV"),
    "纯电": ("纯电动", "BEV"),
    "油耗低": ("馈电油耗", "综合油耗"),
    "奶爸车": ("MPV", "大空间"),
    "家用": ("空间", "座位数"),
    "露营": ("对外放电", "移动电站", "后备厢容积"),
    "长途": ("综合续航", "馈电油耗", "辅助驾驶"),
    "保值": ("保有量", "月销量"),
    "销量": ("月销量",),
}


def expand_query(query: str, max_extra: int = 8) -> tuple[str, list[str]]:
    """领域同义扩展：返回 (扩展后的查询, 追加的扩展词列表)。

    匹配两级：jieba 整词命中同义词典键；否则键作为子串出现在原查询中。
    扩展词按词典顺序追加、去重、截断 max_extra——控制查询膨胀与噪声。
    """
    query = (query or "").strip()
    if not query:
        return query, []
    lowered = query.lower()
    jieba_mod = _get_jieba()
    words: set[str] = set()
    if jieba_mod is not None:
        for run in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", query):
            for w in jieba_mod.lcut(run):
                if len(w) >= 2:
                    words.add(w.lower())
    extra: list[str] = []
    seen: set[str] = {lowered}
    for key, expansions in DOMAIN_SYNONYMS.items():
        hit = key.lower() in lowered or key.lower() in words or any(key.lower() in w for w in words)
        if not hit:
            continue
        for term in expansions:
            term = term.strip()
            if not term or term.lower() in seen:
                continue
            extra.append(term)
            seen.add(term.lower())
            if len(extra) >= max_extra:
                return (f"{query} {' '.join(extra)}".strip(), extra)
    if not extra:
        return query, []
    return (f"{query} {' '.join(extra)}".strip(), extra)
