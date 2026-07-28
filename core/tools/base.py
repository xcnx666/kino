"""异步 Tool 基座：统一的工具抽象、标准化返回、重试装饰器、注册中心。

设计要点：
- 所有 AI 能力（图片/视频/TTS/FFmpeg/分析）都继承 ToolBase，Agent 只调用 execute。
- 返回统一的 ToolResult，Agent 据此判断成功/失败并决定是否重试或修正。
- with_retry 装饰器只作用于真正的不确定失败（网络/HTTP），业务异常由工具内部捕获转 ToolResult。
"""
from __future__ import annotations

import asyncio
import functools
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple, Type


@dataclass
class ToolResult:
    """工具执行的标准化返回。"""

    success: bool
    data: Any = None
    error: Optional[str] = None
    raw: Any = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"success": self.success}
        if self.data is not None:
            d["data"] = self.data
        if self.error:
            d["error"] = self.error
        return d

    def __bool__(self) -> bool:
        return self.success


class ToolBase(ABC):
    """所有工具的抽象基类。子类需声明 name/description/parameters 并实现 execute。"""

    name: str = ""
    description: str = ""
    parameters: Dict[str, Any] = {}

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        """执行工具，返回 ToolResult。"""

    def to_openai_schema(self) -> Dict[str, Any]:
        """转换为 OpenAI function-calling 工具描述。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def with_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: Tuple[Type[BaseException], ...] = (Exception,),
):
    """异步重试装饰器，指数退避。只捕获 exceptions 中列出的异常。"""

    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            delay = base_delay
            last_exc: Optional[BaseException] = None
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:  # noqa: PERF203
                    last_exc = e
                    if attempt < max_retries - 1:
                        await asyncio.sleep(delay)
                        delay *= backoff
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator


class ToolRegistry:
    """工具注册中心：按名注册、按名调用、批量导出 OpenAI schema。"""

    def __init__(self) -> None:
        self._tools: Dict[str, ToolBase] = {}

    def register(self, tool: ToolBase) -> "ToolRegistry":
        if not tool.name:
            raise ValueError("工具必须声明 name")
        if tool.name in self._tools:
            raise ValueError(f"工具 {tool.name} 已存在，请勿重复注册")
        self._tools[tool.name] = tool
        return self

    def get(self, name: str) -> Optional[ToolBase]:
        return self._tools.get(name)

    def all(self) -> list:
        """返回所有已注册的工具实例。"""
        return list(self._tools.values())

    def names(self) -> list:
        return list(self._tools.keys())

    def schemas(self) -> list:
        return [t.to_openai_schema() for t in self._tools.values()]

    async def call(self, name: str, **kwargs) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult(success=False, error=f"未找到工具: {name}")
        return await tool.execute(**kwargs)
