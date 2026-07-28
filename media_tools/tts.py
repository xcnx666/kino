"""TTS 工具：支持多 provider（edge_tts / openai_compatible / agnes）。

根据配置管理器传入的 config 动态选择实现：
- edge_tts: 免费，无需 Key，使用 Microsoft edge-tts
- openai_compatible: 调用 OpenAI 兼容的 /audio/speech 接口
- agnes: 预留（Agnes TTS 上线后在此实现）
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

import httpx

from core.tools.base import ToolBase, ToolResult

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"


class TTSTool(ToolBase):
    name = "text_to_speech"
    description = (
        "将文本合成为语音 MP3。"
        "支持 edge_tts（免费）/ openai_compatible / agnes 多种引擎。"
        "返回音频文件路径。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要合成的文本"},
            "output_path": {"type": "string", "description": "输出音频文件路径（.mp3）"},
            "voice": {
                "type": "string",
                "description": "音色（edge_tts: zh-CN-XiaoxiaoNeural；openai: alloy/echo/fable/onyx/nova/shimmer）",
            },
        },
        "required": ["text", "output_path"],
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None, session_id: str = "") -> None:
        self.config = config or {}
        self.provider = self.config.get("provider", "edge_tts")
        self.default_voice = self.config.get("default_voice", DEFAULT_VOICE)
        self._session_id = session_id
        self._task_id = ""
        self._base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def set_task_id(self, task_id: str) -> None:
        """设置当前任务 ID，输出文件落到 output/<session>/<task>/audio/。"""
        self._task_id = task_id or ""

    def _redirect_path(self, output_path: str) -> str:
        """将输出路径重定向到 output/<session_id>/[<task_id>/]audio/。"""
        if not self._session_id:
            return output_path
        filename = os.path.basename(output_path)
        parts = [self._base_dir, "output", self._session_id]
        if self._task_id:
            parts.append(self._task_id)
        parts.append("audio")
        return os.path.join(*parts, filename)

    async def execute(
        self,
        text: str,
        output_path: str,
        voice: str = "",
    ) -> ToolResult:
        voice = voice or self.default_voice
        output_path = self._redirect_path(output_path)
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        if self.provider == "edge_tts":
            return await self._exec_edge_tts(text, output_path, voice)
        elif self.provider == "openai_compatible":
            return await self._exec_openai(text, output_path, voice)
        elif self.provider == "agnes":
            return await self._exec_agnes(text, output_path, voice)
        else:
            return ToolResult(success=False, error=f"不支持的 TTS provider: {self.provider}")

    async def _exec_edge_tts(self, text: str, output_path: str, voice: str) -> ToolResult:
        try:
            import edge_tts  # type: ignore
        except ImportError:
            return ToolResult(
                success=False,
                error="未安装 edge-tts，请执行: pip install edge-tts",
            )
        try:
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(output_path)
            return ToolResult(
                success=True,
                data={"audio_path": output_path, "voice": voice, "provider": "edge_tts"},
            )
        except Exception as e:  # noqa: BLE001
            return ToolResult(success=False, error=f"edge-tts 合成失败: {e}")

    async def _exec_openai(self, text: str, output_path: str, voice: str) -> ToolResult:
        """调用 OpenAI 兼容的 /audio/speech 接口。"""
        api_key = self.config.get("api_key", "")
        base_url = self.config.get("base_url", "").rstrip("/")
        model = self.config.get("model", "tts-1")

        if not api_key or not base_url:
            return ToolResult(success=False, error="OpenAI TTS 缺少 api_key 或 base_url 配置")

        try:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": model,
                "input": text,
                "voice": voice or "alloy",
                "response_format": "mp3",
            }
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{base_url}/audio/speech",
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                with open(output_path, "wb") as f:
                    f.write(resp.content)

            return ToolResult(
                success=True,
                data={"audio_path": output_path, "voice": voice, "provider": "openai_compatible"},
            )
        except Exception as e:  # noqa: BLE001
            return ToolResult(success=False, error=f"OpenAI TTS 合成失败: {e}")

    async def _exec_agnes(self, text: str, output_path: str, voice: str) -> ToolResult:
        """Agnes TTS（预留接口，上线后在此实现）。"""
        return ToolResult(
            success=False,
            error="Agnes TTS 尚未上线，请使用 edge_tts 或 openai_compatible",
        )
