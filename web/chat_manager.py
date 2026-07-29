"""聊天会话管理：封装 Agent 循环 + WebSocket 流式推送。

每个 ChatSession 维护独立的对话历史和素材库目录，从配置管理器动态加载 LLM 和工具。
- 每个会话创建独立素材目录 uploads/<session_id>/{images,videos,text,other}
- Agent 工具注册中心包含 list_materials 工具，可主动查看素材
- 系统提示词注入素材库摘要，引导 Agent 生成视频前先查看素材
- 用户消息通过 WebSocket 发送，Agent 响应通过 WebSocket 推送。
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import WebSocket
from logger import logger
from llm import LLM, AnthropicLLM
from core.config_manager import get_manager
from core.library_manager import get_library_manager
from core.task_manager import (
    get_task_manager,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_GENERATING,
    STATUS_COMPOSING,
    STATUS_COMPLETED,
    STATUS_FAILED,
)
from media_tools import build_registry_for_session, ListMaterialsTool
from core.skill_manager import get_skill_manager, BUILTIN_SKILLS

# 触发「生成中」状态的工具
_MEDIA_GEN_TOOLS = {"generate_image", "generate_video", "text_to_speech", "download_file", "extract_last_frame"}
# 触发「合成中」状态的工具
_COMPOSE_TOOLS = {"ffmpeg_compose"}

# 项目根目录
_BASE_DIR = Path(__file__).resolve().parent.parent
# 会话持久化目录：每个 session 保存为 sessions/<session_id>.json
SESSIONS_DIR = _BASE_DIR / "sessions"


class ChatSession:
    """单个聊天会话：管理消息历史、LLM、工具、素材库。"""

    def __init__(self, session_id: str, title: str = "新对话", manager: "ChatManager" = None) -> None:
        self.id = session_id
        self.title = title
        self.messages: List[Dict[str, Any]] = []
        self.registry = None
        self.llm: Optional[LLM] = None
        self.ws: Optional[WebSocket] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.materials_tool: Optional[ListMaterialsTool] = None
        self._interrupted: bool = False
        self._queued_messages: List[str] = []
        # 任务系统与断线恢复状态
        self._running: bool = False
        self._agent_task: Optional[asyncio.Task] = None
        self._current_task_id: str = ""
        self._run_state: Dict[str, Any] = self._fresh_run_state()
        # 持有管理器引用，便于消息交互后自动持久化
        self._manager: Optional["ChatManager"] = manager
        self._init_components()

    def _init_components(self) -> None:
        """从配置管理器初始化 LLM 和工具，根据 provider 选择客户端。"""
        manager = get_manager()

        llm_config = manager.get_active("llm")
        if llm_config:
            provider = llm_config.get("provider", "openai_compatible")
            if provider == "anthropic":
                self.llm = AnthropicLLM(
                    api_key=llm_config["api_key"],
                    base_url=llm_config.get("base_url", ""),
                    model=llm_config["model"],
                )
            else:
                self.llm = LLM(
                    api_key=llm_config["api_key"],
                    base_url=llm_config["base_url"],
                    model=llm_config["model"],
                )

        # 使用会话级工具注册中心（包含 list_materials 工具）
        self.registry = build_registry_for_session(self.id)
        # 获取素材工具实例（用于读取素材摘要）
        self.materials_tool = self.registry.get("list_materials")

        # 构建系统提示词（含素材库信息）
        prompt_path = Path(__file__).resolve().parent.parent / "prompt" / "alpha.md"
        base_prompt = prompt_path.read_text(encoding="utf-8")
        system_prompt = self._build_system_prompt(base_prompt)
        self.messages.append({"role": "system", "content": system_prompt})

    def _build_system_prompt(self, base_prompt: str) -> str:
        """构建系统提示词，注入素材库信息和内置技能知识。"""
        materials_summary = "暂无素材"
        if self.materials_tool:
            materials_summary = self.materials_tool.get_summary()

        # 将内置 Skill 的 prompt 合并到系统提示词（作为模型内在能力）
        builtin_skills_section = ""
        for skill in BUILTIN_SKILLS:
            prompt_text = skill.get("prompt", "")
            if prompt_text:
                builtin_skills_section += f"\n\n### {skill.get('title', skill.get('name', ''))}\n{prompt_text}"

        if builtin_skills_section:
            builtin_skills_section = f"\n\n---\n## 内置能力（系统技能）\n以下是你内置的专业能力，可直接运用，无需调用工具：{builtin_skills_section}"

        materials_info = f"""
---
## 当前会话素材库

会话ID: {self.id}
素材目录: uploads/{self.id}/

### 素材内容预览
{materials_summary}

### 素材使用规则
- 生成视频前，必须先调用 `list_materials` 工具查看完整素材信息
- 如果素材库中有文本素材，视频内容必须基于文本内容创作，不能脱离素材主题
- 如果素材库中有图片素材，参考其风格和色调来编写图片生成提示词
- 如果素材库为空，可以根据用户需求自由创作
- 服务器地址: http://localhost:8000 （素材可通过 http://localhost:8000/uploads/{self.id}/... 访问）
"""
        return base_prompt + builtin_skills_section + materials_info

    def refresh_materials_prompt(self) -> None:
        """刷新系统提示词中的素材信息（上传新素材后调用）。"""
        if not self.messages or self.messages[0]["role"] != "system":
            return
        prompt_path = Path(__file__).resolve().parent.parent / "prompt" / "alpha.md"
        base_prompt = prompt_path.read_text(encoding="utf-8")
        self.messages[0]["content"] = self._build_system_prompt(base_prompt)
        logger.info(f"会话 {self.id} 系统提示词已刷新（素材更新）")

    def get_materials_dir(self) -> Path:
        """获取会话素材目录。"""
        if self.materials_tool:
            return self.materials_tool.get_materials_dir()
        return Path("uploads") / self.id

    # ==================== 任务系统 / 断线恢复 ====================

    @staticmethod
    def _fresh_run_state() -> Dict[str, Any]:
        """新一轮 Agent 运行的状态快照（用于断线重放）。"""
        return {"content": "", "tool_events": [], "media_items": [], "task": None}

    def refresh_registry(self) -> None:
        """重建工具注册中心（Skill / 模型配置变更后热更新）。"""
        self.registry = build_registry_for_session(self.id)
        self.materials_tool = self.registry.get("list_materials")
        self._apply_task_id_to_tools()

    def _apply_task_id_to_tools(self) -> None:
        """将当前任务 ID 传播给所有支持 set_task_id 的工具（素材按任务归档）。"""
        if not self.registry:
            return
        for tool in self.registry.all():
            setter = getattr(tool, "set_task_id", None)
            if callable(setter):
                setter(self._current_task_id)

    async def _emit(self, event: Dict[str, Any]) -> None:
        """推送事件到当前 WebSocket 连接，同时记录到运行状态供断线重放。

        连接断开时事件仅更新运行状态，任务继续后台执行。
        """
        etype = event.get("type")
        # 维护运行状态快照
        if etype == "token":
            self._run_state["content"] += event.get("content", "")
        elif etype == "tool_call":
            self._run_state["tool_events"].append({
                "name": event.get("name", ""),
                "arguments": event.get("arguments", {}),
                "success": None,
            })
        elif etype == "tool_result":
            for te in reversed(self._run_state["tool_events"]):
                if te.get("name") == event.get("name") and te.get("success") is None:
                    te["success"] = event.get("success")
                    err = (event.get("data") or {}).get("error", "")
                    if err:
                        te["error"] = err
                    break
        elif etype == "media":
            self._run_state["media_items"].extend(event.get("items", []))
        elif etype == "task_status":
            self._run_state["task"] = event.get("task")
        # 推送到当前连接（断开则跳过，任务不受影哪）
        ws = self.ws
        if ws is not None:
            try:
                await ws.send_json(event)
            except Exception:
                self.ws = None

    async def _set_task_status(self, status: str, error: str = "") -> None:
        """更新当前任务状态并推送。非终态只向前推进，不回退。"""
        if not self._current_task_id:
            return
        tm = get_task_manager()
        current = tm.get(self._current_task_id)
        if not current:
            return
        # 终态不可变；非终态不允许从 composing 回退到 generating 等
        if current.get("status") in (STATUS_COMPLETED, STATUS_FAILED):
            return
        order = [STATUS_PENDING, STATUS_RUNNING, STATUS_GENERATING, STATUS_COMPOSING]
        if status in order and current.get("status") in order:
            if order.index(status) < order.index(current["status"]):
                return
        task = tm.update_status(self._current_task_id, status, error)
        if task:
            await self._emit({"type": "task_status", "task": task})

    def get_replay_payload(self) -> Optional[Dict[str, Any]]:
        """返回进行中任务的重放数据（重连/同步时恢复界面状态）。"""
        if not self._running:
            return None
        return {
            "type": "replay",
            "running": True,
            "content": self._run_state.get("content", ""),
            "tool_events": self._run_state.get("tool_events", []),
            "media_items": self._run_state.get("media_items", []),
            "task": self._run_state.get("task"),
        }

    def _save_to_disk(self) -> None:
        """将会话历史持久化到磁盘（委托给 ChatManager）。"""
        if self._manager is not None:
            try:
                self._manager._save_session(self)
            except Exception as e:
                logger.error(f"会话 {self.id} 持久化失败: {e}")

    def _sanitize_messages(self) -> None:
        """清洗历史消息，确保所有 tool_call 的 arguments 是合法 JSON。

        防止模型流式生成时产生的无效 JSON 参数导致后续 API 调用返回 400 错误。
        """
        for msg in self.messages:
            if msg.get("role") != "assistant" or not msg.get("tool_calls"):
                continue
            for tc in msg["tool_calls"]:
                func = tc.get("function", {})
                args = func.get("arguments", "")
                if not args:
                    func["arguments"] = "{}"
                    continue
                if isinstance(args, str):
                    try:
                        json.loads(args)
                    except json.JSONDecodeError:
                        # 尝试修复
                        repaired = self._try_repair_json(args)
                        if repaired != args:
                            logger.warning(
                                f"会话 {self.id} 清洗历史消息: "
                                f"工具 {func.get('name', '?')} 参数 JSON 已修复"
                            )
                            func["arguments"] = repaired
                        else:
                            func["arguments"] = "{}"
                            logger.warning(
                                f"会话 {self.id} 清洗历史消息: "
                                f"工具 {func.get('name', '?')} 参数 JSON 无法修复，已置空"
                            )

    def _build_tool_result_message(
        self, tool_call_id: str, result: Any, tool_name: str
    ) -> Dict[str, Any]:
        """构建工具结果消息，支持多模态内容（图片）。

        当工具结果中包含 image_data 字段时（如 list_materials 返回的图片素材），
        使用 OpenAI 多模态 content 格式，让具备视觉能力的模型能直接"看到"图片。
        """
        result_dict = result.to_dict() if hasattr(result, "to_dict") else {"success": False}

        # 检查结果中是否包含图片数据
        image_data_urls: List[str] = []
        if tool_name == "list_materials" and isinstance(result_dict.get("data"), dict):
            for item in result_dict["data"].get("materials", []):
                img_data = item.get("image_data", "")
                if img_data:
                    image_data_urls.append(img_data)

        if not image_data_urls:
            # 无图片数据，使用普通文本格式
            return {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": json.dumps(result_dict, ensure_ascii=False),
            }

        # 有图片数据，使用多模态 content 格式
        # 文本部分：工具结果的 JSON（去掉 image_data 字段以减少 token 消耗）
        import copy
        slim_dict = copy.deepcopy(result_dict)
        if isinstance(slim_dict.get("data"), dict):
            for item in slim_dict["data"].get("materials", []):
                item.pop("image_data", None)
        text_content = json.dumps(slim_dict, ensure_ascii=False)

        content_parts: List[Dict[str, Any]] = [
            {"type": "text", "text": text_content}
        ]
        for img_url in image_data_urls:
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": img_url},
            })

        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content_parts,
        }

    @staticmethod
    def _try_repair_json(s: str) -> str:
        """尝试修复无效的 JSON 字符串。"""
        if not s or not s.strip():
            return "{}"
        # 尝试直接解析
        try:
            json.loads(s)
            return s
        except json.JSONDecodeError:
            pass
        # 尝试补全结尾大括号
        candidate = s.rstrip()
        if not candidate.endswith("}"):
            candidate += "}"
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                pass
        # 尝试提取 { ... } 部分
        first = s.find("{")
        last = s.rfind("}")
        if first >= 0 and last > first:
            candidate = s[first:last + 1]
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                pass
        return "{}"

    async def send_message(self, user_message: str, ws: Optional[WebSocket] = None) -> None:
        """运行 Agent 循环，通过 WebSocket 推送响应。

        - 每轮交互对应一个独立 Task（唯一 Task ID），生命周期独立于连接。
        - 连接断开任务继续后台执行；重连后可通过 get_replay_payload 恢复状态。
        - 每次交互结束（正常完成 / 出错 / 中断）后自动持久化对话历史到磁盘。
        - 排队消息由本方法链式启动，不依赖连接存活。
        """
        if ws is not None:
            self.ws = ws
        self._loop = asyncio.get_event_loop()
        self._running = True
        self._interrupted = False
        self._run_state = self._fresh_run_state()

        # 首次对话时，用用户第一句话的前 8 个字作为会话标题
        if not self.title or self.title == "新对话":
            self.title = user_message[:8].strip() or "新对话"

        # 创建任务（独立于聊天连接，切换会话/刷新页面不中断）
        task = get_task_manager().create(self.id, self.title or "新对话", user_message)
        self._current_task_id = task["id"]
        self._apply_task_id_to_tools()
        await self._emit({"type": "task_status", "task": task})

        try:
            await self._run_agent_loop(user_message)
        except Exception as e:
            logger.error(f"会话 {self.id} Agent 循环异常: {e}")
            try:
                await self._set_task_status(STATUS_FAILED, str(e)[:500])
                await self._emit({"type": "error", "message": f"内部错误: {e}"})
            except Exception:
                pass
        finally:
            self._running = False
            # 每次交互结束后自动持久化对话历史到磁盘
            self._save_to_disk()
            # 链式处理排队消息（不依赖 WebSocket 连接存活）
            if self._queued_messages:
                queued = self._queued_messages.pop(0)
                await self._emit({"type": "user_echo", "content": queued})
                self._agent_task = asyncio.create_task(self.send_message(queued))

    async def _run_agent_loop(self, user_message: str) -> None:
        """Agent 循环核心逻辑：处理用户消息并通过 _emit 推送响应。"""
        self.messages.append({"role": "user", "content": user_message})
        await self._set_task_status(STATUS_RUNNING)

        if not self.llm:
            await self._emit({"type": "error", "message": "LLM 未配置，请在设置中添加"})
            await self._set_task_status(STATUS_FAILED, "LLM 未配置")
            return

        tools_schema = self.registry.schemas() if self.registry else None
        # 步数上限需覆盖完整生产链路：一个 6-8 镜头的视频，
        # 每镜头 generate_image + generate_video + extract_last_frame ≈ 3 步，
        # 再加上素材分析、TTS、下载、合成，20 步很容易中途被切断。
        max_steps = 40

        for step in range(max_steps):
            # 检查中断信号
            if self._interrupted:
                # 保存已生成的内容并通知前端
                await self._set_task_status(STATUS_FAILED, "被用户中断")
                await self._emit({"type": "done", "content": ""})
                return

            # 清洗历史消息中的 tool_call arguments，防止无效 JSON 导致 API 400
            self._sanitize_messages()

            # 在后台线程运行 LLM，on_token 回调跨线程推送
            def on_token(token: str) -> None:
                # 检查中断信号：抛异常打断后台线程的流式迭代
                if self._interrupted:
                    raise InterruptedError("用户中断生成")
                if self._loop:
                    asyncio.run_coroutine_threadsafe(
                        self._emit({"type": "token", "content": token}),
                        self._loop,
                    )

            try:
                response = await asyncio.to_thread(
                    self.llm.chat,
                    messages=self.messages,
                    tools=tools_schema,
                    on_token=on_token,
                )
            except InterruptedError:
                logger.info(f"会话 {self.id} LLM 流式生成被用户中断")
                await self._set_task_status(STATUS_FAILED, "被用户中断")
                await self._emit({"type": "done", "content": ""})
                return
            except Exception as e:
                error_str = str(e)
                logger.error(f"LLM 调用失败: {e}")
                # 针对 400 arguments JSON 错误提供更友好的提示
                if "arguments must be valid JSON" in error_str or "tool call" in error_str.lower():
                    err_msg = "模型生成的工具调用参数格式异常，请重试或简化指令"
                else:
                    err_msg = f"模型调用失败: {e}"
                await self._emit({"type": "error", "message": err_msg})
                await self._set_task_status(STATUS_FAILED, err_msg)
                return

            if response is None:
                await self._emit({"type": "error", "message": "模型生成失败"})
                await self._set_task_status(STATUS_FAILED, "模型生成失败")
                return

            message = response.choices[0].message
            tool_calls = message.tool_calls or []
            content = message.content or ""

            if tool_calls:
                self.messages.append({
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["function"]["name"],
                                "arguments": tc["function"]["arguments"],
                            },
                        }
                        for tc in tool_calls
                    ],
                })

                for tc in tool_calls:
                    # 工具执行前检查中断
                    if self._interrupted:
                        await self._set_task_status(STATUS_FAILED, "被用户中断")
                        await self._emit({"type": "done", "content": ""})
                        return

                    tool_name = tc["function"]["name"]
                    raw_args = tc["function"]["arguments"]
                    try:
                        tool_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except json.JSONDecodeError:
                        tool_args = {}

                    # 任务状态流转：媒体生成 → generating，合成 → composing
                    if tool_name in _COMPOSE_TOOLS:
                        await self._set_task_status(STATUS_COMPOSING)
                    elif tool_name in _MEDIA_GEN_TOOLS:
                        await self._set_task_status(STATUS_GENERATING)

                    await self._emit({
                        "type": "tool_call",
                        "name": tool_name,
                        "arguments": tool_args,
                    })

                    result = await self.registry.call(tool_name, **tool_args)

                    # 检测生成内容中的媒体 URL，单独推送给前端展示
                    media_items = []
                    if result.success and result.data:
                        data = result.data if isinstance(result.data, dict) else {}
                        # 图片生成结果
                        for img_url in data.get("images", []):
                            media_items.append({"type": "image", "url": img_url})
                        # 视频生成结果
                        video_url = data.get("video_url", "")
                        if video_url:
                            media_items.append({"type": "video", "url": video_url})
                        # TTS 音频结果（本地文件路径转为 URL）
                        audio_path = data.get("audio_path", "")
                        if audio_path:
                            # 去掉 output/ 前缀，构造 URL
                            clean_path = audio_path.replace("\\", "/")
                            if clean_path.startswith("output/"):
                                clean_path = clean_path[7:]
                            elif "output/" in clean_path:
                                clean_path = clean_path.split("output/", 1)[-1]
                            media_items.append({"type": "audio", "url": f"/output/{clean_path}"})
                        # ffmpeg 合成结果（file_path 可能是视频或音频）
                        compose_path = data.get("file_path", "")
                        if compose_path and tool_name == "ffmpeg_compose":
                            clean_path = compose_path.replace("\\", "/")
                            if clean_path.startswith("output/"):
                                clean_path = clean_path[7:]
                            elif "output/" in clean_path:
                                clean_path = clean_path.split("output/", 1)[-1]
                            url = f"/output/{clean_path}"
                            # 根据扩展名判断类型
                            ext = clean_path.rsplit(".", 1)[-1].lower() if "." in clean_path else ""
                            if ext in ("mp4", "mov", "avi", "mkv", "webm"):
                                media_items.append({"type": "video", "url": url})
                            elif ext in ("mp3", "wav", "aac", "flac", "ogg", "m4a"):
                                media_items.append({"type": "audio", "url": url})

                    # 工具结果（简化，不含完整 data，仅含成功/失败状态和简要信息）
                    brief_data = {"success": result.success}
                    if result.error:
                        brief_data["error"] = result.error
                    await self._emit({
                        "type": "tool_result",
                        "name": tool_name,
                        "success": result.success,
                        "data": brief_data,
                    })

                    # 媒体内容单独推送，前端内联展示
                    if media_items:
                        await self._emit({
                            "type": "media",
                            "items": media_items,
                        })

                        # 素材登记到任务（用户 → 会话 → Task → 素材 层级）
                        try:
                            tm = get_task_manager()
                            for mi in media_items:
                                if mi.get("url"):
                                    tm.add_asset(self._current_task_id, {
                                        "type": mi.get("type", ""),
                                        "url": mi["url"],
                                        "tool": tool_name,
                                    })
                        except Exception as e:
                            logger.warning(f"任务素材登记失败（不影响聊天）: {e}")

                        # 将媒体内容入库到视频库
                        try:
                            lib = get_library_manager()
                            # 构建生成链路（基于当前消息历史）
                            chain = lib.build_generation_chain(
                                self.messages, user_message
                            )
                            for mi in media_items:
                                url = mi.get("url", "")
                                if not url:
                                    continue
                                # 去重：同一路径不重复入库
                                if lib.get_by_file_path(url):
                                    continue
                                # 生成标题
                                title = url.split("/")[-1] or f"{tool_name} 生成结果"
                                # 缩略图：图片用自身，视频/音频无
                                thumb = url if mi["type"] == "image" else ""
                                # 模型信息
                                model_name = ""
                                if tool_name == "generate_image":
                                    model_name = "image model"
                                elif tool_name == "generate_video":
                                    model_name = "video model"
                                elif tool_name == "text_to_speech":
                                    model_name = "tts model"
                                elif tool_name == "ffmpeg_compose":
                                    model_name = "ffmpeg"
                                lib.add_item(
                                    session_id=self.id,
                                    session_title=self.title or "",
                                    title=title,
                                    item_type=mi["type"],
                                    file_path=url,
                                    thumbnail=thumb,
                                    status="completed",
                                    model=model_name,
                                    generation_params=tool_args,
                                    generation_chain=chain,
                                )
                        except Exception as e:
                            logger.warning(f"视频库入库失败（不影响聊天）: {e}")

                    # 构建工具结果消息
                    # 如果工具结果包含图片数据（如 list_materials），使用多模态格式
                    tool_msg = self._build_tool_result_message(tc["id"], result, tool_name)
                    self.messages.append(tool_msg)
                continue

            # 没有工具调用 → 最终回复
            self.messages.append({"role": "assistant", "content": content})
            tm = get_task_manager()
            tm.set_result(self._current_task_id, content)
            await self._set_task_status(STATUS_COMPLETED)
            await self._emit({"type": "done", "content": content})
            return

        await self._emit({"type": "error", "message": "超过最大推理步数"})
        await self._set_task_status(STATUS_FAILED, "超过最大推理步数")

    def get_history(self) -> List[Dict[str, Any]]:
        """获取对话历史（不含 system 消息）。"""
        return [m for m in self.messages if m["role"] != "system"]

    def to_dict(self) -> Dict[str, Any]:
        # 统计素材数量
        materials_count = 0
        if self.materials_tool:
            summary = self.materials_tool.get_summary()
            materials_count = 0 if summary == "暂无素材" else len(summary.split(", "))
        return {
            "id": self.id,
            "title": self.title or "新对话",
            "messages": self.get_history(),
            "materials_count": materials_count,
        }


class ChatManager:
    """聊天会话管理器：维护内存中的会话，并负责磁盘持久化。"""

    def __init__(self) -> None:
        self._sessions: Dict[str, ChatSession] = {}

    # ==================== 持久化 ====================

    def _save_session(self, session: ChatSession) -> None:
        """保存单个会话到磁盘（sessions/<session_id>.json）。

        仅保存 id、title 和非 system 消息；工具调用相关消息（tool / assistant with tool_calls）会一并保存。
        保存时剥离多模态工具结果中的图片数据，避免文件过大。
        """
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

        # 深拷贝消息并剥离多模态内容中的 image_url 部分
        import copy
        saved_messages = []
        for m in session.messages:
            if m.get("role") == "system":
                continue
            msg_copy = copy.deepcopy(m)
            # 如果 tool 消息的 content 是列表（多模态），只保留文本部分
            if msg_copy.get("role") == "tool" and isinstance(msg_copy.get("content"), list):
                text_parts = [
                    p for p in msg_copy["content"]
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                if text_parts:
                    msg_copy["content"] = text_parts[0].get("text", "")
                else:
                    msg_copy["content"] = json.dumps({"success": True, "note": "图片内容已省略"})
            saved_messages.append(msg_copy)

        data = {
            "id": session.id,
            "title": session.title,
            "messages": saved_messages,
        }
        file_path = SESSIONS_DIR / f"{session.id}.json"
        file_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load_from_disk(self) -> None:
        """启动时从 sessions/ 目录加载所有已保存的会话。

        加载流程：
        1. 读取每个 sessions/<session_id>.json
        2. 创建 ChatSession（_init_components 会重新初始化 LLM、工具并注入系统提示词）
        3. 用持久化的非 system 消息覆盖消息历史（保留已注入的系统提示词）
        """
        if not SESSIONS_DIR.exists():
            SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            return

        loaded = 0
        for file_path in sorted(SESSIONS_DIR.glob("*.json")):
            try:
                data = json.loads(file_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.error(f"加载会话文件失败 {file_path}: {e}")
                continue

            session_id = data.get("id") or file_path.stem
            title = data.get("title", "")
            saved_messages = data.get("messages", [])

            # 创建会话：_init_components 会重新初始化 LLM、工具，并添加系统提示词
            try:
                session = ChatSession(session_id, title, manager=self)
            except Exception as e:
                logger.error(f"创建会话 {session_id} 失败（跳过）: {e}")
                continue
            # 保留系统提示词（messages[0]），追加持久化的历史消息
            # （系统提示词由 _init_components 重新生成，确保素材库信息最新）
            session.messages = session.messages[:1]
            session.messages.extend(saved_messages)
            # 加载后立即清洗，防止历史持久化的无效 JSON 参数触发 API 400
            session._sanitize_messages()

            self._sessions[session_id] = session
            loaded += 1

        if loaded:
            logger.info(f"从磁盘加载了 {loaded} 个聊天会话")

    # ==================== 会话管理 ====================

    def create_session(self, title: str = "") -> ChatSession:
        session_id = f"chat_{uuid.uuid4().hex[:8]}"
        session = ChatSession(session_id, title, manager=self)
        self._sessions[session_id] = session
        # 创建新会话时保存初始状态
        self._save_session(session)
        return session

    def get_session(self, session_id: str) -> Optional[ChatSession]:
        return self._sessions.get(session_id)

    def delete_session(self, session_id: str) -> bool:
        if session_id in self._sessions:
            del self._sessions[session_id]
            # 删除对应的磁盘文件
            file_path = SESSIONS_DIR / f"{session_id}.json"
            if file_path.exists():
                try:
                    file_path.unlink()
                except OSError as e:
                    logger.error(f"删除会话文件失败 {file_path}: {e}")
            return True
        return False

    def list_sessions(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": s.id,
                "title": s.title or "新对话",
                "messages_count": len(s.get_history()),
                "materials_count": s.to_dict().get("materials_count", 0),
            }
            for s in self._sessions.values()
        ]

    def refresh_all_registries(self) -> None:
        """刷新所有会话的工具注册中心（Skill / 模型配置变更后热更新）。"""
        for s in self._sessions.values():
            try:
                s.refresh_registry()
            except Exception as e:
                logger.error(f"会话 {s.id} 工具注册中心刷新失败: {e}")


# 全局单例
chat_manager = ChatManager()
