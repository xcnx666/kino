"""视频生成主编排器：串联从创意输入到最终 MP4 输出的完整流程。

支持两种运行模式：
1. 完整模式（run）：需要 LLM + Agnes API key，Agent 自主规划并调用工具。
2. 演示模式（demo_run）：不需要 LLM / API key，用占位素材验证 FFmpeg 合成闭环。

核心流程：
  创意输入 → 剧本/分镜 → Prompt 生成 → 媒体工具调用 → 下载资产 → FFmpeg 合成 → 输出 MP4
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.tools.base import ToolRegistry, ToolResult
from logger import logger


@dataclass
class Shot:
    """单个分镜的定义。"""
    shot_id: int
    visual_prompt: str = ""
    tts_text: str = ""
    duration: float = 5.0
    # 生成结果
    image_url: Optional[str] = None
    video_url: Optional[str] = None
    image_path: Optional[str] = None
    video_path: Optional[str] = None
    audio_path: Optional[str] = None


@dataclass
class VideoProject:
    """一个视频项目的完整数据。"""
    name: str
    creative_request: str
    script: str = ""
    shots: List[Shot] = field(default_factory=list)
    output_dir: str = ""
    final_path: str = ""

    def ensure_dirs(self, base_dir: str = "output") -> None:
        """创建项目目录结构。"""
        self.output_dir = os.path.join(base_dir, self.name)
        for sub in ("images", "videos", "audio"):
            os.makedirs(os.path.join(self.output_dir, sub), exist_ok=True)


class VideoOrchestrator:
    """视频生成主编排器。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def run(
        self,
        creative_request: str,
        project_name: str = "video_project",
    ) -> VideoProject:
        """完整模式：从创意输入到最终 MP4。

        需要 LLM 可用（用于剧本/分镜/Prompt 生成）和 Agnes API key。
        """
        project = VideoProject(name=project_name, creative_request=creative_request)
        project.ensure_dirs()

        logger.info(f"=== 项目启动: {project_name} ===")
        logger.info(f"创意需求: {creative_request}")

        # Phase 1-4: 剧本/分镜/Prompt 生成（需要 LLM）
        # 这部分由 Agent 负责决策，这里暂用简化流程
        logger.info("Phase 1-4: 剧本与分镜生成（需 LLM）...")
        project.shots = await self._generate_storyboard(creative_request, project)

        # Phase 5: 媒体工具调用
        logger.info(f"Phase 5: 媒体生成（{len(project.shots)} 个分镜）...")
        await self._generate_media(project)

        # Phase 6: 下载资产
        logger.info("Phase 6: 资产下载...")
        await self._download_assets(project)

        # Phase 7: FFmpeg 合成
        logger.info("Phase 7: FFmpeg 合成...")
        await self._compose_final(project)

        logger.info(f"=== 项目完成: {project.final_path} ===")
        return project

    async def demo_run(self, project_name: str = "demo") -> VideoProject:
        """演示模式：用占位素材验证 FFmpeg 合成闭环。

        不需要 LLM / Agnes API key / edge-tts 网络。
        用 ffmpeg 生成纯色测试图片 + 静音音频，验证完整合成流程。
        """
        project = VideoProject(
            name=project_name,
            creative_request="[演示模式] 生成测试视频验证 FFmpeg 合成闭环",
        )
        project.ensure_dirs()
        logger.info(f"=== 演示模式启动: {project_name} ===")

        # 用 ffmpeg 生成 3 张纯色图片作为分镜
        colors = [(255, 80, 80), (80, 200, 100), (80, 120, 255)]
        labels = ["Shot 1", "Shot 2", "Shot 3"]
        for i, (color, label) in enumerate(zip(colors, labels)):
            shot = Shot(
                shot_id=i + 1,
                visual_prompt=f"测试图片 {label}",
                tts_text=f"这是第 {i+1} 个镜头的旁白。",
                duration=3.0,
            )
            shot.image_path = await self._generate_placeholder_image(
                os.path.join(project.output_dir, "images", f"shot_{i+1}.png"),
                color, label,
            )
            project.shots.append(shot)
            logger.info(f"  分镜 {i+1}: 占位图片已生成")

        # 生成静音音频（用 ffmpeg）
        audio_path = os.path.join(project.output_dir, "audio", "silent.aac")
        await self._generate_silent_audio(audio_path, total_duration=9.0)
        logger.info(f"  静音音频已生成: {audio_path}")

        # FFmpeg 合成
        logger.info("Phase 7: FFmpeg 合成...")
        clips = [
            {"type": "image", "path": shot.image_path, "duration": shot.duration}
            for shot in project.shots
        ]
        project.final_path = os.path.join(project.output_dir, "final.mp4")
        result = await self.registry.call(
            "ffmpeg_compose",
            clips=clips,
            audio_path=audio_path,
            output_path=project.final_path,
        )

        if result.success:
            logger.info(f"=== 演示完成: {project.final_path} ({result.data['size']} bytes) ===")
        else:
            logger.error(f"演示失败: {result.error}")

        return project

    async def _generate_storyboard(
        self, creative_request: str, project: VideoProject
    ) -> List[Shot]:
        """生成剧本和分镜（需要 LLM）。

        当前为简化实现，后续接入 Agent 后由 Agent 决策。
        """
        # TODO: 接入 LLM/Agent 生成剧本和分镜
        # 暂时返回空列表，由 demo_run 提供测试数据
        return []

    async def _generate_media(self, project: VideoProject) -> None:
        """为每个分镜调用媒体生成工具。"""
        for shot in project.shots:
            # 生成图片
            if self.registry.get("generate_image") and shot.visual_prompt:
                result = await self.registry.call(
                    "generate_image",
                    prompt=shot.visual_prompt,
                    size="1280x720",
                )
                if result.success and result.data.get("images"):
                    shot.image_url = result.data["images"][0]
                    logger.info(f"  分镜 {shot.shot_id}: 图片生成成功")

            # 生成 TTS
            if self.registry.get("text_to_speech") and shot.tts_text:
                audio_path = os.path.join(
                    project.output_dir, "audio", f"shot_{shot.shot_id}.mp3"
                )
                result = await self.registry.call(
                    "text_to_speech",
                    text=shot.tts_text,
                    output_path=audio_path,
                )
                if result.success:
                    shot.audio_path = audio_path
                    logger.info(f"  分镜 {shot.shot_id}: TTS 生成成功")

    async def _download_assets(self, project: VideoProject) -> None:
        """下载所有远程资产到本地。"""
        for shot in project.shots:
            if shot.image_url and self.registry.get("download_file"):
                local_path = os.path.join(
                    project.output_dir, "images", f"shot_{shot.shot_id}.png"
                )
                result = await self.registry.call(
                    "download_file",
                    url=shot.image_url,
                    output_path=local_path,
                )
                if result.success:
                    shot.image_path = local_path
                    logger.info(f"  分镜 {shot.shot_id}: 图片下载成功")

            if shot.video_url and self.registry.get("download_file"):
                local_path = os.path.join(
                    project.output_dir, "videos", f"shot_{shot.shot_id}.mp4"
                )
                result = await self.registry.call(
                    "download_file",
                    url=shot.video_url,
                    output_path=local_path,
                )
                if result.success:
                    shot.video_path = local_path
                    logger.info(f"  分镜 {shot.shot_id}: 视频下载成功")

    async def _compose_final(self, project: VideoProject) -> None:
        """FFmpeg 合成最终视频。"""
        clips: List[Dict[str, Any]] = []
        for shot in project.shots:
            if shot.video_path:
                clips.append({"type": "video", "path": shot.video_path})
            elif shot.image_path:
                clips.append({
                    "type": "image",
                    "path": shot.image_path,
                    "duration": shot.duration,
                })

        if not clips:
            logger.error("没有可用的视频/图片片段，无法合成")
            return

        # 合并所有 TTS 音频为一条
        audio_path = None
        audio_files = [s.audio_path for s in project.shots if s.audio_path]
        if audio_files:
            audio_path = os.path.join(project.output_dir, "audio", "merged.aac")
            await self._concat_audio(audio_files, audio_path)

        project.final_path = os.path.join(project.output_dir, "final.mp4")
        result = await self.registry.call(
            "ffmpeg_compose",
            clips=clips,
            audio_path=audio_path,
            output_path=project.final_path,
        )

        if not result.success:
            logger.error(f"FFmpeg 合成失败: {result.error}")

    async def _concat_audio(self, audio_files: List[str], output_path: str) -> None:
        """拼接多个音频文件。"""
        if not audio_files:
            return

        import tempfile
        tmpdir = tempfile.mkdtemp(prefix="audio_concat_")
        list_path = os.path.join(tmpdir, "list.txt")

        with open(list_path, "w", encoding="utf-8") as f:
            for af in audio_files:
                f.write(f"file '{af}'\n")

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", list_path,
            "-c:a", "aac", "-b:a", "128k",
            output_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(f"音频拼接失败: {stderr.decode('utf-8', errors='replace')[:200]}")

    async def _generate_placeholder_image(
        self, path: str, color: tuple, label: str
    ) -> str:
        """用 PIL 生成纯色测试图片（带文字标签）。"""
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            return await self._generate_placeholder_image_ffmpeg(path, color)

        r, g, b = color
        img = Image.new("RGB", (1280, 720), color=(r, g, b))
        draw = ImageDraw.Draw(img)
        # 尝试加载系统字体，失败则用默认
        font_size = 72
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
        except Exception:
            font = ImageFont.load_default()
        # 居中绘制文字
        bbox = draw.textbbox((0, 0), label, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        x = (1280 - text_w) // 2
        y = (720 - text_h) // 2
        draw.text((x, y), label, fill="white", font=font)
        img.save(path)
        return path

    async def _generate_placeholder_image_ffmpeg(
        self, path: str, color: tuple
    ) -> str:
        """ffmpeg 纯色图片（无文字，PIL 不可用时降级）。"""
        r, g, b = color
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c=0x{r:02x}{g:02x}{b:02x}:s=1280x720:d=1",
            "-frames:v", "1",
            path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return path

    async def _generate_silent_audio(self, path: str, total_duration: float) -> str:
        """用 ffmpeg 生成静音音频。"""
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", str(total_duration),
            "-c:a", "aac", "-b:a", "128k",
            path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return path
