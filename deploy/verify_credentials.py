"""凭据可用性校验（只读；供轮换后验收使用）。

在 api 容器内运行（复用应用自身的客户端与配置，避免另写一套请求逻辑）：

    docker cp deploy/verify_credentials.py deploy-api-1:/tmp/
    docker exec -e PYTHONPATH=/srv/carsel/backend -w /srv/carsel/backend \
        deploy-api-1 python /tmp/verify_credentials.py

说明：
- 只读校验：DeepSeek 一次最小对话、嵌入一次单条向量、重排一次两文档打分、
  Zilliz describe、OSS list_objects(max_keys=1)、SMTP 仅登录不发信；
- 只打印状态与长度/维度，绝不打印密钥明文；
- 任一 FAIL 时退出码为 1，便于运维脚本判失败。
"""
from __future__ import annotations

import asyncio
import os
import smtplib
import ssl
import sys

# 本地直跑时补齐 .env（容器内由 compose 的 env_file 注入，已存在则不动）
for _candidate in ("backend/.env", ".env", "/srv/carsel/backend/.env"):
    if os.path.exists(_candidate):
        try:
            from dotenv import load_dotenv

            load_dotenv(_candidate, override=False)
        except Exception:  # noqa: BLE001 - 无 dotenv 时依赖真实环境变量
            pass
        break

RESULTS: list[tuple[str, bool]] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:10} {detail}", flush=True)


def check_deepseek() -> None:
    try:
        from app.common.llm import LLMClient

        client = LLMClient()
        if not client.available:
            report("deepseek", False, "DEEPSEEK_API_KEY 为空（LLM 已禁用，回答会降级为确定性文案）")
            return
        out = asyncio.run(client.chat([{"role": "user", "content": "ping"}], temperature=0.0))
        choices = out.get("choices") or []
        report("deepseek", bool(choices), f"model={client.model} choices={len(choices)}")
    except Exception as err:  # noqa: BLE001 - 校验脚本需报告任何失败
        report("deepseek", False, f"{type(err).__name__}: {str(err)[:160]}")


def check_embedding() -> None:
    try:
        from app.retrieval.zilliz import make_embedder

        embedder = make_embedder()
        vectors = embedder.embed(["credential rotation check"])
        dim = len(vectors[0]) if vectors and vectors[0] else 0
        report("embedding", dim > 0, f"model={embedder.model} dim={dim}")
    except Exception as err:  # noqa: BLE001
        report("embedding", False, f"{type(err).__name__}: {str(err)[:160]}")


def check_zilliz() -> None:
    try:
        from app.retrieval.zilliz import ZillizRestRetriever

        retriever = ZillizRestRetriever()
        data = retriever._request(
            "POST", "/v2/vectordb/collections/describe", {"collectionName": retriever._collection}
        )
        info = (data or {}).get("data") or {}
        report(
            "zilliz",
            bool(info),
            f"collection={retriever._collection} rowCount={info.get('rowCount', '?')}",
        )
    except Exception as err:  # noqa: BLE001
        report("zilliz", False, f"{type(err).__name__}: {str(err)[:160]}")


def check_rerank() -> None:
    try:
        from app.rag.rerank import CrossEncoderReranker, get_reranker

        impl = type(get_reranker()).__name__
        reranker = CrossEncoderReranker()
        if not reranker._api_key:
            report("rerank", True, f"impl={impl} 未配置 API Key（保持融合序）")
            return
        documents = ["CLTC 续航 520km", "WLTC 油耗 6.2L"]
        last: Exception | None = None
        for path in reranker._candidate_paths():
            # 与 app.rag.rerank 一致：DashScope 原生 text-rerank 需要嵌套 input/parameters
            if path.endswith("/text-rerank/text-rerank"):
                payload = {
                    "model": reranker._model,
                    "input": {"query": "续航", "documents": documents},
                    "parameters": {"top_n": len(documents), "return_documents": False},
                }
            else:
                payload = {
                    "model": reranker._model,
                    "query": "续航",
                    "documents": documents,
                    "top_n": len(documents),
                }
            try:
                items = reranker._extract_items(reranker._post(payload, path))
            except Exception as err:  # noqa: BLE001
                last = err
                continue
            if items:
                report("rerank", True, f"impl={impl} path={path} items={len(items)}")
                return
            last = RuntimeError("响应无有效结果")
        report("rerank", False, f"impl={impl} {type(last).__name__}: {str(last)[:160]}")
    except Exception as err:  # noqa: BLE001
        report("rerank", False, f"{type(err).__name__}: {str(err)[:160]}")


def check_oss() -> None:
    try:
        from app.common.oss import get_oss_bucket

        bucket = get_oss_bucket()
        if bucket is None:
            report("oss", False, "客户端不可用（未配置或连接失败）——上传会静默回退本地存档")
            return
        listed = bucket.list_objects(prefix="", max_keys=1)
        report("oss", True, f"bucket={bucket.bucket_name} list_ok keys={len(listed.object_list)}")
    except Exception as err:  # noqa: BLE001
        report("oss", False, f"{type(err).__name__}: {str(err)[:160]}")


def check_smtp() -> None:
    try:
        from app.common.config import get_settings

        settings = get_settings()
        if not (settings.smtp_host and settings.smtp_user and settings.smtp_password):
            report("smtp", False, "未配置（SMTP_HOST/USER/PASSWORD 为空）")
            return
        server = smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port or 465, timeout=20,
            context=ssl.create_default_context(),
        )
        server.login(settings.smtp_user, settings.smtp_password)
        server.quit()
        report("smtp", True, f"host={settings.smtp_host}:{settings.smtp_port} 登录成功（未发信）")
    except Exception as err:  # noqa: BLE001
        report("smtp", False, f"{type(err).__name__}: {str(err)[:160]}")


def main() -> int:
    for check in (check_deepseek, check_embedding, check_zilliz, check_rerank, check_oss, check_smtp):
        check()
    failed = [name for name, ok in RESULTS if not ok]
    print("---")
    print(f"checked={len(RESULTS)} failed={failed or '无'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
