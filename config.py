from pydantic import BaseModel
from dataclasses import dataclass
from typing import Any

@dataclass
class ModelConfig:
    provider: str
    model: str
    api_key: str
    base_url: str


class ModelResponse(BaseModel):
    content: str = ""
    tool_calls: list = []
    tool_results: list = []
    raw: Any = None