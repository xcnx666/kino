"""文件下载工具：将 URL 资源下载到本地文件。"""
from __future__ import annotations

import os
import shutil
from typing import Optional
from urllib.parse import urlparse

import httpx

from core.tools.base import ToolBase, ToolResult, with_retry
from logger import logger


class DownloadTool(ToolBase):
    name = "download_file"
    description = (
        "将指定 URL 的文件下载到本地路径。"
        "支持图片、视频、音频等任意 HTTP(S) 资源。"
        "返回本地文件路径。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "要下载的文件 URL"},
            "output_path": {"type": "string", "description": "本地保存路径（含文件名）"},
        },
        "required": ["url", "output_path"],
    }

    def __init__(self, session_id: str = "") -> None:
        self._session_id = session_id
        self._task_id = ""
        self._base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def set_task_id(self, task_id: str) -> None:
        """设置当前任务 ID，输出文件落到 output/<session>/<task>/<type>/。"""
        self._task_id = task_id or ""

    def _redirect_path(self, output_path: str) -> str:
        """将输出路径重定向到 output/<session_id>/[<task_id>/]<type>/。"""
        if not self._session_id:
            return output_path
        filename = os.path.basename(output_path)
        # 根据扩展名判断子目录
        ext = os.path.splitext(filename)[1].lower()
        if ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
            subdir = "videos"
        elif ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
            subdir = "images"
        elif ext in (".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a"):
            subdir = "audio"
        else:
            subdir = "other"
        parts = [self._base_dir, "output", self._session_id]
        if self._task_id:
            parts.append(self._task_id)
        parts.append(subdir)
        return os.path.join(*parts, filename)

    def _try_local_copy(self, url: str, output_path: str) -> bool:
        """如果 URL 是 localhost 本地服务地址，直接从磁盘复制文件。

        避免通过 HTTP 代理访问 localhost 导致 502 错误。
        返回 True 表示成功复制，False 表示不是本地 URL 或文件不存在。
        """
        parsed = urlparse(url)
        host = parsed.hostname or ""

        # 只处理 localhost / 127.0.0.1 的请求
        if host not in ("localhost", "127.0.0.1", "0.0.0.0"):
            return False

        path = parsed.path
        if not path:
            return False

        # 将 URL 路径映射到本地文件系统
        # /uploads/... -> <base_dir>/uploads/...
        # /output/... -> <base_dir>/output/...
        if path.startswith("/uploads/") or path.startswith("/output/"):
            local_file = os.path.join(self._base_dir, path.lstrip("/"))
            if os.path.exists(local_file) and os.path.isfile(local_file):
                os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
                shutil.copy2(local_file, output_path)
                logger.info(f"本地文件直接复制（绕过代理）: {url} -> {output_path}")
                return True

        return False

    @with_retry(max_retries=3, base_delay=1.0, exceptions=(httpx.HTTPError, httpx.RequestError))
    async def _download(self, url: str, output_path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        # localhost URL 已由 _try_local_copy 直接复制处理，此处仅处理外部 URL，使用系统代理
        async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            with open(output_path, "wb") as f:
                f.write(resp.content)

    async def execute(self, url: str, output_path: str) -> ToolResult:
        try:
            output_path = self._redirect_path(output_path)

            # 优先尝试本地文件复制（处理 localhost URL 绕过代理）
            if self._try_local_copy(url, output_path):
                size = os.path.getsize(output_path)
                if size == 0:
                    return ToolResult(success=False, error=f"下载文件大小为 0: {output_path}")
                return ToolResult(
                    success=True,
                    data={"file_path": output_path, "size": size},
                )

            # 非 localhost URL 或本地文件不存在，走 HTTP 下载
            await self._download(url, output_path)
            size = os.path.getsize(output_path)
            if size == 0:
                return ToolResult(success=False, error=f"下载文件大小为 0: {output_path}")
            return ToolResult(
                success=True,
                data={"file_path": output_path, "size": size},
            )
        except Exception as e:  # noqa: BLE001
            return ToolResult(success=False, error=f"下载失败: {e}")
