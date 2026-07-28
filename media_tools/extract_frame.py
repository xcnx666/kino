"""提取视频尾帧工具：用 ffmpeg 提取视频最后一帧，返回本地图片路径。

用途：在多段视频连续生成场景中，提取上一段视频的尾帧作为下一段视频的首帧，
保证画面一致性。Agent 可在工作流中自动调用此工具实现连贯视频生成。

工作流示例：
1. generate_image → 生成首帧图片 → generate_video → 得到视频 A
2. extract_last_frame(video_url=A) → 提取视频 A 尾帧图片
3. generate_video(image=尾帧URL, prompt=...) → 生成视频 B（与 A 连贯）
"""
from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any, Dict, Optional

from core.tools.base import ToolBase, ToolResult
from logger import logger


class ExtractLastFrameTool(ToolBase):
    name = "extract_last_frame"
    description = (
        "提取视频的最后一帧作为图片，返回本地图片路径。"
        "用途：在连续生成多段视频时，提取上一段视频的尾帧，"
        "作为下一段视频的首帧图片，保证画面一致性。"
        "输入：video_url（视频 URL 或本地路径）"
        "输出：尾帧图片的本地路径 frame_path。"
        "重要：对于 [CONTINUOUS] 连续镜头，拿到 frame_path 后必须立即将其作为"
        " generate_video 的 image 参数传入（generate_video 会自动上传本地图片），"
        "提取了尾帧却不使用会破坏镜头连贯性。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "video_url": {
                "type": "string",
                "description": "视频 URL 或本地文件路径",
            },
            "output_dir": {
                "type": "string",
                "description": "输出图片目录（可选，默认 output/frames/）",
            },
        },
        "required": ["video_url"],
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None, session_id: str = "") -> None:
        self.config = config or {}
        self._base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._session_id = session_id
        self._task_id = ""

    def set_task_id(self, task_id: str) -> None:
        """设置当前任务 ID，尾帧落到 output/<session>/<task>/frames/。"""
        self._task_id = task_id or ""

    def _resolve_output_dir(self) -> str:
        """计算尾帧输出目录：output/<session_id>/[<task_id>/]frames/。"""
        parts = [self._base_dir, "output"]
        if self._session_id:
            parts.append(self._session_id)
        if self._task_id:
            parts.append(self._task_id)
        parts.append("frames")
        return os.path.join(*parts)

    async def execute(self, video_url: str, output_dir: str = "") -> ToolResult:
        output_dir = output_dir or self._resolve_output_dir()
        os.makedirs(output_dir, exist_ok=True)

        # 生成唯一文件名
        frame_filename = f"frame_{uuid.uuid4().hex[:8]}.png"
        frame_path = os.path.join(output_dir, frame_filename)

        # 判断输入是 URL 还是本地路径
        is_url = video_url.startswith("http://") or video_url.startswith("https://")
        local_video_path = video_url

        # 如果是 URL，先下载到临时文件
        temp_video: Optional[str] = None
        if is_url:
            temp_video = os.path.join(output_dir, f"temp_{uuid.uuid4().hex[:8]}.mp4")
            try:
                download_result = await self._download_video(video_url, temp_video)
                if not download_result:
                    return ToolResult(success=False, error="视频下载失败")
                local_video_path = temp_video
            except Exception as e:
                return ToolResult(success=False, error=f"视频下载失败: {e}")

        try:
            # 用 ffmpeg 提取最后一帧
            # -sseof -0.1: 从文件末尾前 0.1 秒开始定位（兼容某些格式）
            # -frames:v 1: 只取一帧
            # -q:v 2: 高质量 PNG
            cmd = [
                "ffmpeg", "-y",
                "-sseof", "-0.1",
                "-i", local_video_path,
                "-frames:v", "1",
                "-q:v", "2",
                frame_path,
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()

            if process.returncode != 0:
                # 尝试备用方案：直接取最后一帧
                logger.warning("ffmpeg -sseof 失败，尝试备用方案")
                cmd_backup = [
                    "ffmpeg", "-y",
                    "-i", local_video_path,
                    "-vf", "select='eq(n,0)'",
                    "-vsync", "0",
                    "-frames:v", "1",
                    "-q:v", "2",
                    frame_path,
                ]
                process2 = await asyncio.create_subprocess_exec(
                    *cmd_backup,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr2 = await process2.communicate()

                if process2.returncode != 0:
                    # 最后方案：获取视频帧数后取最后一帧
                    duration_cmd = [
                        "ffprobe", "-v", "quiet",
                        "-show_entries", "format=duration",
                        "-of", "csv=p=0",
                        local_video_path,
                    ]
                    dur_proc = await asyncio.create_subprocess_exec(
                        *duration_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    dur_stdout, _ = await dur_proc.communicate()
                    duration = float(dur_stdout.decode().strip())

                    # 在 duration - 0.05 秒处截图
                    seek_time = max(0, duration - 0.05)
                    cmd3 = [
                        "ffmpeg", "-y",
                        "-ss", str(seek_time),
                        "-i", local_video_path,
                        "-frames:v", "1",
                        "-q:v", "2",
                        frame_path,
                    ]
                    process3 = await asyncio.create_subprocess_exec(
                        *cmd3,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, stderr3 = await process3.communicate()

                    if process3.returncode != 0:
                        error_msg = stderr3.decode() if stderr3 else "未知错误"
                        return ToolResult(
                            success=False,
                            error=f"提取尾帧失败: {error_msg}",
                        )

            # 验证文件存在且非空
            if not os.path.exists(frame_path) or os.path.getsize(frame_path) == 0:
                return ToolResult(success=False, error="尾帧文件生成失败或为空")

            # 构造可访问的 URL 路径
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            rel_path = os.path.relpath(frame_path, base_dir)
            # 转为 /output/... 格式的 URL
            url_path = "/" + rel_path.replace("\\", "/").replace("output/", "output/")

            logger.info(f"尾帧提取成功: {frame_path}")

            return ToolResult(
                success=True,
                data={
                    "frame_path": frame_path,
                    "frame_url": url_path,
                    "source_video": video_url,
                },
            )

        except Exception as e:
            return ToolResult(success=False, error=f"提取尾帧异常: {e}")
        finally:
            # 清理临时视频文件
            if temp_video and os.path.exists(temp_video):
                try:
                    os.remove(temp_video)
                except Exception:
                    pass

    async def _download_video(self, url: str, output_path: str) -> bool:
        """下载视频到本地临时文件。"""
        import httpx

        try:
            async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                with open(output_path, "wb") as f:
                    f.write(resp.content)
            return True
        except Exception as e:
            logger.error(f"下载视频失败: {e}")
            return False
