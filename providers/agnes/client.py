"""Agnes AI 异步客户端。

覆盖两类已上线能力：
- 文生图：POST /v1/images/generations（OpenAI 兼容，同步返回）
- 文生视频：POST /v1/videos 创建任务 → GET /v1/videos/{task_id} 轮询（异步）

鉴权：Authorization: Bearer <AGNES_API_KEY>
Base URL 默认 https://apihub.agnes-ai.com/v1，可用 AGNES_BASE_URL 覆盖。

参考:
- 视频 API 指南: https://lmwmm.com/post/840.html
- 接入示例: https://www.aijourney.vip/2605.html
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, Optional

import httpx

from core.tools.base import with_retry


class AgnesClient:
    """Agnes AI 的轻量异步 HTTP 客户端。"""

    DEFAULT_BASE_URL = "https://apihub.agnes-ai.com/v1"
    IMAGE_MODEL = "agnes-image-2.1-flash"
    VIDEO_MODEL = "agnes-video-v2.0"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        video_create_path: str = "/videos",
        video_poll_path: str = "/videos",
    ) -> None:
        self.api_key = api_key or os.getenv("AGNES_API_KEY")
        self.base_url = (base_url or os.getenv("AGNES_BASE_URL", self.DEFAULT_BASE_URL)).rstrip("/")
        # 视频接口路径可配置：官方文档以 /videos 为准，部分第三方文章写作 /video/generations，
        # 若 /videos 不可用可改传 video_create_path="/video/generations"。
        self.video_create_path = video_create_path
        self.video_poll_path = video_poll_path
        if not self.api_key:
            raise ValueError("AGNES_API_KEY 未配置，请在 .env 中设置")

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    @with_retry(max_retries=3, exceptions=(httpx.HTTPError, httpx.RequestError))
    async def generate_image(
        self,
        prompt: str,
        model: str = IMAGE_MODEL,
        n: int = 1,
        size: str = "1024x1024",
    ) -> Dict[str, Any]:
        """文生图，返回 OpenAI 兼容响应（data[*].url 为图片地址）。"""
        url = f"{self.base_url}/images/generations"
        payload = {"model": model, "prompt": prompt, "n": n, "size": size}
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, json=payload, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    @with_retry(max_retries=3, exceptions=(httpx.HTTPError, httpx.RequestError))
    async def create_video_task(
        self,
        prompt: str,
        model: str = VIDEO_MODEL,
        width: int = 1152,
        height: int = 768,
        num_frames: int = 121,
        frame_rate: int = 24,
    ) -> Dict[str, Any]:
        """创建视频生成任务，返回 {"task_id": "..."}。"""
        url = f"{self.base_url}{self.video_create_path}"
        payload = {
            "model": model,
            "prompt": prompt,
            "width": width,
            "height": height,
            "num_frames": num_frames,
            "frame_rate": frame_rate,
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=payload, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    @with_retry(max_retries=5, exceptions=(httpx.HTTPError, httpx.RequestError))
    async def get_video_task(self, task_id: str) -> Dict[str, Any]:
        """轮询视频任务状态。

        返回字段：
        - status: queued / in_progress / completed / failed
        - progress: 0-100
        - remixed_from_video_id: 完成时的视频下载 URL（注意字段名）
        - error: 失败时的错误信息
        """
        url = f"{self.base_url}{self.video_poll_path}/{task_id}"
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    async def generate_video(
        self,
        prompt: str,
        *,
        model: str = VIDEO_MODEL,
        width: int = 1152,
        height: int = 768,
        num_frames: int = 121,
        frame_rate: int = 24,
        poll_interval: float = 5.0,
        timeout: float = 300.0,
        on_progress=None,
    ) -> Dict[str, Any]:
        """创建视频任务并轮询直到完成/失败/超时。

        on_progress(status_dict) 回调可用于上报进度。
        """
        create_resp = await self.create_video_task(
            prompt, model=model, width=width, height=height,
            num_frames=num_frames, frame_rate=frame_rate,
        )
        task_id = create_resp.get("task_id")
        if not task_id:
            raise RuntimeError(f"创建视频任务失败，未返回 task_id: {create_resp}")

        elapsed = 0.0
        while elapsed < timeout:
            status = await self.get_video_task(task_id)
            if on_progress:
                on_progress(status)
            state = status.get("status")
            if state == "completed":
                video_url = status.get("remixed_from_video_id")
                return {"task_id": task_id, "video_url": video_url, "status": status}
            if state == "failed":
                raise RuntimeError(f"视频生成失败: {status.get('error', '未知错误')}")
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        raise TimeoutError(f"视频生成超时 (task_id={task_id}, timeout={timeout}s)")
