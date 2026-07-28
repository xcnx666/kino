"""媒体工具注册：根据模型配置动态构建 ToolRegistry。

从 config/models.json 读取每类模型的激活配置，动态创建对应工具。
未配置某类模型时跳过注册，不影响其他工具。
"""
from __future__ import annotations

from core.tools.base import ToolRegistry
from core.config_manager import get_manager
from core.skill_manager import get_skill_manager
from logger import logger
from .image_gen import ImageGenTool
from .video_gen import VideoGenTool
from .tts import TTSTool
from .download import DownloadTool
from .ffmpeg_compose import FFmpegComposeTool
from .materials import ListMaterialsTool
from .extract_frame import ExtractLastFrameTool
from .file_ops import ReadFileTool, WriteFileTool, EditFileTool, BashTool


def _register_common_tools(registry: ToolRegistry, session_id: str = "") -> None:
    """注册通用工具（文件操作、下载、合成、尾帧提取等），与模型配置无关。"""
    registry.register(DownloadTool(session_id=session_id))
    registry.register(FFmpegComposeTool(session_id=session_id))
    registry.register(ExtractLastFrameTool(session_id=session_id))
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(EditFileTool())
    registry.register(BashTool())


def _register_skills(registry: ToolRegistry) -> None:
    """将所有启用的 Skill 注册为动态工具（skill_<name>）。

    Skill 不绑定具体模型，执行时使用当前激活的 LLM 配置；
    注册失败（如重名）只跳过该 Skill，不影响其他工具。
    """
    try:
        for tool in get_skill_manager().build_skill_tools():
            try:
                registry.register(tool)
            except ValueError as e:
                logger.warning(f"Skill 工具注册跳过: {e}")
    except Exception as e:
        logger.error(f"Skill 工具注册失败: {e}")


def build_default_registry() -> ToolRegistry:
    """根据配置动态构造工具注册中心。"""
    manager = get_manager()
    manager.reload()  # 热更新：每次构建都重新读取配置文件
    registry = ToolRegistry()

    # 文生图
    image_config = manager.get_active("image")
    if image_config:
        registry.register(ImageGenTool(config=image_config))

    # 文生视频
    video_config = manager.get_active("video")
    if video_config:
        registry.register(VideoGenTool(config=video_config))

    # TTS
    tts_config = manager.get_active("tts")
    if tts_config:
        registry.register(TTSTool(config=tts_config))
    else:
        # 未配置 TTS 时用免费 edge_tts 兜底
        registry.register(TTSTool())

    # 通用工具（始终注册）
    _register_common_tools(registry)

    # Skill 技能工具（始终注册，模型无关）
    _register_skills(registry)

    return registry


def build_registry_for_session(session_id: str) -> ToolRegistry:
    """为指定会话构造工具注册中心，注入会话级素材库工具。

    与 build_default_registry 的区别：
    - 额外注册 ListMaterialsTool，绑定该会话的素材目录
    - Agent 可调用 list_materials 查看用户上传的素材
    """
    manager = get_manager()
    manager.reload()
    registry = ToolRegistry()

    # 会话级素材库工具（始终注册）
    materials_tool = ListMaterialsTool(session_id=session_id)
    materials_tool.ensure_dirs()
    registry.register(materials_tool)

    # 文生图
    image_config = manager.get_active("image")
    if image_config:
        registry.register(ImageGenTool(config=image_config, session_id=session_id))

    # 文生视频
    video_config = manager.get_active("video")
    if video_config:
        registry.register(VideoGenTool(config=video_config))

    # TTS
    tts_config = manager.get_active("tts")
    if tts_config:
        registry.register(TTSTool(config=tts_config, session_id=session_id))
    else:
        # 未配置 TTS 时用免费 edge_tts 兜底
        registry.register(TTSTool(session_id=session_id))

    # 通用工具
    _register_common_tools(registry, session_id=session_id)

    # Skill 技能工具
    _register_skills(registry)

    return registry


def build_registry_without_agnes() -> ToolRegistry:
    """构造不含图片/视频生成工具的注册中心（TTS / 下载 / FFmpeg / 文件操作）。

    在未配置图片/视频模型时使用，方便测试合成流程。
    """
    manager = get_manager()
    manager.reload()
    registry = ToolRegistry()

    tts_config = manager.get_active("tts")
    registry.register(TTSTool(config=tts_config) if tts_config else TTSTool())
    _register_common_tools(registry)
    _register_skills(registry)

    return registry
