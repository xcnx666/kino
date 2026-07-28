"""文生图工具：通用 OpenAI 兼容接口，支持市面上大部分图片生成 API。

兼容的 API 格式：
- POST {base_url}/images/generations
- 请求体：{"model": ..., "prompt": ..., "n": ..., "size": ..., "response_format": "url"|"b64_json"}
- 响应体：{"data": [{"url": "..."} | {"b64_json": "..."}]}

兼容的 provider / 服务：
- OpenAI DALL-E 系列
- Stability AI（兼容接口）
- Agnes agnes-image 系列
- 智谱 CogView、通义万相、百度文心一格等（OpenAI 兼容模式）
- 任意遵循上述格式的第三方服务

稳定性设计：
- 网络异常自动重试（3 次，指数退避）
- b64_json 图片保存后返回可访问的 URL 路径（/output/images/xxx.png）
- 错误信息包含 HTTP 状态码和响应体，便于诊断
"""
from __future__ import annotations

import asyncio
import base64
import os
import uuid
from typing import Any, Dict, Optional

import httpx

from core.tools.base import ToolBase, ToolResult, with_retry
from logger import logger


class ImageGenTool(ToolBase):
    name = "generate_image"
    description = (
        "根据文本提示词生成图片，返回图片 URL。"
        "提示词必须是英文，包含主体、动作、环境、光照、镜头、风格等要素。"
        "返回的图片 URL 可直接传给 generate_video 的 image 参数。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "图片描述提示词（英文），越具体越好。应包含：主体+动作+环境+光照+镜头+风格",
            },
            "size": {
                "type": "string",
                "description": "图片尺寸，如 1024x1024、1280x720、1792x1024",
            },
            "n": {"type": "integer", "description": "生成数量", "default": 1},
        },
        "required": ["prompt"],
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None, session_id: str = "") -> None:
        self.config = config or {}
        self.provider = self.config.get("provider", "openai_compatible")
        self.api_key = self.config.get("api_key", "")
        self.base_url = self.config.get("base_url", "").rstrip("/")
        self.model = self.config.get("model", "")
        self.default_size = self.config.get("default_size", "1024x1024")
        # 项目根目录
        self._base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # 会话级输出目录：output/<session_id>/[<task_id>/]images/
        self._session_id = session_id
        self._task_id = ""

    def set_task_id(self, task_id: str) -> None:
        """设置当前任务 ID，b64 图片落到 output/<session>/<task>/images/。"""
        self._task_id = task_id or ""

    def _resolve_output_dir(self) -> str:
        parts = [self._base_dir, "output"]
        if self._session_id:
            parts.append(self._session_id)
        if self._task_id:
            parts.append(self._task_id)
        parts.append("images")
        return os.path.join(*parts)

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    @with_retry(max_retries=3, base_delay=2.0, backoff=2.0, exceptions=(httpx.HTTPError, httpx.RequestError))
    async def _call_api(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """调用图片生成 API（带重试）。"""
        url = f"{self.base_url}/images/generations"
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, json=payload, headers=self._headers())

            # 429 速率限制
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "10"))
                logger.warning(f"图片 API 返回 429，等待 {retry_after}s")
                await asyncio.sleep(retry_after)
                raise httpx.HTTPStatusError(
                    f"429 Too Many Requests", request=resp.request, response=resp
                )

            # 400 且包含 response_format 不支持：去掉参数重试一次
            if resp.status_code == 400 and "response_format" in resp.text:
                logger.warning("API 不支持 response_format 参数，去掉后重试")
                payload.pop("response_format", None)
                resp = await client.post(url, json=payload, headers=self._headers())

            # 非 2xx 响应：记录详细错误信息
            if resp.status_code >= 400:
                body = resp.text[:500]
                logger.error(f"图片 API 错误 {resp.status_code}: {body}")
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}: {body}",
                    request=resp.request,
                    response=resp,
                )

            return resp.json()

    async def execute(self, prompt: str, size: str = "", n: int = 1) -> ToolResult:
        size = size or self.default_size
        if not self.api_key or not self.base_url:
            return ToolResult(
                success=False,
                error="图片生成缺少 api_key 或 base_url 配置，请在模型配置中添加",
            )

        payload: Dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "n": n,
            "size": size,
            # 优先请求 URL 格式，避免 b64_json 需要本地保存
            "response_format": "url",
        }

        try:
            data = await self._call_api(payload)
        except httpx.HTTPStatusError as e:
            return ToolResult(success=False, error=f"图片生成 API 错误: {e}")
        except (httpx.HTTPError, httpx.RequestError) as e:
            return ToolResult(success=False, error=f"图片生成网络错误（已重试3次）: {e}")
        except Exception as e:
            return ToolResult(success=False, error=f"图片生成失败: {e}")

        images = []
        for item in data.get("data", []):
            # 优先 URL 格式
            if item.get("url"):
                images.append(item["url"])
            # b64_json 格式：保存到本地文件，返回可访问的 URL 路径
            elif item.get("b64_json"):
                url_path = self._save_b64(item["b64_json"])
                if url_path:
                    images.append(url_path)

        if not images:
            return ToolResult(
                success=False,
                error="接口未返回图片 URL 或 b64_json",
                raw=data,
            )

        logger.info(f"图片生成成功: {len(images)} 张, size={size}")
        return ToolResult(
            success=True,
            data={"images": images, "count": len(images), "size": size},
            raw=data,
        )

    def _save_b64(self, b64_data: str) -> Optional[str]:
        """将 base64 编码的图片保存到本地文件，返回可访问的 URL 路径。

        返回 /output/images/xxx.png 格式的路径，可通过 Web 服务器访问。
        """
        try:
            output_dir = self._resolve_output_dir()
            os.makedirs(output_dir, exist_ok=True)
            file_name = f"img_{uuid.uuid4().hex[:8]}.png"
            file_path = os.path.join(output_dir, file_name)
            with open(file_path, "wb") as f:
                f.write(base64.b64decode(b64_data))

            # 返回可访问的 URL 路径（相对于项目根目录）
            rel_path = os.path.relpath(file_path, self._base_dir)
            url_path = "/" + rel_path.replace("\\", "/")
            logger.info(f"b64 图片已保存: {file_path} → URL: {url_path}")
            return url_path
        except Exception as e:
            logger.error(f"保存 b64 图片失败: {e}")
            return None
