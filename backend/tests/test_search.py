"""车系搜索（GET /api/v1/vehicles?q=）测试。

覆盖：归一化匹配（大小写/空格/分隔符）、品牌名与别名、品牌前缀短名、
相关度排序（精确 > 前缀/品牌 > 子串）、空关键词、无结果、与筛选条件叠加。
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.catalog.series_index import normalize_name
from app.vehicles.router import keyword_score
from tests.seed import make_brand, make_sales, make_series, make_source


def _fixture(db_session: Session) -> dict[str, int]:
    """比亚迪（海豚/汉EV别名）+ 特斯拉（Model 3/Model Y）+ 腾势（Z9GT）。"""
    source = make_source(db_session)
    byd = make_brand(db_session, name="比亚迪", source=source)
    tesla = make_brand(db_session, name="Tesla", source=source)
    denza = make_brand(db_session, name="腾势", source=source)

    dolphin = make_series(db_session, byd, name="比亚迪海豚", body_type="sedan", source=source)
    han = make_series(db_session, byd, name="比亚迪汉", body_type="sedan", source=source)
    han.aliases = ["汉EV", "汉 DM-i"]
    model3 = make_series(db_session, tesla, name="Model 3", body_type="sedan", source=source)
    modely = make_series(db_session, tesla, name="Model Y", body_type="suv", source=source)
    z9gt = make_series(db_session, denza, name="腾势Z9GT", body_type="sedan", source=source)
    db_session.commit()
    return {
        "dolphin": dolphin.id,
        "han": han.id,
        "model3": model3.id,
        "modely": modely.id,
        "z9gt": z9gt.id,
    }


def _search(client: TestClient, q: str, **params) -> dict:
    resp = client.get("/api/v1/vehicles", params={"q": q, **params})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_normalize_name_equivalence():
    assert normalize_name("腾势Z9 GT") == normalize_name("腾势z9gt") == "腾势z9gt"
    assert normalize_name("Model  Y") == "modely"


def test_keyword_score_ranking(db_session: Session):
    source = make_source(db_session, name="排序来源")
    brand = make_brand(db_session, name="腾势", source=source)
    exact = make_series(db_session, brand, name="腾势Z9GT", source=source)
    prefix = make_series(db_session, brand, name="腾势Z9GT 猎装版", source=source)
    substring = make_series(db_session, brand, name="腾势N7", source=source)
    db_session.commit()

    assert keyword_score(exact, brand, "腾势z9gt") == 0
    assert keyword_score(prefix, brand, "腾势z9gt") == 1
    assert keyword_score(substring, brand, "腾势") == 1, "品牌名精确命中"
    assert keyword_score(substring, brand, "z9gt") is None
    assert keyword_score(substring, brand, "n7") == 0, "去掉品牌前缀的短名精确命中"
    assert keyword_score(exact, brand, "") is None, "空关键词不参与过滤"


def test_search_by_series_name_with_separators(client: TestClient, db_session: Session):
    ids = _fixture(db_session)
    for q in ("腾势Z9GT", "腾势Z9 GT", "z9gt", "Z9GT"):
        body = _search(client, q)
        assert body["total"] == 1, q
        assert body["items"][0]["series_id"] == ids["z9gt"], q


def test_search_by_brand_lists_whole_brand(client: TestClient, db_session: Session):
    ids = _fixture(db_session)
    body = _search(client, "比亚迪")
    found = {i["series_id"] for i in body["items"]}
    assert found == {ids["dolphin"], ids["han"]}
    assert all(i["brand_name"] == "比亚迪" for i in body["items"])


def test_search_ascii_is_case_insensitive(client: TestClient, db_session: Session):
    ids = _fixture(db_session)
    body = _search(client, "model")
    found = {i["series_id"] for i in body["items"]}
    assert found == {ids["model3"], ids["modely"]}
    assert {i["series_id"] for i in _search(client, "MODEL Y")["items"]} == {ids["modely"]}


def test_search_by_alias_and_short_name(client: TestClient, db_session: Session):
    ids = _fixture(db_session)
    assert {i["series_id"] for i in _search(client, "汉EV")["items"]} == {ids["han"]}
    # 车系名自带品牌前缀时，「海豚」应能命中「比亚迪海豚」
    assert {i["series_id"] for i in _search(client, "海豚")["items"]} == {ids["dolphin"]}


def test_search_exact_match_ranks_first(client: TestClient, db_session: Session):
    """「腾势Z9GT」精确命中应排在「腾势Z9GT 猎装版」这类前缀命中之前。"""
    source = make_source(db_session, name="相关度来源")
    brand = make_brand(db_session, name="腾势", source=source)
    longer = make_series(db_session, brand, name="腾势Z9GT 猎装版", source=source)
    exact = make_series(db_session, brand, name="腾势Z9GT", source=source)
    db_session.commit()
    body = _search(client, "腾势Z9GT")
    assert body["total"] == 2
    assert body["items"][0]["series_id"] == exact.id, "精确命中优先于前缀命中"
    assert body["items"][1]["series_id"] == longer.id


def test_search_no_result_and_blank_keyword(client: TestClient, db_session: Session):
    _fixture(db_session)
    empty = _search(client, "不存在的车系名XYZ")
    assert empty["total"] == 0 and empty["items"] == []
    # 纯空白关键词等价于不搜索（不得因空串匹配到全部车系）
    assert _search(client, "   ")["total"] == _search(client, "")["total"] > 0


def test_search_combines_with_filters_and_pagination(client: TestClient, db_session: Session):
    ids = _fixture(db_session)
    body = _search(client, "比亚迪", body_type="sedan", page_size=1)
    assert body["total"] == 2 and len(body["items"]) == 1 and body["page_size"] == 1
    assert body["items"][0]["series_id"] in {ids["dolphin"], ids["han"]}
    assert _search(client, "model", body_type="suv")["total"] == 1
    # 关键词与筛选互斥时返回空，而不是忽略关键词
    assert _search(client, "model", body_type="mpv")["total"] == 0


def test_search_keyword_length_guard(client: TestClient):
    assert client.get("/api/v1/vehicles", params={"q": "x" * 41}).status_code == 422


def _sales_fixture(db_session: Session, month: str = "2026-08") -> dict[str, int]:
    """销量榜种子：海豚 9000（榜首）、Model Y 7000、汉 5000、腾势Z9GT 1000。"""
    source = make_source(db_session, name="销量来源")
    byd = make_brand(db_session, name="比亚迪", source=source)
    tesla = make_brand(db_session, name="Tesla", source=source)
    denza = make_brand(db_session, name="腾势", source=source)
    dolphin = make_series(db_session, byd, name="比亚迪海豚", body_type="sedan", source=source)
    han = make_series(db_session, byd, name="比亚迪汉", body_type="sedan", source=source)
    modely = make_series(db_session, tesla, name="Model Y", body_type="suv", source=source)
    z9gt = make_series(db_session, denza, name="腾势Z9GT", body_type="sedan", source=source)
    for series, count in ((dolphin, 9000), (modely, 7000), (han, 5000), (z9gt, 1000)):
        make_sales(db_session, series, month, count, source=source)
    db_session.commit()
    return {"dolphin": dolphin.id, "han": han.id, "modely": modely.id, "z9gt": z9gt.id}


def test_home_search_filters_ranking_without_renumbering(client: TestClient, db_session: Session):
    """首页（热销榜）关键词搜索：只留命中车系，且保留全站真实名次。"""
    ids = _sales_fixture(db_session)
    body = client.get("/api/v1/home", params={"q": "比亚迪"}).json()
    assert [c["series_name"] for c in body] == ["比亚迪海豚", "比亚迪汉"]
    # 名次沿用全站排名（海豚 1、汉 3），与能源/价格筛选一致，不重新编号
    assert [c["rank"] for c in body] == [1, 3]
    assert all(c["series_id"] in {ids["dolphin"], ids["han"]} for c in body)

    assert [c["series_name"] for c in client.get("/api/v1/home", params={"q": "z9gt"}).json()] == [
        "腾势Z9GT"
    ]
    assert [c["series_name"] for c in client.get("/api/v1/home", params={"q": "  "}).json()] == [
        "比亚迪海豚",
        "Model Y",
        "比亚迪汉",
        "腾势Z9GT",
    ]
    assert client.get("/api/v1/home", params={"q": "不存在的车系XYZ"}).json() == []


def test_home_search_combines_with_filters(client: TestClient, db_session: Session):
    _sales_fixture(db_session)
    only_suv = client.get("/api/v1/home", params={"q": "model", "body_type": "suv"}).json()
    assert [c["series_name"] for c in only_suv] == ["Model Y"]
    # 关键词与筛选互斥时为空，而不是忽略关键词
    assert client.get("/api/v1/home", params={"q": "model", "body_type": "mpv"}).json() == []
    # 关键词命中但被价格筛选排除时同样为空
    assert client.get("/api/v1/home", params={"q": "腾势", "price_max": 300000}).json() == []


def test_home_search_keyword_length_guard(client: TestClient):
    assert client.get("/api/v1/home", params={"q": "x" * 41}).status_code == 422
