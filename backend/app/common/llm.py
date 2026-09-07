"""LLMClient 适配层。

业务代码访问 LLM 的唯一入口：统一封装聊天、流式输出、工具调用、超时/重试；
模型名称与密钥全部配置化（Settings），不得散落原始 API 调用。

密钥为空时 `available=False`：Agent 进入确定性模式（只基于数据库与工具结果回答，
不使用模型生成事实），保证 Agent 不因 LLM 不可用而整体失效。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.common.config import get_settings


class LLMError(RuntimeError):
    """LLM 调用失败（网络/限流/服务端错误）。"""


class LLMClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self._api_key = api_key if api_key is not None else settings.deepseek_api_key
        self._base_url = (base_url or settings.deepseek_base_url).rstrip("/")
        self._model = model or settings.deepseek_model
        self._timeout = httpx.Timeout(30.0, connect=10.0)
        self._transport = httpx.AsyncHTTPTransport(retries=2)

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    @property
    def model(self) -> str:
        return self._model

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """统一请求：网络/超时/畸形响应一律包装为 LLMError，保证调用方确定性回退。"""
        if not self.available:
            raise LLMError("DeepSeek API 未配置（DEEPSEEK_API_KEY 为空）")
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.post(path, headers=self._headers(), json=payload)
        except httpx.HTTPError as err:
            raise LLMError(f"DeepSeek API 网络失败：{type(err).__name__}") from err
        if resp.status_code != 200:
            raise LLMError(f"DeepSeek API {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except (ValueError, KeyError) as err:
            raise LLMError(f"DeepSeek API 响应解析失败：{type(err).__name__}") from err

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        json_mode: bool = False,
    ) -> dict[str, Any]:
        """非流式对话（可选工具调用；由调用方循环执行工具）。"""
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return await self._post("/chat/completions", payload)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> AsyncIterator[str]:
        """流式对话：逐块 yield 文本增量。"""
        if not self.available:
            raise LLMError("DeepSeek API 未配置（DEEPSEEK_API_KEY 为空）")
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout, transport=self._transport
            ) as client:
                async with client.stream("POST", "/chat/completions", headers=self._headers(), json=payload) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        raise LLMError(f"DeepSeek API {resp.status_code}: {body[:200]}")
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                        if delta.get("content"):
                            yield delta["content"]
        except httpx.HTTPError as err:  # 评审 P2：流式网络异常统一包装 LLMError
            raise LLMError(f"DeepSeek API 流式网络失败：{type(err).__name__}") from err


def get_llm_client() -> LLMClient:
    return LLMClient()
