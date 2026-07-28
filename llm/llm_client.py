from __future__ import annotations
from typing import Dict, List, Any, Optional
import json
import httpx
from openai import OpenAI
from .base import LLM_BASE
from dotenv import load_dotenv
import os
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
from logger import logger


def _repair_json_string(s: str) -> str:
    """尝试修复无效的 JSON 字符串，返回合法 JSON 字符串。

    常见问题：
    - 未转义的双引号
    - 未转义的换行符
    - 不完整的 JSON（缺少结尾的 }）
    """
    if not s or not s.strip():
        return "{}"

    # 尝试直接解析
    try:
        json.loads(s)
        return s
    except json.JSONDecodeError:
        pass

    # 尝试：补全缺失的结尾大括号
    candidate = s.rstrip()
    if not candidate.endswith("}"):
        candidate += "}"
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    # 尝试：提取最外层的 { ... } 部分
    first_brace = s.find("{")
    last_brace = s.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        candidate = s[first_brace:last_brace + 1]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            # 尝试修复常见的未转义引号问题
            pass

    # 最终降级：返回空对象，避免 API 400 错误
    logger.warning(f"工具调用参数 JSON 修复失败，使用空对象替代。原始内容前100字符: {s[:100]}")
    return "{}"

class LLM(LLM_BASE):
    def __init__(self, api_key=None, base_url=None, model=None):
        self.api_key = api_key or os.getenv('api_key')
        self.base_url = base_url or os.getenv('base_url')
        self.model = model or os.getenv('model')
        super().__init__(self.api_key, self.base_url, self.model)

        self.client = OpenAI(
            api_key = self.api_key,
            base_url = self.base_url,
            http_client=httpx.Client(timeout=300.0),
        )

    def _requests_model_api(self, messages:list[dict[str, Any]], tools:list[Any] | None = None):

        params = {
            'model':self.model,
            'messages':messages,
            'stream' : True
        }

        if tools:
            params['tools'] = self._Tool_convert(tools)

        response = self.client.chat.completions.create(**params)

        return response
    
    def _Tool_convert(self,tools:List[Any]):

        tools_result = []
        for tool in tools:
            if isinstance(tool,dict):
                if "type" in tool and tool["type"] == "function":
                    tools_result.append(tool)
                else:
                    tools_result.append({
                        'type':'function',
                        'function':{
                            'name':tool['name'],
                            'description':tool['description'],
                            'parameters':tool["input_schema"]
                        }
                    })
            
            elif hasattr(tool,'to_openai_schema'):
                tools_result.append(tool.to_openai_schema)

            else:
                logger.warning(f'没有找到工具{tool}')
                raise

        return tools_result
    
    def _Messages_convert(self, messages):

        messages_result = []

        for msg in messages:

            if msg['role'] == 'system':
                messages_result.append(
                    {'role':'system','content':msg['content']}
                )
                continue

            if msg['role'] == 'user':
                messages_result.append(
                    {'role':'user','content':msg['content']}
                )

            elif msg['role'] == 'assistant':
                
                if msg.content:
                    messages_result.append(
                        {'role':'assistant','content':msg['content']}
                    )
                
        return messages_result
        
    def to_openai_schema(self) -> dict[str,Any]:

        return {
            'type':'function',
            'function':{
                'name':self.name,
                'description':self.description,
                'parameters':self.parameters
            }
        }
    
    def to_anthropic_schema(self) -> dict[str,Any]:
        return {
            'name':self.name,
            'description':self.description,
            'parameters':self.parameters
        }
    
    def chat(self, messages: list[dict[str, Any]], tools=None, on_token=None) -> Any:

        stream = self._requests_model_api(
            messages=messages,
            tools=tools,
        )

        content_parts: list[str] = []
        # 流式下 tool_calls 按 index 增量到达，要按 index 聚合
        tool_calls_buf: dict[int, dict[str, Any]] = {}

        try:
            for chunk in stream:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                # 1) 文本增量：通过回调推送或实时打印
                if delta.content:
                    content_parts.append(delta.content)
                    if on_token:
                        on_token(delta.content)
                    else:
                        print(delta.content, end="", flush=True)

                # 2) tool_calls 增量：按 index 合并
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_buf:
                            tool_calls_buf[idx] = {
                                "id": tc.id or "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        if tc.id:
                            tool_calls_buf[idx]["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                tool_calls_buf[idx]["function"]["name"] += tc.function.name
                            if tc.function.arguments:
                                tool_calls_buf[idx]["function"]["arguments"] += tc.function.arguments
        except InterruptedError:
            # 用户中断：关闭流并重新抛出
            try:
                stream.close()
            except Exception:
                pass
            raise

        tool_calls_list = [tool_calls_buf[i] for i in sorted(tool_calls_buf)]

        # 流式组装完成后，校验并修复每个 tool_call 的 arguments
        for tc in tool_calls_list:
            raw_args = tc.get("function", {}).get("arguments", "")
            if raw_args:
                repaired = _repair_json_string(raw_args)
                if repaired != raw_args:
                    logger.warning(
                        f"工具调用参数 JSON 已修复: name={tc['function']['name']}, "
                        f"原始长度={len(raw_args)}, 修复后={len(repaired)}"
                    )
                    tc["function"]["arguments"] = repaired
            else:
                tc["function"]["arguments"] = "{}"

        # 流式下没有真正的 message 对象，自己拼一个轻量 message，保留 .tool_calls / .content
        class _Msg:
            def __init__(self, content, tool_calls):
                self.content = content
                self.tool_calls = tool_calls

        class _Choice:
            def __init__(self, message):
                self.message = message

        assembled_message = _Msg(
            "".join(content_parts),
            tool_calls_list if tool_calls_list else None,
        )
        assembled_choice = _Choice(assembled_message)

        class _StreamResult:
            def __init__(self, content, tool_calls, choice):
                self.content = content
                self.tool_calls = tool_calls
                self.choices = [choice]

        # 流式结束后换行（仅命令行模式）
        if content_parts and not on_token:
            print(flush=True)

        return _StreamResult(
            content="".join(content_parts),
            tool_calls=tool_calls_list if tool_calls_list else None,
            choice=assembled_choice,
        )