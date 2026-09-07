"""端到端冒烟脚本（阶段 8 评测）：只依赖标准库，可本地/CI 运行。

前置：后端（默认 http://127.0.0.1:8000）与前端（默认 http://127.0.0.1:3000）已启动，
且后端 dev.db 已通过 tools/seed_dev.py 写入样例数据（或接入真实数据）。
可选环境变量：
- 后端以 DEV_ECHO_CODES=true 启动 → 执行完整认证流程（否则跳过验证码环节）；
- E2E_ADMIN_TOKEN=<后端 ADMIN_API_TOKEN> → 执行管理后台检查（否则跳过）。

用法：
    python tools/e2e_smoke.py [--backend URL] [--frontend URL]
退出码：0 = 全部通过（跳过项不计失败）；1 = 存在失败项。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


class Skipped(Exception):
    """环境未满足的可选检查（不计失败）。"""


def request(
    url: str,
    method: str = "GET",
    payload: dict | None = None,
    timeout: int = 30,
    headers: dict | None = None,
):
    data = None
    req_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
        return resp.status, resp.headers, body


def check(name: str, fn) -> bool:
    try:
        fn()
        print(f"[{PASS}] {name}")
        return True
    except Skipped as skip:
        print(f"[{SKIP}] {name}：{skip}")
        return True
    except Exception as err:  # noqa: BLE001
        print(f"[{FAIL}] {name}: {err}")
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", default="http://127.0.0.1:8000")
    parser.add_argument("--frontend", default="http://127.0.0.1:3000")
    args = parser.parse_args(argv)
    backend, frontend = args.backend.rstrip("/"), args.frontend.rstrip("/")

    results: list[bool] = []

    def backend_health():
        _, _, body = request(f"{backend}/api/v1/health")
        assert json.loads(body)["status"] == "ok"

    def home_api():
        _, _, body = request(f"{backend}/api/v1/home")
        cards = json.loads(body)
        assert isinstance(cards, list)

    def vehicle_browse_api():
        _, _, body = request(f"{backend}/api/v1/vehicles?page_size=5")
        data = json.loads(body)
        assert data["total"] > 100, "全部车型应有数百个车系"
        assert len(data["items"]) == 5

    def vehicle_detail():
        _, _, body = request(f"{backend}/api/v1/vehicles/1")
        assert json.loads(body)["id"] == 1

    compare_ids: list[int] = []

    def compare_api():
        # 无 SKU 时跳过；有 SKU 则取真实款型 ID 验证创建+读取
        _, _, body = request(f"{backend}/api/v1/vehicles/1/variants")
        variants = json.loads(body)
        if not variants:
            raise Skipped("当前数据无 SKU（车型配置数据待导入），跳过对比 API")
        variant_ids = [v["id"] for v in variants[:2]]
        _, _, body = request(
            f"{backend}/api/v1/comparisons",
            method="POST",
            payload={"variant_ids": variant_ids},
        )
        comparison_id = json.loads(body)["id"]
        _, _, detail = request(f"{backend}/api/v1/comparisons/{comparison_id}")
        assert json.loads(detail)["id"] == comparison_id
        compare_ids.extend(variant_ids)

    def agent_flow():
        _, _, body = request(f"{backend}/api/v1/agent/sessions", method="POST", payload={})
        sid = json.loads(body)["session_id"]
        _, _, body = request(
            f"{backend}/api/v1/agent/sessions/{sid}/messages",
            method="POST",
            payload={"message": "我想买台车"},
        )
        out = json.loads(body)
        assert out["need_clarification"] is True
        _, _, body = request(
            f"{backend}/api/v1/agent/sessions/{sid}/messages",
            method="POST",
            payload={"message": "预算26万，家庭用车，5口人，想要新能源SUV"},
        )
        out = json.loads(body)
        assert out["need_clarification"] is False
        if not out["recommended_variants"]:
            # 无 SKU 数据时如实返回空并建议放宽，属预期行为
            assert "建议放宽" in (out["explanation"] or "")
            return
        assert all(v["price_cny"] <= 260000 for v in out["recommended_variants"])

    def agent_sse():
        _, _, body = request(f"{backend}/api/v1/agent/sessions", method="POST", payload={})
        sid = json.loads(body)["session_id"]
        request(
            f"{backend}/api/v1/agent/sessions/{sid}/messages",
            method="POST",
            payload={"message": "预算26万，家庭用车，5口人，想要新能源SUV"},
        )
        status, headers, body = request(f"{backend}/api/v1/agent/sessions/{sid}/stream")
        assert status == 200
        assert headers.get("Content-Type", "").startswith("text/event-stream")
        assert "event: message" in body

    def frontend_home():
        status, _, body = request(f"{frontend}/")
        assert status == 200
        assert "<html" in body and len(body) > 1000

    def frontend_detail():
        status, _, body = request(f"{frontend}/vehicles/1")
        assert status == 200
        assert "<html" in body

    def frontend_browse():
        status, _, body = request(f"{frontend}/vehicles")
        assert status == 200
        assert "<html" in body and len(body) > 1000

    def frontend_compare():
        ids = ",".join(map(str, compare_ids)) if compare_ids else "1,2"
        status, _, body = request(f"{frontend}/compare?variant_ids={ids}")
        assert status == 200
        assert "<html" in body

    def frontend_favorites():
        status, _, body = request(f"{frontend}/favorites")
        assert status == 200
        assert "<html" in body

    def frontend_privacy():
        status, _, body = request(f"{frontend}/privacy")
        assert status == 200
        assert "<html" in body

    def auth_favorites_flow():
        # 需后端以 DEV_ECHO_CODES=true 启动；否则跳过验证码环节
        import uuid

        email = f"e2e-{uuid.uuid4().hex[:8]}@example.com"
        _, _, body = request(f"{backend}/api/v1/auth/register", method="POST", payload={"email": email})
        registered = json.loads(body)
        code = registered.get("dev_code")
        if not code:
            raise Skipped("后端未开启 DEV_ECHO_CODES，无法获取验证码")
        _, _, body = request(
            f"{backend}/api/v1/auth/verify-code", method="POST", payload={"email": email, "code": code}
        )
        token = json.loads(body)["token"]
        headers = {"Authorization": f"Bearer {token}"}
        status, _, body = request(
            f"{backend}/api/v1/me/favorites/1?kind=series", method="PUT", payload={}, headers=headers
        )
        assert status in (200, 201)
        status, _, body = request(f"{backend}/api/v1/me/favorites", headers=headers)
        assert status == 200
        assert json.loads(body), "收藏列表不应为空"
        status, _, _ = request(f"{backend}/api/v1/me", method="DELETE", headers=headers)
        assert status == 200

    def admin_stats():
        admin_token = os.environ.get("E2E_ADMIN_TOKEN")
        if not admin_token:
            raise Skipped("未设置 E2E_ADMIN_TOKEN")
        status, _, body = request(
            f"{backend}/api/v1/admin/stats", headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert status == 200
        stats = json.loads(body)
        assert stats["brands"] >= 1
        try:
            request(f"{backend}/api/v1/admin/stats")
        except urllib.error.HTTPError as err:
            assert err.code == 401, f"无凭据请求应返回 401，实际 {err.code}"
        else:
            raise AssertionError("无凭据请求应返回 401")

    def frontend_api_rewrite():
        _, _, body = request(f"{frontend}/api/v1/health")
        assert json.loads(body)["status"] == "ok"

    checks = [
        ("后端健康检查", backend_health),
        ("首页 API", home_api),
        ("全部车型 API", vehicle_browse_api),
        ("车型详情 API", vehicle_detail),
        ("对比 API（创建+读取）", compare_api),
        ("Agent 流程（追问→推荐）", agent_flow),
        ("Agent SSE", agent_sse),
        ("认证流程（注册→收藏→注销）", auth_favorites_flow),
        ("管理后台（stats + 401）", admin_stats),
        ("前端首页", frontend_home),
        ("前端全部车型页", frontend_browse),
        ("前端详情页", frontend_detail),
        ("前端对比页", frontend_compare),
        ("前端收藏页", frontend_favorites),
        ("前端隐私政策页", frontend_privacy),
        ("前端 /api/v1 同源转发", frontend_api_rewrite),
    ]
    for name, fn in checks:
        results.append(check(name, fn))

    passed = sum(results)
    print(f"\n总计：{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
