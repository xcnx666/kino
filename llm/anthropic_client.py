"""Anthropic 协议 LLM 客户端。

使用 Anthropic Messages API（兼容 Claude 系列模型），支持流式输出和工具调用。
接口与 llm_client.LLM 保持一致，可在配置中切换使用。

消息格式转换：
- OpenAI assistant+tool_calls → Anthropic content blocks (text + tool_use)
- OpenAI tool role → Anthropic user role + tool_result content block
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import httpx
from dotenv import load_dotenv
import os

from .base import LLM_BASE
from logger import logger

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

# Anthropic API 默认配置
_DEFAULT_BASE_URL = "https://api.anthropic.com"
_API_VERSION = "2023-06-01"


class AnthropicLLM(LLM_BASE):
    """Anthropic Messages API 客户端，接口与 LLM 对齐。"""

    def __init__(self, api_key=None, base_url=None, model=None):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self.base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self.model = model or "claude-sonnet-4-20250514"
        super().__init__(self.api_key, self.base_url, self.model)

        if not self.api_key:
            logger.warning("Anthropic API Key 未配置")

    # ==================== 消息转换 ====================

    def _convert_messages(self, messages: List[Dict[str, Any]]) -> tuple[str, List[Dict[str, Any]]]:
        """将 OpenAI 格式消息转为 Anthropic 格式。

        返回 (system_text, chat_messages)。
        - system 消息合并为 system_text
        - assistant+tool_calls → content blocks
        - tool role → user role + tool_result block
        """
        system_text = ""
        chat_messages: List[Dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "")

            if role == "system":
                system_text += msg.get("content", "") + "\n"
                continue

            if role == "user":
                chat_messages.append({"role": "user", "content": msg.get("content", "")})
                continue

            if role == "assistant":
                content_blocks: List[Dict[str, Any]] = []
                text = msg.get("content", "")
                if text:
                    content_blocks.append({"type": "text", "text": text})

                tool_calls = msg.get("tool_calls", [])
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    raw_args = fn.get("arguments", "{}")
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except json.JSONDecodeError:
                        args = {}
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": args,
                    })

                chat_messages.append({
                    "role": "assistant",
                    "content": content_blocks if content_blocks else "",
                })
                continue

            if role == "tool":
                # OpenAI tool 结果 → Anthropic user + tool_result
                chat_messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": msg.get("content", ""),
                    }],
                })
                continue

        return system_text.strip(), chat_messages

    def _convert_tools(self, tools: List[Any]) -> List[Dict[str, Any]]:
        """将工具列表转为 Anthropic tools 格式。"""
        result = []
        for tool in tools:
            if isinstance(tool, dict):
                if "type" in tool and tool["type"] == "function":
                    fn = tool["function"]
                    result.append({
                        "name": fn["name"],
                        "description": fn.get("description", ""),
                        "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                    })
                elif "name" in tool:
                    result.append({
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "input_schema": tool.get("input_schema", tool.get("parameters", {"type": "object", "properties": {}})),
                    })
            elif hasattr(tool, "to_openai_schema"):
                schema = tool.to_openai_schema()
                if callable(schema):
                    schema = schema()
                fn = schema.get("function", schema)
                result.append({
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                })
        return result

    # ==================== 请求 ====================

    def _requests_model_api(self, messages: List[Dict[str, Any]], tools=None):
        """发起 Anthropic 流式请求，返回 (headers, url, body)。"""

        system_text, chat_messages = self._convert_messages(messages)

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }

        body: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": 8192,
            "messages": chat_messages,
            "stream": True,
        }
        if system_text:
            body["system"] = system_text
        if tools:
            body["tools"] = self._convert_tools(tools)

        url = f"{self.base_url}/v1/messages"
        return headers, url, body

    # ==================== chat（与 LLM.chat 接口一致） ====================

    def chat(self, messages: List[Dict[str, Any]], tools=None, on_token=None) -> Any:
        """流式调用 Anthropic API，返回与 LLM.chat 相同结构的聚合结果。"""

        headers, url, body = self._requests_model_api(messages, tools)

        content_parts: List[str] = []
        tool_calls_buf: Dict[int, Dict[str, Any]] = {}
        tool_call_idx = 0

        try:
            with httpx.Client(timeout=120.0) as client:
                with client.stream("POST", url, headers=headers, json=body) as response:
                    if response.status_code != 200:
                        error_text = response.read().decode("utf-8", errors="replace")
                        logger.error(f"Anthropic API 错误 {response.status_code}: {error_text[:500]}")
                        return None

                    current_tool_id = None
                    current_tool_name = ""
                    current_tool_args = ""

                    for line in response.iter_lines():
                        if not line or not line.startswith("data: "):
                            continue

                        try:
                            event = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue

                        event_type = event.get("type", "")

                        if event_type == "content_block_start":
                            block = event.get("content_block", {})
                            if block.get("type") == "tool_use":
                                current_tool_id = block.get("id", "")
                                current_tool_name = block.get("name", "")
                                current_tool_args = ""

                        elif event_type == "content_block_delta":
                            delta = event.get("delta", {})
                            delta_type = delta.get("type", "")

                            if delta_type == "text_delta":
                                text = delta.get("text", "")
                                if text:
                                    content_parts.append(text)
                                    if on_token:
                                        on_token(text)
                                    else:
                                        print(text, end="", flush=True)

                            elif delta_type == "input_json_delta":
                                current_tool_args += delta.get("partial_json", "")

                        elif event_type == "content_block_stop":
                            if current_tool_id:
                                tool_calls_buf[tool_call_idx] = {
                                    "id": current_tool_id,
                                    "type": "function",
                                    "function": {
                                        "name": current_tool_name,
                                        "arguments": current_tool_args,
                                    },
                                }
                                tool_call_idx += 1
                                current_tool_id = None
                                current_tool_name = ""
                                current_tool_args = ""
        except InterruptedError:
            # 用户中断：上下文管理器会自动关闭连接，重新抛出异常
            raise

        tool_calls_list = [tool_calls_buf[i] for i in sorted(tool_calls_buf)]

        # 构建与 LLM.chat 相同的返回结构
        class _Msg:
            def __init__(self, content, tool_calls):
                self.content = content
                self.tool_calls = tool_calls

        class _Choice:
            def __init__(self, message):
                self.message = message

        class _StreamResult:
            def __init__(self, content, tool_calls, choice):
                self.content = content
                self.tool_calls = tool_calls
                self.choices = [choice]

        assembled_message = _Msg(
            "".join(content_parts),
            tool_calls_list if tool_calls_list else None,
        )

        if content_parts and not on_token:
            print(flush=True)

        return _StreamResult(
            content="".join(content_parts),
            tool_calls=tool_calls_list if tool_calls_list else None,
            choice=_Choice(assembled_message),
        )
