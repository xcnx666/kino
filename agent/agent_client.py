from llm.llm_client import LLM
from .base import Base
from pydantic import BaseModel
from typing import Dict, List
from logger import logger
from tools.tool import Tool
from config import ModelResponse, ModelConfig
import json


class Agent:
    def __init__(
        self,
        llm: ModelConfig,
        max_llm_step: int,
        max_tool_step,
        system_prompt: str,
        tools,
    ):
        self.llm: LLM = llm
        self.system_prompt = system_prompt
        self.tools: Dict[str, List[any]] = tools
        self.tool = Tool()
        self.messages = []
        self.max_llm_step = max_llm_step
        self.max_tool_step = max_tool_step

    def run(self, question: str):

        if not self.messages:
            self.messages.append(
                {'role': 'system', 'content': self.system_prompt}
            )

        self.messages.append(
            {'role': 'user', 'content': question}
        )

        llm_step = 0
        tool_step = 0
        while True:

            if llm_step > self.max_llm_step:
                logger.warning('已到达最大推理次数')
                raise RuntimeError("LLM step exceeded.")

            response = self.llm.chat(
                messages=self.messages,
                tools=self.tools,
            )

            # chat() 可能返回 None，做防御
            if response is None:
                logger.warning('模型生成失败')
                continue

            llm_step += 1

            # 流式输出已在 chat() 内部完成；这里拿到的是已聚合的 message
            message = response.choices[0].message
            tool_calls = message.tool_calls or []
            content = message.content or ""

            if tool_calls:
                tool_step += len(tool_calls)

                if tool_step >= self.max_tool_step:
                    logger.warning('🔧调用上限')

                # 把整条 assistant 消息入历史（含 tool_calls），下一轮再喂给模型
                self.messages.append(
                    {
                        'role': 'assistant',
                        'content': content,
                        'tool_calls': [
                            {
                                'id': tc.id,
                                'type': 'function',
                                'function': {
                                    'name': tc.function.name,
                                    'arguments': tc.function.arguments,
                                },
                            }
                            for tc in tool_calls
                        ],
                    }
                )

                count = self._execute_tool(tool_calls)
                tool_step += count
                continue

            # 没有工具调用 → 最终回复
            self.messages.append(
                {'role': 'assistant', 'content': content}
            )
            return ModelResponse(content=content, tool_calls=[], raw=response)

    def _execute_tool(self, tool_calls) -> int:

        if not tool_calls:
            return 0

        count = 0
        for tc in tool_calls:

            # 流式下 arguments 是分段拼起来的字符串
            raw_args = tc.function.arguments
            if isinstance(raw_args, str):
                try:
                    tool_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    logger.warning(f'工具参数解析失败: {raw_args}')
                    tool_args = {}
            else:
                tool_args = raw_args or {}

            tool_name = tc.function.name
            tool_response = self.tool.execute(tool_name, tool_args)

            self.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(tool_response, ensure_ascii=False),
                }
            )

            count += 1

        return count
