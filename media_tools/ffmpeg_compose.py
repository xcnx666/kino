"""FFmpeg 视频合成工具：拼接视频/图片片段，叠加音频，输出 MP4。"""
from __future__ import annotations

import asyncio
import os
import re
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from core.tools.base import ToolBase, ToolResult
from logger import logger


# 缓存 filter 可用性检测结果
_libass_available: Optional[bool] = None
_drawtext_available: Optional[bool] = None


async def _check_filter_available(filter_name: str) -> bool:
    """检测 FFmpeg 是否支持指定 filter。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-filters", "-hide_banner",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode("utf-8", errors="replace")
        return filter_name in output
    except Exception:
        return False


async def _check_libass_support() -> bool:
    """检测当前 FFmpeg 是否编译了 libass（subtitles filter 可用性）。"""
    global _libass_available
    if _libass_available is not None:
        return _libass_available
    _libass_available = await _check_filter_available("subtitles")
    if _libass_available:
        logger.info("FFmpeg 支持 subtitles filter（libass 已编译）")
    else:
        logger.warning("FFmpeg 不支持 subtitles filter（libass 未编译）")
    return _libass_available


async def _check_drawtext_support() -> bool:
    """检测当前 FFmpeg 是否支持 drawtext filter（需要 libfreetype）。"""
    global _drawtext_available
    if _drawtext_available is not None:
        return _drawtext_available
    _drawtext_available = await _check_filter_available("drawtext")
    if _drawtext_available:
        logger.info("FFmpeg 支持 drawtext filter")
    else:
        logger.warning("FFmpeg 不支持 drawtext filter（libfreetype 未编译）")
    return _drawtext_available


def _parse_srt(srt_path: str) -> List[Tuple[float, float, str]]:
    """解析 SRT 字幕文件，返回 [(start_sec, end_sec, text), ...]。"""
    entries: List[Tuple[float, float, str]] = []
    try:
        with open(srt_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        try:
            with open(srt_path, "r", encoding="gbk") as f:
                content = f.read()
        except Exception as e:
            logger.error(f"读取 SRT 文件失败: {e}")
            return entries

    # 匹配 SRT 时间戳格式: 00:00:00,000 --> 00:00:04,500
    pattern = re.compile(
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
    )

    blocks = re.split(r"\n\s*\n", content.strip())
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 2:
            continue
        # 找到时间戳行
        time_line_idx = -1
        for i, line in enumerate(lines):
            if "-->" in line:
                time_line_idx = i
                break
        if time_line_idx < 0:
            continue
        match = pattern.search(lines[time_line_idx])
        if not match:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = match.groups()
        start = int(h1) * 3600 + int(m1) * 60 + int(s1) + int(ms1) / 1000.0
        end = int(h2) * 3600 + int(m2) * 60 + int(s2) + int(ms2) / 1000.0
        # 字幕文本（时间戳行之后的所有行）
        text_lines = lines[time_line_idx + 1:]
        text = "\n".join(text_lines).strip()
        if text:
            entries.append((start, end, text))
    return entries


def _escape_drawtext(text: str) -> str:
    """转义 drawtext filter 中的特殊字符。"""
    # drawtext 中需要转义的字符
    result = text.replace("\\", "\\\\")
    result = result.replace(":", "\\:")
    result = result.replace("'", "\\'")
    result = result.replace("%", "\\%")
    # 换行符用 %{n} 替代（drawtext 不支持直接换行）
    result = result.replace("\n", " ")
    return result


def _build_drawtext_filter_chain(
    entries: List[Tuple[float, float, str]],
    font_name: str = "PingFang SC",
    font_size: int = 24,
) -> str:
    """从 SRT 条目构建 drawtext filter 链。

    返回可直接用于 -vf 的 filter 字符串。
    """
    filters: List[str] = []
    for start, end, text in entries:
        escaped = _escape_drawtext(text)
        # 居中显示在底部，带半透明背景框
        f = (
            f"drawtext=fontfile='':text='{escaped}':"
            f"fontcolor=white:fontsize={font_size}:"
            f"x=(w-text_w)/2:y=h-text_h-40:"
            f"box=1:boxcolor=black@0.6:boxborderw=8:"
            f"enable='between(t,{start},{end})'"
        )
        filters.append(f)
    return ",".join(filters)


def _find_chinese_font() -> Optional[str]:
    """查找系统中可用的中文字体路径。"""
    font_candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/Library/Fonts/Songti.ttc",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for path in font_candidates:
        if os.path.exists(path):
            return path
    return None


def _render_subtitle_images(
    entries: List[Tuple[float, float, str]],
    output_dir: str,
    video_width: int = 1280,
    font_size: int = 36,
) -> List[Tuple[str, float, float]]:
    """用 PIL 将每条字幕渲染为透明 PNG 图片。

    返回 [(png_path, start_sec, end_sec), ...]
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        logger.error("Pillow 未安装，无法使用图片字幕降级方案")
        return []

    font_path = _find_chinese_font()
    os.makedirs(output_dir, exist_ok=True)
    results: List[Tuple[str, float, float]] = []

    for i, (start, end, text) in enumerate(entries):
        try:
            # 先测量文字尺寸
            font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default()
            # PIL 的 getbbox 获取文字边界
            temp_img = Image.new("RGBA", (1, 1))
            temp_draw = ImageDraw.Draw(temp_img)
            bbox = temp_draw.textbbox((0, 0), text, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]

            # 添加 padding
            padding_x = 24
            padding_y = 16
            img_w = text_w + padding_x * 2
            img_h = text_h + padding_y * 2

            # 创建透明图片
            img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)

            # 绘制半透明黑色背景
            draw.rounded_rectangle(
                [0, 0, img_w - 1, img_h - 1],
                radius=8,
                fill=(0, 0, 0, 160),
            )

            # 绘制白色文字
            draw.text(
                (padding_x - bbox[0], padding_y - bbox[1]),
                text,
                font=font,
                fill=(255, 255, 255, 255),
            )

            # 保存
            png_path = os.path.join(output_dir, f"subtitle_{i:03d}.png")
            img.save(png_path)
            results.append((png_path, start, end))

        except Exception as e:
            logger.warning(f"渲染字幕图片失败 (#{i}): {e}")

    return results


class FFmpegComposeTool(ToolBase):
    name = "ffmpeg_compose"
    description = (
        "使用 FFmpeg 将多个视频片段和/或图片合成为最终视频。"
        "支持视频拼接、图片转视频、音频叠加、字幕烧录。"
        "返回最终 MP4 文件路径。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "clips": {
                "type": "array",
                "description": (
                    "视频片段列表，每个元素为 {type, path, duration}。"
                    "type='video' 时 path 是视频文件路径；"
                    "type='image' 时 path 是图片路径，需提供 duration（秒）。"
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["video", "image"]},
                        "path": {"type": "string"},
                        "duration": {"type": "number", "description": "图片持续秒数（type=image 时必填）"},
                    },
                },
            },
            "audio_path": {
                "type": "string",
                "description": "背景音频文件路径（MP3/WAV），可选",
            },
            "subtitle_path": {
                "type": "string",
                "description": "SRT 字幕文件路径，可选，烧录到视频中",
            },
            "output_path": {
                "type": "string",
                "description": "输出 MP4 文件路径",
            },
        },
        "required": ["clips", "output_path"],
    }

    def __init__(self, session_id: str = "") -> None:
        self._session_id = session_id
        self._task_id = ""
        self._base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def set_task_id(self, task_id: str) -> None:
        """设置当前任务 ID，输出文件落到 output/<session>/<task>/final/。"""
        self._task_id = task_id or ""

    def _redirect_path(self, output_path: str) -> str:
        """将输出路径重定向到 output/<session_id>/[<task_id>/]final/。"""
        if not self._session_id:
            return output_path
        filename = os.path.basename(output_path)
        parts = [self._base_dir, "output", self._session_id]
        if self._task_id:
            parts.append(self._task_id)
        parts.append("final")
        return os.path.join(*parts, filename)

    async def _run_ffmpeg(self, cmd: List[str]) -> tuple[bool, str]:
        """执行 ffmpeg 命令，返回 (成功, 输出/错误信息)。"""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            return True, stdout.decode("utf-8", errors="replace")
        return False, stderr.decode("utf-8", errors="replace")

    async def _image_to_video(
        self, image_path: str, duration: float, tmpdir: str, index: int, width: int = 1280, height: int = 720
    ) -> str:
        """将单张图片转为指定时长的视频片段。"""
        out_path = os.path.join(tmpdir, f"segment_{index:03d}.mp4")
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-t", str(duration),
            "-i", image_path,
            "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,fps=24",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-r", "24",
            out_path,
        ]
        ok, msg = await self._run_ffmpeg(cmd)
        if not ok:
            raise RuntimeError(f"图片转视频失败 ({image_path}): {msg[:200]}")
        return out_path

    async def _normalize_video(
        self, video_path: str, tmpdir: str, index: int, width: int = 1280, height: int = 720
    ) -> str:
        """将视频标准化（统一分辨率、帧率、编码），确保拼接不出错。"""
        out_path = os.path.join(tmpdir, f"segment_{index:03d}.mp4")
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,fps=24",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-r", "24",
            "-an",
            out_path,
        ]
        ok, msg = await self._run_ffmpeg(cmd)
        if not ok:
            raise RuntimeError(f"视频标准化失败 ({video_path}): {msg[:200]}")
        return out_path

    async def execute(
        self,
        clips: List[Dict[str, Any]],
        output_path: str,
        audio_path: Optional[str] = None,
        subtitle_path: Optional[str] = None,
    ) -> ToolResult:
        try:
            if not clips:
                return ToolResult(success=False, error="clips 列表为空")

            # 重定向输出路径到会话目录
            output_path = self._redirect_path(output_path)
            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

            # 临时目录存放标准化后的片段
            tmpdir = tempfile.mkdtemp(prefix="ffmpeg_compose_")
            segment_paths: List[str] = []

            # 阶段 1：将所有片段标准化为统一格式
            for i, clip in enumerate(clips):
                clip_type = clip.get("type", "video")
                clip_path = clip.get("path", "")

                if not clip_path or not os.path.exists(clip_path):
                    return ToolResult(success=False, error=f"片段文件不存在: {clip_path}")

                if clip_type == "image":
                    duration = clip.get("duration", 5)
                    seg = await self._image_to_video(clip_path, float(duration), tmpdir, i)
                else:
                    seg = await self._normalize_video(clip_path, tmpdir, i)
                segment_paths.append(seg)

            # 阶段 2：拼接所有片段
            concat_list_path = os.path.join(tmpdir, "concat_list.txt")
            with open(concat_list_path, "w", encoding="utf-8") as f:
                for seg in segment_paths:
                    # concat demuxer 要求路径用单引号包裹
                    f.write(f"file '{seg}'\n")

            concat_output = os.path.join(tmpdir, "concat_result.mp4")
            concat_cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", concat_list_path,
                "-c", "copy",
                concat_output,
            ]
            ok, msg = await self._run_ffmpeg(concat_cmd)
            if not ok:
                # concat copy 失败时回退到重新编码
                concat_cmd = [
                    "ffmpeg", "-y",
                    "-f", "concat", "-safe", "0",
                    "-i", concat_list_path,
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    concat_output,
                ]
                ok, msg = await self._run_ffmpeg(concat_cmd)
                if not ok:
                    return ToolResult(success=False, error=f"视频拼接失败: {msg[:300]}")

            # 阶段 3：叠加音频 + 字幕
            # 字幕烧录三级降级：subtitles filter → drawtext filter → PIL 图片 overlay
            use_overlay_mode = False
            overlay_images: List[Tuple[str, float, float]] = []

            if subtitle_path and os.path.exists(subtitle_path):
                srt_entries = _parse_srt(subtitle_path)
                if not srt_entries:
                    logger.warning(f"SRT 文件解析为空，跳过字幕烧录: {subtitle_path}")
                    vf_filters = []
                else:
                    # 第一级：尝试 subtitles filter（libass）
                    libass_ok = await _check_libass_support()
                    if libass_ok:
                        escaped_path = subtitle_path.replace("'", "\\'")
                        vf_filters = [f"subtitles='{escaped_path}'"]
                        logger.info(f"使用 subtitles filter 烧录字幕: {subtitle_path}")
                    else:
                        # 第二级：尝试 drawtext filter（libfreetype）
                        drawtext_ok = await _check_drawtext_support()
                        if drawtext_ok:
                            vf_filters = [_build_drawtext_filter_chain(srt_entries)]
                            logger.info(f"使用 drawtext 降级方案烧录字幕: {len(srt_entries)} 条字幕")
                        else:
                            # 第三级：PIL 图片 + overlay filter（始终可用）
                            sub_dir = os.path.join(tmpdir, "subtitles")
                            overlay_images = _render_subtitle_images(srt_entries, sub_dir)
                            if overlay_images:
                                use_overlay_mode = True
                                logger.info(f"使用 PIL 图片 overlay 降级方案烧录字幕: {len(overlay_images)} 条字幕")
                            else:
                                logger.warning("PIL 图片渲染失败，跳过字幕烧录")
                                vf_filters = []
            else:
                vf_filters = []

            if use_overlay_mode:
                # overlay 模式：使用 -filter_complex
                final_cmd = ["ffmpeg", "-y", "-i", concat_output]
                input_count = 1
                if audio_path and os.path.exists(audio_path):
                    final_cmd.extend(["-i", audio_path])
                    audio_input_idx = input_count
                    input_count += 1
                # 添加字幕图片作为输入
                for img_path, _, _ in overlay_images:
                    final_cmd.extend(["-i", img_path])
                    input_count += 1

                # 构建 filter_complex overlay 链
                filter_parts: List[str] = []
                prev_label = "0:v"
                for idx, (_, start, end) in enumerate(overlay_images):
                    img_input_idx = 1 + (1 if audio_path and os.path.exists(audio_path) else 0) + idx
                    out_label = f"v{idx}"
                    filter_parts.append(
                        f"[{prev_label}][{img_input_idx}:v]overlay=(W-w)/2:H-h-40:"
                        f"enable='between(t,{start},{end})'[{out_label}]"
                    )
                    prev_label = out_label
                final_cmd.extend(["-filter_complex", ";".join(filter_parts)])

                # 映射输出
                final_cmd.extend(["-map", f"[{prev_label}]"])
                if audio_path and os.path.exists(audio_path):
                    final_cmd.extend([
                        "-map", f"{audio_input_idx}:a",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "128k", "-shortest",
                    ])
                else:
                    final_cmd.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p"])
            else:
                # 普通模式：使用 -vf（subtitles 或 drawtext）
                final_cmd = ["ffmpeg", "-y", "-i", concat_output]
                input_count = 1
                if audio_path and os.path.exists(audio_path):
                    final_cmd.extend(["-i", audio_path])
                    audio_input_idx = input_count
                    input_count += 1

                if vf_filters:
                    final_cmd.extend(["-vf", ",".join(vf_filters)])

                if audio_path and os.path.exists(audio_path):
                    final_cmd.extend([
                        "-map", "0:v",
                        "-map", f"{audio_input_idx}:a",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "128k", "-shortest",
                    ])
                else:
                    final_cmd.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p"])

            final_cmd.append(output_path)
            ok, msg = await self._run_ffmpeg(final_cmd)
            if not ok:
                return ToolResult(success=False, error=f"最终合成失败: {msg[:300]}")

            # 验证输出
            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                return ToolResult(success=False, error="输出文件不存在或大小为 0")

            size = os.path.getsize(output_path)
            return ToolResult(
                success=True,
                data={
                    "file_path": output_path,
                    "size": size,
                    "clips_count": len(clips),
                },
            )

        except Exception as e:  # noqa: BLE001
            return ToolResult(success=False, error=f"FFmpeg 合成失败: {e}")
