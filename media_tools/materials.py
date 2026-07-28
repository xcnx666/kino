"""素材库查看工具：让 Agent 主动查看当前会话素材库中的素材。

每个聊天会话会创建独立素材目录 uploads/<session_id>/{images,videos,text,other}。
本工具列出该目录下所有素材，并返回：
- 文本文件：完整内容（便于 Agent 直接理解素材）
- 图片文件：尺寸、可访问 URL（便于 Agent 引用）
- 视频文件：时长、分辨率（便于 Agent 规划）
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, Optional

from core.tools.base import ToolBase, ToolResult
from logger import logger


class ListMaterialsTool(ToolBase):
    """列出当前会话素材库中的素材，包含内容预览。"""

    name = "list_materials"
    description = (
        "列出当前会话素材库中的所有素材文件，并返回详细内容。\n"
        "- 文本文件：返回完整文本内容\n"
        "- 图片文件：返回尺寸、可访问 URL\n"
        "- 视频文件：返回时长、分辨率\n"
        "在生成视频前必须调用此工具，了解用户上传的素材内容，"
        "从而根据素材生成贴合用户意图的视频。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "可选，指定只查看某一类素材：images/videos/text/other。留空则查看全部。",
            },
        },
        "required": [],
    }

    # 项目根目录
    _BASE_DIR = Path(__file__).resolve().parent.parent
    _UPLOAD_DIR = _BASE_DIR / "uploads"
    _CATEGORIES = ["images", "videos", "text", "other"]
    _EXT_MAP = {
        "images": (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"),
        "videos": (".mp4", ".mov", ".avi", ".mkv", ".webm"),
        "text": (".txt", ".md", ".json"),
    }
    _TEXT_PREVIEW_LIMIT = 3000  # 文本内容预览最大字符数
    _IMAGE_BASE64_LIMIT = 4 * 1024 * 1024  # 图片 base64 编码上限 4MB（避免 token 爆炸）

    def __init__(self, session_id: str = "") -> None:
        self.session_id = session_id
        # 会话级素材目录
        self.materials_dir = self._UPLOAD_DIR / session_id if session_id else self._UPLOAD_DIR

    def _classify(self, ext: str) -> str:
        ext = ext.lower()
        for cat, exts in self._EXT_MAP.items():
            if ext in exts:
                return cat
        return "other"

    def _get_file_url(self, file_path: Path) -> str:
        """构造可通过 Web 访问的 URL 路径。"""
        try:
            rel = file_path.relative_to(self._BASE_DIR)
            return "/" + str(rel).replace("\\", "/")
        except ValueError:
            return str(file_path)

    # 支持的文本编码列表（按优先级排序）
    _ENCODINGS = ["utf-8", "gbk", "gb2312", "gb18030", "big5", "latin-1"]

    def _read_text_content(self, file_path: Path) -> str:
        """读取文本文件内容，限制长度。依次尝试多种编码，避免 GBK 等编码报错。"""
        for encoding in self._ENCODINGS:
            try:
                content = file_path.read_text(encoding=encoding)
                if len(content) > self._TEXT_PREVIEW_LIMIT:
                    return content[:self._TEXT_PREVIEW_LIMIT] + "\n...(内容已截断)"
                return content
            except UnicodeDecodeError:
                continue
            except Exception as e:
                return f"[读取失败: {e}]"
        # 所有编码都失败，用 latin-1 + errors="replace" 兜底
        try:
            content = file_path.read_text(encoding="latin-1", errors="replace")
            if len(content) > self._TEXT_PREVIEW_LIMIT:
                return content[:self._TEXT_PREVIEW_LIMIT] + "\n...(内容已截断)"
            return content
        except Exception:
            return f"[无法读取文件内容，文件路径: {file_path}]"

    def _get_image_info(self, file_path: Path) -> Dict[str, Any]:
        """获取图片尺寸信息。"""
        info: Dict[str, Any] = {}
        try:
            from PIL import Image
            with Image.open(file_path) as img:
                info["width"] = img.width
                info["height"] = img.height
                info["format"] = img.format or ""
        except ImportError:
            # PIL 不可用，用 ffprobe
            info.update(self._ffprobe_info(file_path))
        except Exception as e:
            info["error"] = str(e)
        return info

    def _get_image_base64(self, file_path: Path) -> str:
        """读取图片并返回 base64 data URL（用于多模态模型理解图片内容）。

        如果图片过大（超过 _IMAGE_BASE64_LIMIT），返回空字符串。
        """
        try:
            import base64
            file_size = file_path.stat().st_size
            if file_size > self._IMAGE_BASE64_LIMIT:
                logger.info(f"图片 {file_path.name} 太大 ({file_size} bytes)，跳过 base64 编码")
                return ""

            ext = file_path.suffix.lower()
            mime_map = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".gif": "image/gif",
                ".webp": "image/webp", ".bmp": "image/bmp",
            }
            mime = mime_map.get(ext, "image/jpeg")

            with open(file_path, "rb") as f:
                data = base64.b64encode(f.read()).decode("utf-8")
            return f"data:{mime};base64,{data}"
        except Exception as e:
            logger.warning(f"读取图片 base64 失败 {file_path}: {e}")
            return ""

    def _ffprobe_info(self, file_path: Path) -> Dict[str, Any]:
        """用 ffprobe 获取媒体文件信息。"""
        info: Dict[str, Any] = {}
        try:
            import subprocess
            result = subprocess.run(
                [
                    "ffprobe", "-v", "quiet",
                    "-print_format", "json",
                    "-show_streams", "-show_format",
                    str(file_path),
                ],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                import json
                data = json.loads(result.stdout)
                streams = data.get("streams", [])
                for s in streams:
                    if s.get("codec_type") == "video":
                        info["width"] = s.get("width", 0)
                        info["height"] = s.get("height", 0)
                        info["codec"] = s.get("codec_name", "")
                        break
                fmt = data.get("format", {})
                if "duration" in fmt:
                    info["duration"] = float(fmt["duration"])
        except Exception as e:
            info["error"] = str(e)
        return info

    def _get_video_info(self, file_path: Path) -> Dict[str, Any]:
        """获取视频信息（时长、分辨率）。"""
        return self._ffprobe_info(file_path)

    async def execute(self, category: str = "") -> ToolResult:
        """列出素材文件，包含内容预览。"""
        if not self.materials_dir.exists():
            return ToolResult(
                success=True,
                data={
                    "session_id": self.session_id,
                    "materials": [],
                    "message": "素材库为空，暂无上传的素材。",
                },
            )

        categories = [category] if category and category in self._CATEGORIES else self._CATEGORIES
        materials: list = []

        for cat in categories:
            cat_dir = self.materials_dir / cat
            if not cat_dir.exists():
                continue
            for f in cat_dir.iterdir():
                if not f.is_file():
                    continue

                item: Dict[str, Any] = {
                    "name": f.name,
                    "category": cat,
                    "path": str(f),
                    "url": self._get_file_url(f),
                    "size": f.stat().st_size,
                }

                # 根据类型添加内容预览
                if cat == "text":
                    item["content"] = self._read_text_content(f)
                elif cat == "images":
                    item.update(self._get_image_info(f))
                    # 添加 base64 数据供多模态模型理解图片内容
                    item["image_data"] = self._get_image_base64(f)
                elif cat == "videos":
                    item.update(self._get_video_info(f))

                materials.append(item)

        if not materials:
            msg = "素材库为空，暂无上传的素材。"
        else:
            # 构建摘要
            parts = []
            for m in materials:
                if m["category"] == "text":
                    preview = m.get("content", "")[:200]
                    parts.append(f"[文本] {m['name']}: {preview}")
                elif m["category"] == "images":
                    w = m.get("width", "?")
                    h = m.get("height", "?")
                    parts.append(f"[图片] {m['name']} ({w}x{h})")
                elif m["category"] == "videos":
                    dur = m.get("duration", 0)
                    parts.append(f"[视频] {m['name']} ({dur:.1f}s)")
                else:
                    parts.append(f"[其他] {m['name']}")
            msg = f"素材库共有 {len(materials)} 个素材文件：\n" + "\n".join(parts)

        return ToolResult(
            success=True,
            data={
                "session_id": self.session_id,
                "materials": materials,
                "message": msg,
            },
        )

    def get_summary(self) -> str:
        """获取素材库摘要（用于注入系统提示词），包含文本内容。"""
        if not self.materials_dir.exists():
            return "暂无素材"

        parts: list = []
        for cat in self._CATEGORIES:
            cat_dir = self.materials_dir / cat
            if not cat_dir.exists():
                continue
            for f in cat_dir.iterdir():
                if not f.is_file():
                    continue
                if cat == "text":
                    content = self._read_text_content(f)
                    parts.append(f"[文本] {f.name}:\n{content}")
                elif cat == "images":
                    info = self._get_image_info(f)
                    w = info.get("width", "?")
                    h = info.get("height", "?")
                    url = self._get_file_url(f)
                    parts.append(f"[图片] {f.name} ({w}x{h}, URL: {url})")
                elif cat == "videos":
                    info = self._get_video_info(f)
                    dur = info.get("duration", 0)
                    w = info.get("width", "?")
                    h = info.get("height", "?")
                    parts.append(f"[视频] {f.name} ({dur:.1f}s, {w}x{h})")
                else:
                    parts.append(f"[其他] {f.name}")

        if not parts:
            return "暂无素材"
        return "\n".join(parts)

    def get_materials_dir(self) -> Path:
        """获取素材目录路径。"""
        return self.materials_dir

    def ensure_dirs(self) -> None:
        """确保素材目录及子分类目录存在。"""
        for cat in self._CATEGORIES:
            (self.materials_dir / cat).mkdir(parents=True, exist_ok=True)
