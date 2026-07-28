"""media_tools 包：平台媒体工具实现。

每个工具继承 core.tools.base.ToolBase，封装一类 AI 能力，供 Agent 调用。
当前已实现：
- generate_image      — 文生图（Agnes）
- generate_video      — 文生视频/图生视频（Agnes，异步）
- text_to_speech      — 语音合成（edge-tts）
- download_file       — URL 下载到本地
- ffmpeg_compose      — FFmpeg 视频合成
- extract_last_frame  — 提取视频尾帧（用于连贯视频生成）
- read_file           — 读取本地文件
- write_file          — 写入本地文件
- edit_file           — 编辑本地文件
- bash                — 执行 shell 命令
"""
from .image_gen import ImageGenTool
from .video_gen import VideoGenTool
from .tts import TTSTool
from .download import DownloadTool
from .ffmpeg_compose import FFmpegComposeTool
from .materials import ListMaterialsTool
from .extract_frame import ExtractLastFrameTool
from .file_ops import ReadFileTool, WriteFileTool, EditFileTool, BashTool
from .registry import build_default_registry, build_registry_without_agnes, build_registry_for_session

__all__ = [
    "ImageGenTool",
    "VideoGenTool",
    "TTSTool",
    "DownloadTool",
    "FFmpegComposeTool",
    "ListMaterialsTool",
    "ExtractLastFrameTool",
    "ReadFileTool",
    "WriteFileTool",
    "EditFileTool",
    "BashTool",
    "build_default_registry",
    "build_registry_without_agnes",
    "build_registry_for_session",
]
