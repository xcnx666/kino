"""文件操作工具：为 Agent 提供读、写、编辑、执行命令的能力。

所有工具继承 ToolBase（异步），统一注册到 ToolRegistry，
与 media_tools 其他工具（generate_image/video 等）使用同一套调用机制。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, Optional

from core.tools.base import ToolBase, ToolResult
from logger import logger


class ReadFileTool(ToolBase):
    name = "read_file"
    description = (
        "读取本地文件内容，返回文本。"
        "用于读取脚本、配置文件、素材文本等。"
        "自动检测文件编码（UTF-8 / GBK / GB2312 / Latin-1）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "文件路径（相对路径基于项目根目录）",
            },
        },
        "required": ["file_path"],
    }

    # 支持的文本编码列表（按优先级排序）
    _ENCODINGS = ["utf-8", "gbk", "gb2312", "gb18030", "big5", "latin-1"]
    # 二进制文件扩展名（直接读取会报编码错误）
    _BINARY_EXTS = {
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico",
        ".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv",
        ".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a",
        ".zip", ".tar", ".gz", ".rar", ".7z",
        ".exe", ".dll", ".so", ".dylib", ".bin",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx",
        ".ttf", ".otf", ".woff", ".woff2",
    }

    @staticmethod
    def _is_binary_file(file_path: str) -> bool:
        """判断是否为二进制文件（通过扩展名和内容检测）。"""
        ext = os.path.splitext(file_path)[1].lower()
        if ext in ReadFileTool._BINARY_EXTS:
            return True
        # 读取前 1024 字节检测是否包含 null 字节
        try:
            with open(file_path, "rb") as f:
                chunk = f.read(1024)
            return b"\x00" in chunk
        except Exception:
            return False

    async def execute(self, file_path: str) -> ToolResult:
        try:
            # 支持相对路径
            if not os.path.isabs(file_path):
                base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                file_path = os.path.join(base_dir, file_path)

            if not os.path.exists(file_path):
                return ToolResult(success=False, error=f"文件不存在: {file_path}")
            if os.path.isdir(file_path):
                return ToolResult(success=False, error=f"路径是目录而非文件: {file_path}")

            # 二进制文件不尝试文本解码
            if self._is_binary_file(file_path):
                file_size = os.path.getsize(file_path)
                return ToolResult(
                    success=False,
                    error=f"该文件是二进制文件（{os.path.splitext(file_path)[1]}），无法作为文本读取。文件大小: {file_size} bytes",
                )

            # 依次尝试多种编码
            last_error = None
            for encoding in self._ENCODINGS:
                try:
                    with open(file_path, "r", encoding=encoding) as f:
                        content = f.read()
                    return ToolResult(
                        success=True,
                        data={
                            "file_path": file_path,
                            "content": content,
                            "size": len(content),
                            "encoding": encoding,
                        },
                    )
                except UnicodeDecodeError as e:
                    last_error = e
                    continue
                except Exception as e:
                    return ToolResult(success=False, error=f"读取文件失败: {e}")

            # 所有编码都失败，用 latin-1 + errors="replace" 兜底（永远不会报错）
            try:
                with open(file_path, "r", encoding="latin-1", errors="replace") as f:
                    content = f.read()
                return ToolResult(
                    success=True,
                    data={
                        "file_path": file_path,
                        "content": content,
                        "size": len(content),
                        "encoding": "latin-1 (with replacement)",
                        "warning": "文件编码无法准确识别，部分字符可能显示异常",
                    },
                )
            except Exception as e:
                return ToolResult(
                    success=False,
                    error=f"文件编码读取失败（已尝试 {', '.join(self._ENCODINGS)}）: {last_error}",
                )

        except Exception as e:
            return ToolResult(success=False, error=f"读取文件失败: {e}")


class WriteFileTool(ToolBase):
    name = "write_file"
    description = (
        "将文本内容写入文件（覆盖写入）。"
        "用于保存脚本、配置、生成结果等。"
        "如果目录不存在会自动创建。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "文件路径（相对路径基于项目根目录）",
            },
            "content": {
                "type": "string",
                "description": "要写入的文本内容",
            },
        },
        "required": ["file_path", "content"],
    }

    async def execute(self, file_path: str, content: str) -> ToolResult:
        try:
            if not os.path.isabs(file_path):
                base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                file_path = os.path.join(base_dir, file_path)

            os.makedirs(os.path.dirname(os.path.abspath(file_path)), exist_ok=True)

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)

            return ToolResult(
                success=True,
                data={"file_path": file_path, "size": len(content)},
            )
        except Exception as e:
            return ToolResult(success=False, error=f"写入文件失败: {e}")


class EditFileTool(ToolBase):
    name = "edit_file"
    description = (
        "编辑文件：将文件中的 old_text 替换为 new_text（只替换第一处匹配）。"
        "用于局部修改文件内容。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "文件路径（相对路径基于项目根目录）",
            },
            "old_text": {
                "type": "string",
                "description": "要被替换的原文本（必须精确匹配）",
            },
            "new_text": {
                "type": "string",
                "description": "替换后的新文本",
            },
        },
        "required": ["file_path", "old_text", "new_text"],
    }

    async def execute(self, file_path: str, old_text: str, new_text: str) -> ToolResult:
        try:
            if not os.path.isabs(file_path):
                base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                file_path = os.path.join(base_dir, file_path)

            if not os.path.exists(file_path):
                return ToolResult(success=False, error=f"文件不存在: {file_path}")

            # 依次尝试多种编码读取（兼容 GBK 等非 UTF-8 文件）
            content = None
            used_encoding = "utf-8"
            for encoding in ReadFileTool._ENCODINGS:
                try:
                    with open(file_path, "r", encoding=encoding) as f:
                        content = f.read()
                    used_encoding = encoding
                    break
                except UnicodeDecodeError:
                    continue
            if content is None:
                return ToolResult(
                    success=False,
                    error=f"无法解码文件（已尝试 {', '.join(ReadFileTool._ENCODINGS)}）: {file_path}",
                )

            if old_text not in content:
                return ToolResult(
                    success=False,
                    error=f"未找到要替换的文本，请检查 old_text 是否精确匹配文件内容",
                )

            new_content = content.replace(old_text, new_text, 1)

            with open(file_path, "w", encoding=used_encoding) as f:
                f.write(new_content)

            return ToolResult(
                success=True,
                data={"file_path": file_path, "replaced": True, "encoding": used_encoding},
            )
        except Exception as e:
            return ToolResult(success=False, error=f"编辑文件失败: {e}")


class BashTool(ToolBase):
    name = "bash"
    description = (
        "执行 shell 命令（如 ffmpeg、ls、mkdir 等），返回 stdout 和 stderr。"
        "用于文件操作、视频处理、系统命令等。"
        "命令在项目根目录下执行，超时时间默认 120 秒。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 shell 命令",
            },
            "timeout": {
                "type": "integer",
                "description": "超时时间（秒），默认 120",
                "default": 120,
            },
        },
        "required": ["command"],
    }

    async def execute(self, command: str, timeout: int = 120) -> ToolResult:
        try:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=base_dir,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                return ToolResult(
                    success=False,
                    error=f"命令执行超时（{timeout}s）: {command}",
                )

            stdout_text = stdout.decode("utf-8", errors="replace").strip()
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            return_code = process.returncode

            return ToolResult(
                success=(return_code == 0),
                data={
                    "stdout": stdout_text,
                    "stderr": stderr_text,
                    "return_code": return_code,
                },
                error=stderr_text if return_code != 0 else None,
            )
        except Exception as e:
            return ToolResult(success=False, error=f"命令执行失败: {e}")
