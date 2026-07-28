"""FastAPI 后端服务：提供视频生成平台的 REST API。

接口列表：
  POST   /api/upload          上传素材文件
  POST   /api/generate        开始生成视频（异步）
  GET    /api/status/{pid}    查询生成状态
  GET    /api/video/{pid}     下载/播放最终视频
  GET    /api/projects        列出所有项目
  DELETE /api/projects/{pid}  删除项目
  WS     /ws/{pid}            WebSocket 实时进度推送
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import (
    FastAPI,
    File,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    HTTPException,
    BackgroundTasks,
    Request,
)
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from logger import logger
from pipeline.orchestrator import VideoOrchestrator, VideoProject
from media_tools import build_default_registry, build_registry_without_agnes
from core.config_manager import get_manager
from core.library_manager import get_library_manager
from core.skill_manager import get_skill_manager
from core.task_manager import get_task_manager
from web.chat_manager import chat_manager

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "output"
WEB_DIR = Path(__file__).resolve().parent

UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时加载已保存的聊天会话。"""
    try:
        chat_manager.load_from_disk()
    except Exception as e:
        logger.error(f"加载已保存会话失败: {e}")
    yield


app = FastAPI(title="Kino", version="1.0.0", lifespan=lifespan)

# 静态文件（前端）
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
# 生成的文件（音频/视频等）
app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")
# 上传的素材文件（图片/视频/文本，供前端预览和 API 引用）
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

# 全局状态：项目管理
_projects: Dict[str, Dict[str, Any]] = {}
# WebSocket 连接管理
_ws_connections: Dict[str, list] = {}


def _get_registry():
    """获取工具注册中心（根据模型配置动态构建）。"""
    try:
        return build_default_registry()
    except Exception:
        return build_registry_without_agnes()


def _broadcast_status(project_id: str, status: str, message: str = "", data: Any = None):
    """向所有订阅该项目的 WebSocket 客户端推送状态。"""
    if project_id not in _ws_connections:
        return
    msg = json.dumps({
        "project_id": project_id,
        "status": status,
        "message": message,
        "data": data,
    }, ensure_ascii=False)
    for ws in _ws_connections.get(project_id, []):
        try:
            asyncio.create_task(ws.send_text(msg))
        except Exception:
            pass


# ==================== 页面路由 ====================

@app.get("/", response_class=HTMLResponse)
async def index():
    """返回主页面。"""
    html_path = WEB_DIR / "static" / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


# ==================== API 路由 ====================

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    """上传素材文件。"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    # 按扩展名分类
    ext = Path(file.filename).suffix.lower()
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
        category = "images"
    elif ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
        category = "videos"
    elif ext in (".txt", ".md", ".json"):
        category = "text"
    else:
        category = "other"

    save_dir = UPLOAD_DIR / category
    save_dir.mkdir(parents=True, exist_ok=True)
    save_path = save_dir / file.filename

    content = await file.read()
    save_path.write_bytes(content)

    logger.info(f"文件上传: {file.filename} -> {save_path} ({len(content)} bytes)")

    return {
        "success": True,
        "filename": file.filename,
        "category": category,
        "path": str(save_path),
        "size": len(content),
    }


@app.post("/api/generate")
async def generate_video(
    background_tasks: BackgroundTasks,
    creative_request: str = "",
    project_name: str = "",
    mode: str = "demo",
):
    """开始生成视频。

    mode:
      - demo: 演示模式，不需要 API key
      - full: 完整模式，需要 LLM + Agnes API key
    """
    project_id = project_name or f"project_{uuid.uuid4().hex[:8]}"

    _projects[project_id] = {
        "id": project_id,
        "creative_request": creative_request,
        "mode": mode,
        "status": "pending",
        "message": "任务已创建，等待执行",
        "final_path": None,
        "created_at": asyncio.get_event_loop().time(),
    }

    # 后台异步执行
    background_tasks.add_task(_run_generation, project_id, creative_request, mode)

    return {"success": True, "project_id": project_id, "status": "pending"}


async def _run_generation(project_id: str, creative_request: str, mode: str):
    """后台执行视频生成。"""
    project_info = _projects.get(project_id, {})
    project_info["status"] = "running"
    project_info["message"] = "正在生成视频..."
    _broadcast_status(project_id, "running", "正在生成视频...")

    try:
        registry = _get_registry()
        orchestrator = VideoOrchestrator(registry)

        if mode == "demo":
            project = await orchestrator.demo_run(project_name=project_id)
        else:
            project = await orchestrator.run(
                creative_request=creative_request,
                project_name=project_id,
            )

        if project.final_path and os.path.exists(project.final_path):
            project_info["status"] = "completed"
            project_info["message"] = "视频生成完成"
            project_info["final_path"] = project.final_path
            project_info["shots_count"] = len(project.shots)
            _broadcast_status(project_id, "completed", "视频生成完成", {
                "final_path": project.final_path,
                "shots_count": len(project.shots),
            })
        else:
            project_info["status"] = "failed"
            project_info["message"] = "视频生成失败：未输出文件"
            _broadcast_status(project_id, "failed", "视频生成失败：未输出文件")

    except Exception as e:
        logger.error(f"项目 {project_id} 生成失败: {e}")
        project_info["status"] = "failed"
        project_info["message"] = f"生成失败: {str(e)}"
        _broadcast_status(project_id, "failed", f"生成失败: {str(e)}")


@app.get("/api/status/{project_id}")
async def get_status(project_id: str):
    """查询项目生成状态。"""
    if project_id not in _projects:
        raise HTTPException(status_code=404, detail="项目不存在")
    return _projects[project_id]


@app.get("/api/video/{project_id}")
async def get_video(project_id: str):
    """获取最终视频文件。"""
    if project_id not in _projects:
        raise HTTPException(status_code=404, detail="项目不存在")

    info = _projects[project_id]
    if info.get("status") != "completed" or not info.get("final_path"):
        raise HTTPException(status_code=400, detail="视频尚未生成完成")

    video_path = info["final_path"]
    if not os.path.exists(video_path):
        raise HTTPException(status_code=404, detail="视频文件不存在")

    return FileResponse(
        path=video_path,
        media_type="video/mp4",
        filename=f"{project_id}.mp4",
    )


@app.get("/api/projects")
async def list_projects():
    """列出所有项目。"""
    return {"projects": list(_projects.values())}


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):
    """删除项目。"""
    if project_id not in _projects:
        raise HTTPException(status_code=404, detail="项目不存在")

    info = _projects.pop(project_id)

    # 清理文件
    output_path = OUTPUT_DIR / project_id
    if output_path.exists():
        shutil.rmtree(output_path, ignore_errors=True)

    return {"success": True, "message": f"项目 {project_id} 已删除"}


@app.get("/api/uploads")
async def list_uploads():
    """列出已上传的素材。"""
    result = {"images": [], "videos": [], "text": [], "other": []}
    for category in result:
        cat_dir = UPLOAD_DIR / category
        if cat_dir.exists():
            for f in cat_dir.iterdir():
                if f.is_file():
                    result[category].append({
                        "name": f.name,
                        "size": f.stat().st_size,
                        "path": str(f),
                    })
    return result


# ==================== 视频库 API ====================

# 媒体类型扩展名映射（用于文件系统扫描补充）
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
_VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm")
_AUDIO_EXTS = (".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a")
_CATEGORY_DIRS = ("images", "videos", "audio", "frames")


def _media_type(filename: str) -> str:
    """根据扩展名判断媒体类型：image / video / audio / other。"""
    ext = Path(filename).suffix.lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _AUDIO_EXTS:
        return "audio"
    return "other"


@app.get("/api/library")
async def get_library(
    type: str = "",
    status: str = "",
    q: str = "",
    session_id: str = "",
    import_fs: bool = False,
):
    """返回视频库中所有生成内容。

    支持搜索和筛选：
    - type: 类型筛选 (video/image/audio)
    - status: 状态筛选 (generating/completed/failed)
    - q: 关键词搜索（匹配标题）
    - session_id: 按会话筛选
    - import_fs: 是否从文件系统导入未入库的文件

    返回结构：
      {
        "items": [ { id, session_id, session_title, title, type, file_path, thumbnail, status, created_at, model, ... } ],
        "groups": [ { session_id, session_title, items: [...] } ]  // 按会话分组
        "total": int
      }
    """
    lib = get_library_manager()
    lib.reload()

    # 可选：从文件系统导入未入库的文件
    if import_fs:
        lib.import_filesystem_items()

    items = lib.list_items(item_type=type, status=status, q=q, session_id=session_id)

    # 按会话分组
    groups_map: Dict[str, Dict[str, Any]] = {}
    for item in items:
        sid = item.get("session_id") or "_ungrouped"
        if sid not in groups_map:
            groups_map[sid] = {
                "session_id": sid,
                "session_title": item.get("session_title") or "未分组",
                "items": [],
            }
        groups_map[sid]["items"].append(item)

    groups = list(groups_map.values())
    # 未分组放最后
    groups.sort(key=lambda g: (g["session_id"] == "_ungrouped", g["session_id"]))

    return {
        "items": items,
        "groups": groups,
        "total": len(items),
    }


@app.get("/api/library/{item_id}")
async def get_library_item(item_id: str):
    """获取单个视频库条目的详细信息（含生成链路）。"""
    lib = get_library_manager()
    lib.reload()
    item = lib.get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")
    return item


@app.delete("/api/library")
async def delete_library_file(request: Request):
    """删除视频库中的文件（支持单个或批量删除）。

    Body:
        {"path": "/output/xxx/yyy.mp4"}           单个删除
        {"paths": ["/output/xxx/a.mp4", ...]}     批量删除
        {"item_ids": ["lib_xxx", ...]}            按条目ID删除

    返回:
        {"success": true, "deleted": [...], "failed": [...]}
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON")

    lib = get_library_manager()
    lib.reload()

    deleted: List[str] = []
    failed: List[Dict[str, str]] = []

    # 按条目 ID 删除
    item_ids = body.get("item_ids", [])
    if item_ids and isinstance(item_ids, list):
        for iid in item_ids:
            item = lib.get_item(iid)
            if not item:
                failed.append({"path": iid, "error": "条目不存在"})
                continue
            # 删除物理文件
            url_path = item.get("file_path", "")
            if url_path and url_path.startswith("/output/"):
                rel = url_path[len("/output/"):]
                file_path = (OUTPUT_DIR / rel).resolve()
                output_resolved = OUTPUT_DIR.resolve()
                try:
                    file_path.relative_to(output_resolved)
                    if file_path.exists():
                        if file_path.is_file():
                            file_path.unlink()
                        elif file_path.is_dir():
                            shutil.rmtree(file_path, ignore_errors=True)
                except (ValueError, OSError):
                    pass
            # 删除库记录
            lib.delete_item(iid)
            deleted.append(url_path or iid)
        # 清理空目录
        _cleanup_empty_dirs()
        return {"success": True, "deleted": deleted, "failed": failed}

    # 按文件路径删除（兼容旧接口）
    raw_paths = body.get("paths")
    if raw_paths and isinstance(raw_paths, list):
        url_paths = raw_paths
    else:
        single = body.get("path")
        url_paths = [single] if single else []

    if not url_paths:
        raise HTTPException(status_code=400, detail="未提供文件路径")

    output_resolved = OUTPUT_DIR.resolve()

    for url_path in url_paths:
        if not url_path or not isinstance(url_path, str):
            continue
        rel = url_path.lstrip("/")
        if rel.startswith("output/"):
            rel = rel[len("output/"):]
        file_path = (OUTPUT_DIR / rel).resolve()

        try:
            file_path.relative_to(output_resolved)
        except ValueError:
            failed.append({"path": url_path, "error": "非法路径"})
            continue

        if not file_path.exists():
            # 文件可能已删，但仍清理库记录
            lib.delete_by_file_path(url_path)
            failed.append({"path": url_path, "error": "文件不存在"})
            continue

        try:
            if file_path.is_file():
                file_path.unlink()
                deleted.append(url_path)
            elif file_path.is_dir():
                shutil.rmtree(file_path, ignore_errors=True)
                deleted.append(url_path)
            # 同步删除库记录
            lib.delete_by_file_path(url_path)
        except Exception as e:
            failed.append({"path": url_path, "error": str(e)})

    _cleanup_empty_dirs()
    logger.info(f"视频库删除: 成功 {len(deleted)} 个, 失败 {len(failed)} 个")
    return {"success": True, "deleted": deleted, "failed": failed}


def _cleanup_empty_dirs() -> None:
    """清理 output/ 下的空目录。"""
    for entry in sorted(OUTPUT_DIR.rglob("*"), reverse=True):
        if entry.is_dir() and not any(entry.iterdir()):
            try:
                entry.rmdir()
            except OSError:
                pass


# ==================== WebSocket ====================

@app.websocket("/ws/{project_id}")
async def websocket_endpoint(websocket: WebSocket, project_id: str):
    """WebSocket 实时进度推送。"""
    await websocket.accept()

    if project_id not in _ws_connections:
        _ws_connections[project_id] = []
    _ws_connections[project_id].append(websocket)

    # 立即推送当前状态
    if project_id in _projects:
        info = _projects[project_id]
        await websocket.send_text(json.dumps({
            "project_id": project_id,
            "status": info.get("status", "unknown"),
            "message": info.get("message", ""),
        }, ensure_ascii=False))

    try:
        while True:
            # 保持连接，等待客户端消息（心跳）
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        if project_id in _ws_connections:
            _ws_connections[project_id].remove(websocket)


# ==================== 模型配置管理 ====================

@app.get("/api/models")
async def get_models():
    """获取全部模型配置（api_key 打码）。"""
    manager = get_manager()
    return manager.get_all(mask=True)


@app.get("/api/models/providers")
async def get_providers():
    """获取各类模型支持的 provider 列表。"""
    manager = get_manager()
    return manager.get_providers()


@app.post("/api/models/{category}")
async def add_model(category: str, request: Request):
    """添加一个模型配置。"""
    body = await request.json()
    manager = get_manager()
    try:
        result = manager.add(category, body)
        return {"success": True, "config": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/models/{category}/{config_id}")
async def update_model(category: str, config_id: str, request: Request):
    """更新模型配置（api_key 留空则不修改）。"""
    body = await request.json()
    manager = get_manager()
    try:
        result = manager.update(category, config_id, body)
        if result is None:
            raise HTTPException(status_code=404, detail="配置不存在")
        return {"success": True, "config": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/models/{category}/{config_id}")
async def delete_model(category: str, config_id: str):
    """删除模型配置。"""
    manager = get_manager()
    try:
        manager.delete(category, config_id)
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/models/{category}/{config_id}/activate")
async def activate_model(category: str, config_id: str):
    """激活某个模型配置。"""
    manager = get_manager()
    try:
        success = manager.activate(category, config_id)
        if not success:
            raise HTTPException(status_code=404, detail="配置不存在")
        return {"success": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ==================== 聊天会话 API ====================

@app.post("/api/chat/sessions")
async def create_chat_session(request: Request):
    """创建新的聊天会话。"""
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    title = body.get("title", "")
    session = chat_manager.create_session(title)
    return {"success": True, "session": session.to_dict()}


@app.get("/api/chat/sessions")
async def list_chat_sessions():
    """列出所有聊天会话。"""
    return {"success": True, "sessions": chat_manager.list_sessions()}


@app.get("/api/chat/sessions/{session_id}")
async def get_chat_session(session_id: str):
    """获取某个聊天会话详情（含历史消息）。"""
    session = chat_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"success": True, "session": session.to_dict()}


@app.delete("/api/chat/sessions/{session_id}")
async def delete_chat_session(session_id: str):
    """删除聊天会话。"""
    if chat_manager.delete_session(session_id):
        # 同时清理会话素材目录
        session_upload = UPLOAD_DIR / session_id
        if session_upload.exists():
            shutil.rmtree(session_upload, ignore_errors=True)
        # 清理会话生成内容目录 output/<session_id>/
        session_output = OUTPUT_DIR / session_id
        if session_output.exists():
            shutil.rmtree(session_output, ignore_errors=True)
            logger.info(f"已清理会话生成内容目录: {session_output}")
        # 清理视频库中该会话的条目
        lib = get_library_manager()
        deleted_count = lib.delete_by_session(session_id)
        if deleted_count:
            logger.info(f"已清理视频库中 {deleted_count} 条会话 {session_id} 的记录")
        # 清理该会话的任务记录
        deleted_tasks = get_task_manager().delete_by_session(session_id)
        if deleted_tasks:
            logger.info(f"已清理会话 {session_id} 的 {deleted_tasks} 条任务记录")
        return {"success": True}
    raise HTTPException(status_code=404, detail="会话不存在")


# ==================== 会话级素材管理 ====================

def _classify_file(filename: str) -> str:
    """按扩展名分类文件。"""
    ext = Path(filename).suffix.lower()
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
        return "images"
    elif ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
        return "videos"
    elif ext in (".txt", ".md", ".json"):
        return "text"
    else:
        return "other"


@app.post("/api/chat/sessions/{session_id}/materials")
async def upload_session_material(session_id: str, file: UploadFile = File(...)):
    """上传素材到指定会话的素材库。"""
    session = chat_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    category = _classify_file(file.filename)
    materials_dir = session.get_materials_dir()
    save_dir = materials_dir / category
    save_dir.mkdir(parents=True, exist_ok=True)
    save_path = save_dir / file.filename

    content = await file.read()
    save_path.write_bytes(content)

    # 刷新系统提示词中的素材信息
    session.refresh_materials_prompt()

    logger.info(f"会话 {session_id} 素材上传: {file.filename} -> {save_path} ({len(content)} bytes)")

    return {
        "success": True,
        "filename": file.filename,
        "category": category,
        "path": str(save_path),
        "size": len(content),
        "session_id": session_id,
    }


@app.get("/api/chat/sessions/{session_id}/materials")
async def list_session_materials(session_id: str, category: str = ""):
    """列出指定会话素材库中的素材。"""
    session = chat_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    materials_dir = session.get_materials_dir()
    categories = [category] if category else ["images", "videos", "text", "other"]
    result = {}

    for cat in categories:
        cat_dir = materials_dir / cat
        files = []
        if cat_dir.exists():
            for f in cat_dir.iterdir():
                if f.is_file():
                    files.append({
                        "name": f.name,
                        "size": f.stat().st_size,
                        "path": str(f),
                        "category": cat,
                    })
        result[cat] = files

    return {
        "success": True,
        "session_id": session_id,
        "materials": result,
        "total": sum(len(v) for v in result.values()),
    }


@app.delete("/api/chat/sessions/{session_id}/materials/{category}/{filename}")
async def delete_session_material(session_id: str, category: str, filename: str):
    """删除指定会话素材库中的某个素材。"""
    session = chat_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    materials_dir = session.get_materials_dir()
    file_path = materials_dir / category / filename

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="素材不存在")

    file_path.unlink()
    session.refresh_materials_prompt()

    logger.info(f"会话 {session_id} 素材删除: {category}/{filename}")
    return {"success": True, "message": f"素材 {filename} 已删除"}


@app.get("/api/chat/sessions/{session_id}/materials/preview/{category}/{filename}")
async def preview_session_material(session_id: str, category: str, filename: str):
    """预览/下载素材文件。"""
    session = chat_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    materials_dir = session.get_materials_dir()
    file_path = materials_dir / category / filename

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="素材不存在")

    # 根据扩展名设置 media_type
    ext = Path(filename).suffix.lower()
    media_types = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
        ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
        ".txt": "text/plain", ".md": "text/plain", ".json": "application/json",
    }
    media_type = media_types.get(ext, "application/octet-stream")

    return FileResponse(path=str(file_path), media_type=media_type)


@app.websocket("/ws/chat/{session_id}")
async def chat_websocket(websocket: WebSocket, session_id: str):
    """聊天 WebSocket：接收用户消息，流式推送 Agent 响应。

    设计要点：
    - Agent 任务独立于连接运行：断开连接任务不中断，输出事件缓存在服务端运行状态中；
    - 连接建立或收到 {"type":"sync"} 时，重放进行中任务的状态（断线恢复）；
    - {"type":"stop"} 中断当前生成；运行中收到新消息则排队，由会话链式处理。
    """
    await websocket.accept()

    session = chat_manager.get_session(session_id)
    if not session:
        await websocket.send_json({"type": "error", "message": "会话不存在"})
        await websocket.close()
        return

    # 绑定/重绑当前连接（进行中任务的后续输出将转发到此连接）
    session.ws = websocket

    # 重放进行中的任务状态（断线恢复）
    replay = session.get_replay_payload()
    if replay:
        try:
            await websocket.send_json(replay)
        except Exception:
            pass

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "无效的消息格式"})
                continue

            msg_type = msg.get("type")
            if msg_type == "stop":
                session._interrupted = True
                logger.info(f"会话 {session_id} 收到停止信号，正在中断...")
                continue
            if msg_type == "sync":
                payload = session.get_replay_payload()
                if payload:
                    await websocket.send_json(payload)
                continue

            user_message = (msg.get("content") or "").strip()
            if not user_message:
                continue

            # 推送用户消息回显
            await websocket.send_json({"type": "user_echo", "content": user_message})

            if session._running:
                # Agent 运行中：消息排队，由会话在当前回复后链式处理
                session._queued_messages.append(user_message)
                await websocket.send_json({
                    "type": "info",
                    "message": "消息已加入队列，将在当前回复后处理",
                })
            else:
                # 启动 Agent 循环（独立于连接运行，断开不中断）
                session._agent_task = asyncio.create_task(
                    session.send_message(user_message, websocket)
                )

    except WebSocketDisconnect:
        logger.info(f"聊天 WebSocket 断开: {session_id}（任务继续后台运行）")
    except Exception as e:
        logger.error(f"聊天 WebSocket 异常: {e}")
    finally:
        # 仅解绑连接，不中断任务
        if session.ws is websocket:
            session.ws = None


# ==================== Skill 技能管理 ====================

@app.get("/api/skills")
async def list_skills(enabled_only: bool = False):
    """列出所有 Skill（含内置与自定义）。"""
    sm = get_skill_manager()
    sm.reload()
    return {"success": True, "skills": sm.list_skills(enabled_only=enabled_only)}


@app.get("/api/skills/available-tools")
async def skill_available_tools():
    """返回可供 Skill 声明使用的基础工具清单（排除 skill_ 前缀的动态工具）。"""
    try:
        registry = _get_registry()
        names = [n for n in registry.names() if not n.startswith("skill_")]
    except Exception:
        names = []
    return {"success": True, "tools": sorted(names)}


@app.post("/api/skills/import")
async def import_skill_package(file: UploadFile = File(...)):
    """上传 zip 压缩包导入 Skill。

    支持 skill.json 和 SKILL.md 两种格式。
    """
    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="请上传 .zip 格式的压缩包")

    contents = await file.read()
    sm = get_skill_manager()
    try:
        skill = sm.import_package(contents)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Skill 包导入失败: {e}")
        raise HTTPException(status_code=500, detail=f"导入失败: {e}")

    chat_manager.refresh_all_registries()
    return {"success": True, "skill": skill}


@app.post("/api/skills/import-url")
async def import_skill_from_url(request: Request):
    """从 GitHub URL 导入 Skill。

    Body: {"url": "https://github.com/user/repo"}
    """
    body = await request.json()
    url = body.get("url", "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="请提供 GitHub 仓库 URL")

    sm = get_skill_manager()
    try:
        skill = await asyncio.to_thread(sm.import_from_github, url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Skill GitHub 导入失败: {e}")
        raise HTTPException(status_code=500, detail=f"导入失败: {e}")

    chat_manager.refresh_all_registries()
    return {"success": True, "skill": skill}


@app.post("/api/skills")
async def create_skill(request: Request):
    """创建 Skill。"""
    body = await request.json()
    sm = get_skill_manager()
    try:
        skill = sm.add(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    chat_manager.refresh_all_registries()
    return {"success": True, "skill": skill}


@app.put("/api/skills/{skill_id}")
async def update_skill(skill_id: str, request: Request):
    """更新 Skill。"""
    body = await request.json()
    sm = get_skill_manager()
    try:
        skill = sm.update(skill_id, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not skill:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    chat_manager.refresh_all_registries()
    return {"success": True, "skill": skill}


@app.delete("/api/skills/{skill_id}")
async def delete_skill(skill_id: str):
    """删除 Skill（内置技能不可删除，可禁用）。"""
    sm = get_skill_manager()
    try:
        ok = sm.delete(skill_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    chat_manager.refresh_all_registries()
    return {"success": True}


@app.post("/api/skills/{skill_id}/toggle")
async def toggle_skill(skill_id: str, request: Request):
    """启用/禁用 Skill。"""
    body = await request.json()
    enabled = bool(body.get("enabled", True))
    sm = get_skill_manager()
    skill = sm.toggle(skill_id, enabled)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    chat_manager.refresh_all_registries()
    return {"success": True, "skill": skill}


# ==================== 任务系统 ====================

@app.get("/api/tasks")
async def list_tasks(session_id: str = "", status: str = ""):
    """列出任务（可按会话/状态筛选）。"""
    tm = get_task_manager()
    return {"success": True, "tasks": tm.list(session_id=session_id, status=status)}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    """获取任务详情（含素材列表）。"""
    task = get_task_manager().get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"success": True, "task": task}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    """删除任务记录（不删除素材文件）。"""
    ok = get_task_manager().delete(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"success": True}


# ==================== 健康检查 ====================

@app.get("/api/health")
async def health_check():
    """健康检查。"""
    return {
        "status": "ok",
        "service": "Kino",
        "version": "1.0.0",
    }
