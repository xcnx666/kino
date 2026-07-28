"""视频库管理：AI 生成内容的资产管理中心。

设计要点：
- JSON 持久化存储，位于 config/library.json
- 每个条目关联 Agent 会话，记录完整生成链路
- 支持按类型、状态、关键词搜索筛选
- 与文件系统扫描互补：DB 记录元数据，文件系统提供实际文件
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from logger import logger

# 配置文件路径
_BASE_DIR = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _BASE_DIR / "config"
_LIBRARY_FILE = _CONFIG_DIR / "library.json"
_OUTPUT_DIR = _BASE_DIR / "output"

# 媒体类型扩展名
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
_VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm")
_AUDIO_EXTS = (".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a")

# 生成链路步骤定义
_CHAIN_STEPS = {
    "user": "用户需求",
    "llm": "剧本生成",
    "generate_image": "图片生成",
    "generate_video": "视频生成",
    "text_to_speech": "音频生成",
    "ffmpeg_compose": "最终合成",
    "download_file": "素材下载",
    "list_materials": "素材查看",
}


def _media_type(filename: str) -> str:
    """根据扩展名判断媒体类型。"""
    ext = Path(filename).suffix.lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _AUDIO_EXTS:
        return "audio"
    return "other"


def _now_iso() -> str:
    """当前时间 ISO 格式。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


class LibraryManager:
    """视频库管理器：单例模式，读写 config/library.json。"""

    _instance: Optional["LibraryManager"] = None

    def __new__(cls) -> "LibraryManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._loaded = False
        return cls._instance

    def __init__(self) -> None:
        if not self._loaded:
            self._items: Dict[str, Dict[str, Any]] = {}
            self._load()
            self._loaded = True

    def _load(self) -> None:
        """从文件加载。"""
        if _LIBRARY_FILE.exists():
            try:
                data = json.loads(_LIBRARY_FILE.read_text(encoding="utf-8"))
                self._items = data.get("items", {})
            except (json.JSONDecodeError, OSError):
                self._items = {}
                self._save()
        else:
            self._items = {}
            self._save()

    def _save(self) -> None:
        """保存到文件。"""
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {"items": self._items}
        _LIBRARY_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def reload(self) -> None:
        """重新从文件加载（热更新）。"""
        self._load()

    # ==================== 增删改查 ====================

    def add_item(
        self,
        session_id: str = "",
        session_title: str = "",
        title: str = "",
        item_type: str = "video",
        file_path: str = "",
        thumbnail: str = "",
        status: str = "completed",
        model: str = "",
        generation_params: Optional[Dict[str, Any]] = None,
        generation_chain: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """添加一个视频库条目。"""
        item_id = f"lib_{uuid.uuid4().hex[:8]}"
        item = {
            "id": item_id,
            "session_id": session_id,
            "session_title": session_title,
            "title": title or file_path.split("/")[-1] or "未命名",
            "type": item_type,
            "file_path": file_path,
            "thumbnail": thumbnail,
            "status": status,
            "created_at": _now_iso(),
            "model": model,
            "generation_params": generation_params or {},
            "generation_chain": generation_chain or [],
        }
        self._items[item_id] = item
        self._save()
        logger.info(f"视频库新增条目: {item_id} ({item_type}) {title}")
        return item

    def get_item(self, item_id: str) -> Optional[Dict[str, Any]]:
        """获取单个条目。"""
        return self._items.get(item_id)

    def get_by_file_path(self, file_path: str) -> Optional[Dict[str, Any]]:
        """根据文件路径查找条目（去重用）。"""
        for item in self._items.values():
            if item.get("file_path") == file_path:
                return item
        return None

    def list_items(
        self,
        item_type: str = "",
        status: str = "",
        q: str = "",
        session_id: str = "",
    ) -> List[Dict[str, Any]]:
        """列出条目，支持筛选和搜索。

        Args:
            item_type: 类型筛选 (video/image/audio)
            status: 状态筛选 (generating/completed/failed)
            q: 关键词搜索（匹配标题）
            session_id: 按会话筛选
        """
        results = list(self._items.values())

        if item_type:
            results = [r for r in results if r.get("type") == item_type]
        if status:
            results = [r for r in results if r.get("status") == status]
        if session_id:
            results = [r for r in results if r.get("session_id") == session_id]
        if q:
            q_lower = q.lower()
            results = [
                r for r in results
                if q_lower in r.get("title", "").lower()
                or q_lower in r.get("session_title", "").lower()
            ]

        # 按创建时间倒序
        results.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return results

    def update_status(self, item_id: str, status: str) -> bool:
        """更新条目状态。"""
        if item_id in self._items:
            self._items[item_id]["status"] = status
            self._save()
            return True
        return False

    def delete_item(self, item_id: str) -> bool:
        """删除条目（仅删除记录，不删文件）。"""
        if item_id in self._items:
            del self._items[item_id]
            self._save()
            return True
        return False

    def delete_by_file_path(self, file_path: str) -> bool:
        """根据文件路径删除条目。"""
        item = self.get_by_file_path(file_path)
        if item:
            return self.delete_item(item["id"])
        return False

    def delete_by_session(self, session_id: str) -> int:
        """删除某会话的所有条目。"""
        to_delete = [
            iid for iid, item in self._items.items()
            if item.get("session_id") == session_id
        ]
        for iid in to_delete:
            del self._items[iid]
        if to_delete:
            self._save()
        return len(to_delete)

    # ==================== 文件系统扫描（补充） ====================

    def scan_filesystem(self) -> List[Dict[str, Any]]:
        """扫描 output/ 目录，返回未入库的文件列表。

        用于将历史生成文件补充入库。
        从路径中解析 session_id（output/<session_id>/<type>/... 结构）。
        """
        existing_paths = {
            item.get("file_path", "")
            for item in self._items.values()
        }

        new_items: List[Dict[str, Any]] = []
        if not _OUTPUT_DIR.exists():
            return new_items

        for root, dirs, files in os.walk(_OUTPUT_DIR):
            # 跳过隐藏目录
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in files:
                if fname.startswith(".") or fname.startswith("_"):
                    continue
                fpath = Path(root) / fname
                mtype = _media_type(fname)
                if mtype == "other":
                    continue
                try:
                    rel = fpath.relative_to(_OUTPUT_DIR)
                    url_path = "/output/" + str(rel).replace("\\", "/")
                except ValueError:
                    continue

                if url_path in existing_paths:
                    continue

                # 从路径中解析 session_id
                # 新结构: output/<session_id>/<type>/<file>
                # 旧结构: output/<type>/<file> 或 output/<file>
                parts = str(rel).replace("\\", "/").split("/")
                session_id = ""
                session_title = ""
                if len(parts) >= 3:
                    # 第一段是 session_id（如 chat_xxxx）
                    potential_sid = parts[0]
                    if potential_sid.startswith("chat_") or potential_sid.startswith("session_"):
                        session_id = potential_sid

                new_items.append({
                    "file_path": url_path,
                    "type": mtype,
                    "title": fname,
                    "size": fpath.stat().st_size if fpath.exists() else 0,
                    "session_id": session_id,
                    "session_title": session_title,
                })

        return new_items

    def import_filesystem_items(self) -> int:
        """将文件系统中未入库的文件批量导入。"""
        new_items = self.scan_filesystem()
        count = 0
        for item in new_items:
            self.add_item(
                title=item["title"],
                item_type=item["type"],
                file_path=item["file_path"],
                status="completed",
                session_id=item.get("session_id", ""),
                session_title=item.get("session_title", ""),
            )
            count += 1
        if count:
            logger.info(f"视频库从文件系统导入 {count} 个条目")
        return count

    def build_generation_chain(
        self,
        messages: List[Dict[str, Any]],
        user_message: str = "",
    ) -> List[Dict[str, Any]]:
        """从对话历史中提取生成链路。

        Args:
            messages: 会话消息历史
            user_message: 用户原始输入
        """
        chain: List[Dict[str, Any]] = []

        # 步骤1：用户需求
        if user_message:
            chain.append({
                "step": "用户需求",
                "tool": "user",
                "content": user_message[:200],
                "status": "done",
                "time": _now_iso(),
            })
        else:
            # 从消息历史中找第一条 user 消息
            for msg in messages:
                if msg.get("role") == "user":
                    chain.append({
                        "step": "用户需求",
                        "tool": "user",
                        "content": str(msg.get("content", ""))[:200],
                        "status": "done",
                        "time": _now_iso(),
                    })
                    break

        # 步骤2：遍历工具调用
        for msg in messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                content = msg.get("content", "")
                if content and len(chain) < 2:
                    # 第一条 assistant 文本作为剧本生成
                    chain.append({
                        "step": "剧本生成",
                        "tool": "llm",
                        "content": str(content)[:200],
                        "status": "done",
                        "time": _now_iso(),
                    })
                for tc in msg["tool_calls"]:
                    tool_name = tc.get("function", {}).get("name", "")
                    step_label = _CHAIN_STEPS.get(tool_name, tool_name)
                    try:
                        args_str = tc.get("function", {}).get("arguments", "{}")
                        args = json.loads(args_str) if isinstance(args_str, str) else args_str
                    except (json.JSONDecodeError, TypeError):
                        args = {}

                    # 提取关键参数摘要
                    summary = ""
                    if "prompt" in args:
                        summary = str(args["prompt"])[:150]
                    elif "text" in args:
                        summary = str(args["text"])[:150]

                    chain.append({
                        "step": step_label,
                        "tool": tool_name,
                        "content": summary,
                        "status": "done",
                        "time": _now_iso(),
                        "params": args,
                    })

            elif msg.get("role") == "tool":
                # 工具结果
                tool_call_id = msg.get("tool_call_id", "")
                content_str = msg.get("content", "")
                try:
                    result_data = json.loads(content_str) if isinstance(content_str, str) else content_str
                except (json.JSONDecodeError, TypeError):
                    result_data = {}

                # 提取输出文件
                output_url = ""
                if isinstance(result_data, dict):
                    data = result_data.get("data", {})
                    if isinstance(data, dict):
                        if data.get("images"):
                            output_url = data["images"][0] if data["images"] else ""
                        elif data.get("video_url"):
                            output_url = data["video_url"]
                        elif data.get("audio_path"):
                            output_url = data["audio_path"]
                        elif data.get("file_path"):
                            output_url = data["file_path"]

                # 更新最后一个同工具步骤的输出
                for step in reversed(chain):
                    if step.get("tool") and not step.get("output"):
                        step["output"] = output_url
                        step["success"] = result_data.get("success", True) if isinstance(result_data, dict) else True
                        break

        return chain

    def all_items(self) -> List[Dict[str, Any]]:
        """返回所有条目（按时间倒序）。"""
        return self.list_items()


# 全局单例
def get_library_manager() -> LibraryManager:
    return LibraryManager()
