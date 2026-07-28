"""任务管理：独立于聊天会话的后台任务跟踪与持久化。

设计要点：
- 每轮用户消息（一次完整的 Agent 执行）对应一个 Task，拥有唯一 Task ID。
- Task 生命周期独立于 WebSocket / 会话切换：切换会话后任务继续后台执行，
  重新进入会话时可恢复任务状态与进度。
- 状态机：pending(等待执行) → running(执行中) → generating(生成中)
  → composing(合成中) → completed(已完成) / failed(失败)
- 素材统一保存层级：用户 → 项目(会话) → Task → 素材，
  即 output/<session_id>/<task_id>/{images,videos,audio,frames}/。
- 每个任务持久化为 tasks/<task_id>.json，服务重启不丢。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from logger import logger

_BASE_DIR = Path(__file__).resolve().parent.parent
_TASKS_DIR = _BASE_DIR / "tasks"

# 任务状态枚举
STATUS_PENDING = "pending"        # 等待执行
STATUS_RUNNING = "running"        # 执行中（规划/提示词阶段）
STATUS_GENERATING = "generating"  # 生成中（媒体生成阶段）
STATUS_COMPOSING = "composing"    # 合成中
STATUS_COMPLETED = "completed"    # 已完成
STATUS_FAILED = "failed"          # 失败

ALL_STATUSES = (
    STATUS_PENDING, STATUS_RUNNING, STATUS_GENERATING,
    STATUS_COMPOSING, STATUS_COMPLETED, STATUS_FAILED,
)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


class TaskManager:
    """任务管理器：单例模式，每个任务独立 JSON 文件持久化。"""

    _instance: Optional["TaskManager"] = None

    def __new__(cls) -> "TaskManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._loaded = False
        return cls._instance

    def __init__(self) -> None:
        if not self._loaded:
            self._tasks: Dict[str, Dict[str, Any]] = {}
            self._load()
            self._loaded = True

    # ==================== 持久化 ====================

    def _load(self) -> None:
        """启动时加载所有任务文件；中断的非终态任务标记为失败。"""
        self._tasks = {}
        if not _TASKS_DIR.exists():
            return
        for fp in sorted(_TASKS_DIR.glob("task_*.json")):
            try:
                task = json.loads(fp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.error(f"加载任务文件失败 {fp}: {e}")
                continue
            # 服务重启时，此前处于进行中状态的任务实际已中断
            if task.get("status") in (
                STATUS_PENDING, STATUS_RUNNING, STATUS_GENERATING, STATUS_COMPOSING,
            ):
                task["status"] = STATUS_FAILED
                task["error"] = "服务重启导致任务中断"
                task["updated_at"] = _now_iso()
                self._save_task(task)
            self._tasks[task["id"]] = task
        if self._tasks:
            logger.info(f"从磁盘加载了 {len(self._tasks)} 个任务")

    def _save_task(self, task: Dict[str, Any]) -> None:
        _TASKS_DIR.mkdir(parents=True, exist_ok=True)
        fp = _TASKS_DIR / f"{task['id']}.json"
        fp.write_text(
            json.dumps(task, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ==================== 增删改查 ====================

    def create(self, session_id: str, session_title: str, user_message: str) -> Dict[str, Any]:
        """创建一个新任务（pending 状态）。"""
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        task = {
            "id": task_id,
            "session_id": session_id,
            "session_title": session_title,
            "user_message": user_message[:500],
            "status": STATUS_PENDING,
            "error": "",
            "assets": [],
            "result_text": "",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        self._tasks[task_id] = task
        self._save_task(task)
        logger.info(f"任务创建: {task_id} (会话 {session_id})")
        return task

    def update_status(self, task_id: str, status: str, error: str = "") -> Optional[Dict[str, Any]]:
        task = self._tasks.get(task_id)
        if not task:
            return None
        if status not in ALL_STATUSES:
            raise ValueError(f"非法任务状态: {status}")
        task["status"] = status
        if error:
            task["error"] = error[:500]
        task["updated_at"] = _now_iso()
        self._save_task(task)
        logger.info(f"任务 {task_id} 状态 → {status}")
        return task

    def add_asset(self, task_id: str, asset: Dict[str, Any]) -> None:
        """登记任务产出的素材（type/url/local_path）。"""
        task = self._tasks.get(task_id)
        if not task:
            return
        # 去重：同 url 不重复登记
        for existing in task["assets"]:
            if existing.get("url") == asset.get("url"):
                existing.update(asset)
                task["updated_at"] = _now_iso()
                self._save_task(task)
                return
        task["assets"].append(asset)
        task["updated_at"] = _now_iso()
        self._save_task(task)

    def set_result(self, task_id: str, result_text: str) -> None:
        task = self._tasks.get(task_id)
        if not task:
            return
        task["result_text"] = (result_text or "")[:2000]
        task["updated_at"] = _now_iso()
        self._save_task(task)

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self._tasks.get(task_id)

    def list(self, session_id: str = "", status: str = "") -> List[Dict[str, Any]]:
        tasks = list(self._tasks.values())
        if session_id:
            tasks = [t for t in tasks if t.get("session_id") == session_id]
        if status:
            tasks = [t for t in tasks if t.get("status") == status]
        tasks.sort(key=lambda t: t.get("created_at", ""), reverse=True)
        return tasks

    def delete(self, task_id: str) -> bool:
        task = self._tasks.pop(task_id, None)
        if not task:
            return False
        fp = _TASKS_DIR / f"{task_id}.json"
        if fp.exists():
            try:
                fp.unlink()
            except OSError:
                pass
        return True

    def delete_by_session(self, session_id: str) -> int:
        ids = [t["id"] for t in self._tasks.values() if t.get("session_id") == session_id]
        for tid in ids:
            self.delete(tid)
        return len(ids)


# 全局单例
def get_task_manager() -> TaskManager:
    return TaskManager()
