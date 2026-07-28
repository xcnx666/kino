"""文生视频 / 图生视频工具：通用接口，支持同步/异步轮询两种模式，兼容多家视频生成 API。

支持的 provider 模式：
- agnes: Agnes 专用异步轮询（POST /videos → GET /videos/{task_id}）
- async_poll: 通用异步轮询（可配置创建/轮询端点和字段名）
- sync: 同步模式（POST 请求直接返回视频 URL）

核心能力：
- 文生视频：仅使用文本 prompt 生成视频
- 图生视频：传入 image 参数（首帧图片 URL），结合 prompt 描述运动来生成视频
  * 工作流：先调用 generate_image 生成首帧图片 → 再调用 generate_video 并传入图片 URL

兼容的服务（通过 async_poll 或 sync 模式配置）：
- Agnes agnes-video 系列
- MiniMax video-01 系列
- Runway Gen-3 系列
- 智谱 CogVideoX、通义万相视频等
- 任意遵循 POST 创建 → 轮询状态的异步 API
- 任意 POST 直接返回 URL 的同步 API

duration 参数由 LLM 根据内容自行决定视频时长（秒），
工具内部根据 frame_rate 自动换算 num_frames。
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
import time
from typing import Any, Dict, Optional

import httpx

from core.tools.base import ToolBase, ToolResult, with_retry
from logger import logger

# ==================== 模块级速率限制器 ====================
# Agnes 视频 API 限制：每分钟 1 次请求
_last_video_create_ts: float = 0.0
_VIDEO_RATE_LIMIT_INTERVAL = 65.0  # 65 秒间隔，留 5 秒缓冲


async def _wait_for_video_rate_limit() -> None:
    """等待直到满足视频 API 速率限制（每分钟 1 次）。"""
    global _last_video_create_ts
    now = time.time()
    elapsed = now - _last_video_create_ts
    if elapsed < _VIDEO_RATE_LIMIT_INTERVAL:
        wait_time = _VIDEO_RATE_LIMIT_INTERVAL - elapsed
        logger.info(f"视频 API 速率限制：等待 {wait_time:.1f}s...")
        await asyncio.sleep(wait_time)
    _last_video_create_ts = time.time()


def _normalize_num_frames(num_frames: int) -> int:
    """将 num_frames 调整为符合 8n+1 规则的值（Agnes API 要求）。"""
    if num_frames <= 1:
        return 121  # 默认 5 秒 @ 24fps
    # 调整到最近的 8n+1 值
    n = round((num_frames - 1) / 8)
    result = 8 * n + 1
    # 上限 441
    return min(result, 441)


class VideoGenTool(ToolBase):
    name = "generate_video"
    description = (
        "根据文本提示词生成视频，支持文生视频和图生视频两种模式。"
        "图生视频模式：传入 image 参数（首帧图片），结合 prompt 描述画面运动。"
        "image 参数支持两种形式：1) generate_image 返回的 http(s) 图片 URL；"
        "2) extract_last_frame 返回的本地 frame_path（工具内部自动转为 data URI 上传，无需手动处理）。"
        "连续镜头必须将上一镜头 extract_last_frame 返回的 frame_path 作为本镜头的 image，保证画面连贯。"
        "返回视频下载 URL。"
        "duration 参数控制视频时长（秒），由模型根据内容决定。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "视频描述提示词。图生视频时描述画面应如何运动"},
            "image": {
                "type": "string",
                "description": "首帧图片（图生视频模式），传入后会以该图片为首帧生成视频。"
                "支持：1) generate_image 返回的 http(s) URL；"
                "2) extract_last_frame 返回的本地 frame_path（自动上传）。"
                "连续镜头必须传上一镜头的尾帧 frame_path。",
            },
            "duration": {
                "type": "number",
                "description": "视频时长（秒），由模型根据剧情需要决定。如 5 秒、10 秒等",
            },
            "width": {"type": "integer", "description": "视频宽度（像素）"},
            "height": {"type": "integer", "description": "视频高度（像素）"},
            "num_frames": {"type": "integer", "description": "总帧数（部分 provider 使用，一般由 duration×frame_rate 自动计算）"},
            "frame_rate": {"type": "integer", "description": "帧率 fps"},
            "timeout": {"type": "integer", "default": 300, "description": "轮询超时秒数"},
        },
        "required": ["prompt", "duration"],
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config = config or {}
        self.provider = self.config.get("provider", "agnes")
        self.api_key = self.config.get("api_key", "")
        self.base_url = self.config.get("base_url", "").rstrip("/")
        self.model = self.config.get("model", "")
        self.config_name = self.config.get("name", "未知模型")
        self.default_width = int(self.config.get("default_width", 1280))
        self.default_height = int(self.config.get("default_height", 720))
        self.default_num_frames = int(self.config.get("default_num_frames", 121))
        self.default_frame_rate = int(self.config.get("default_frame_rate", 24))
        self.default_duration = float(self.config.get("default_duration", 5))

        # 异步轮询模式的可配置字段（provider=async_poll 时生效）
        self.mode = self.config.get("mode", "async_poll")
        self.create_path = self.config.get("create_path", "/videos")
        self.poll_path = self.config.get("poll_path", "/videos/{task_id}")
        self.task_id_key = self.config.get("task_id_key", "task_id")
        self.status_key = self.config.get("status_key", "status")
        self.done_value = self.config.get("done_value", "completed")
        self.fail_value = self.config.get("fail_value", "failed")
        self.url_key = self.config.get("url_key", "video_url")

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def execute(
        self,
        prompt: str,
        duration: float = 0,
        image: str = "",
        width: int = 0,
        height: int = 0,
        num_frames: int = 0,
        frame_rate: int = 0,
        timeout: int = 300,
    ) -> ToolResult:
        duration = duration or self.default_duration
        frame_rate = frame_rate or self.default_frame_rate
        width = width or self.default_width
        height = height or self.default_height
        # 优先使用显式传入的 num_frames，否则根据 duration × frame_rate 计算
        if not num_frames:
            num_frames = int(duration * frame_rate) or self.default_num_frames
        # Agnes API 要求 num_frames 符合 8n+1 规则
        num_frames = _normalize_num_frames(num_frames)

        if not self.api_key or not self.base_url:
            return ToolResult(
                success=False,
                error=(
                    f"【视频生成失败】模型「{self.config_name}」配置不完整："
                    f"base_url={'未设置' if not self.base_url else '已设置'}, "
                    f"api_key={'未设置' if not self.api_key else '已设置'}。"
                    "请在「模型配置 → 文生视频」中检查配置，或更换其他视频生成服务。"
                ),
            )

        # 验证图片：外部 API 需要可访问的图片内容。
        # 支持 http(s) URL 直传；本地路径（绝对路径或 /output/... 相对 URL）自动转 data URI 上传，
        # 这样 extract_last_frame 返回的尾帧可以直接复用，保证连续镜头的画面一致性。
        if image and not image.startswith(("http://", "https://")):
            logger.info(f"视频生成收到本地图片路径，转为 data URI 上传: {image}")
            data_uri = self._local_image_to_data_uri(image)
            if data_uri:
                image = data_uri
            else:
                return ToolResult(
                    success=False,
                    error=(
                        f"image 参数必须是 http(s) URL 或存在的本地图片路径，收到: {image}。"
                        "可传入 generate_image 返回的图片 URL，"
                        "或 extract_last_frame 返回的 frame_path（尾帧本地路径）。"
                    ),
                )

        # 标记生成模式
        gen_mode = "图生视频" if image else "文生视频"

        try:
            if self.provider == "agnes":
                video_url = await self._gen_agnes(
                    prompt, image=image, width=width, height=height,
                    num_frames=num_frames, frame_rate=frame_rate, timeout=timeout,
                )
            elif self.mode == "sync":
                video_url = await self._gen_sync(
                    prompt, image=image, width=width, height=height,
                    duration=duration, num_frames=num_frames, frame_rate=frame_rate,
                )
            else:
                # 通用异步轮询
                video_url = await self._gen_async_poll(
                    prompt, image=image, width=width, height=height,
                    duration=duration, num_frames=num_frames, frame_rate=frame_rate,
                    timeout=timeout,
                )

            if not video_url:
                return ToolResult(success=False, error="视频任务完成但未返回 URL")
            return ToolResult(
                success=True,
                data={
                    "video_url": video_url,
                    "duration": duration,
                    "mode": gen_mode,
                    "has_image": bool(image),
                },
            )
        except Exception as e:  # noqa: BLE001
            error_msg = str(e)
            # 提供更清晰的错误信息，告诉用户哪个模型不可用
            return ToolResult(
                success=False,
                error=(
                    f"【视频生成失败】模型「{self.config_name}」({self.model or '未指定'}) 无法完成视频生成。\n"
                    f"错误原因: {error_msg}\n\n"
                    "建议：\n"
                    "1. 该模型可能不支持视频生成功能，请在「模型配置 → 文生视频」中更换为支持的视频服务\n"
                    "2. 或检查 API Key 是否有效、网络连接是否正常\n"
                    "3. 支持的视频服务包括：Seedance、Runway、fal.ai 等"
                ),
            )

    # ==================== 本地图片处理 ====================

    @staticmethod
    def _local_image_to_data_uri(image: str) -> Optional[str]:
        """将本地图片路径转为 base64 data URI（JPEG 压缩以控制请求体积）。

        支持两种输入：
        - 绝对路径，如 /Users/.../output/chat_xxx/frames/frame_xxx.png
        - 相对 URL，如 /output/chat_xxx/frames/frame_xxx.png（映射到项目根目录）
        找不到文件或转换失败时返回 None。
        """
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        candidates = []
        if os.path.isabs(image):
            # 真·绝对路径（extract_last_frame 返回的 frame_path）
            candidates.append(image)
        # /output/... 或 /uploads/... 形式的相对 URL（以 / 开头会被 isabs 误判为绝对路径，
        # 因此无论是否 isabs 都追加项目根目录拼接作为候选）
        candidates.append(os.path.join(base_dir, image.lstrip("/")))

        local_path = next((p for p in candidates if os.path.isfile(p)), None)
        if not local_path:
            logger.warning(f"本地图片不存在: {image}")
            return None

        try:
            from PIL import Image as PILImage

            with PILImage.open(local_path) as im:
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=90)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            logger.info(
                f"本地图片已转为 data URI: {local_path} "
                f"({os.path.getsize(local_path)} 字节 → {len(b64)} 字符)"
            )
            return f"data:image/jpeg;base64,{b64}"
        except Exception as e:
            logger.error(f"本地图片转 data URI 失败: {local_path}: {e}")
            return None

    # ==================== Agnes 专用 ====================

    async def _agnes_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """创建 Agnes 视频任务，自动处理速率限制、429 和 503 重试。"""
        url = f"{self.base_url}/videos"
        logger.info(f"视频创建请求 URL: {url}, model: {self.model}")
        max_retries = 3
        last_exc: Optional[Exception] = None

        for attempt in range(max_retries):
            # 等待速率限制
            await _wait_for_video_rate_limit()

            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    resp = await client.post(url, json=payload, headers=self._headers())

                    # 429 速率限制：读取 Retry-After，等待后重试
                    if resp.status_code == 429:
                        retry_after = int(resp.headers.get("Retry-After", "65"))
                        logger.warning(f"视频 API 返回 429，等待 {retry_after}s 后重试 (attempt {attempt+1}/{max_retries})")
                        await asyncio.sleep(retry_after)
                        continue

                    # 503 服务不可用：等待后重试
                    if resp.status_code == 503:
                        delay = 10 * (attempt + 1)
                        logger.warning(f"视频 API 返回 503 (Service Unavailable)，{delay}s 后重试 (attempt {attempt+1}/{max_retries})")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(delay)
                            continue
                        # 最后一次重试也失败，返回详细错误
                        body = resp.text[:300]
                        raise httpx.HTTPStatusError(
                            f"503 Service Unavailable — 视频生成服务暂时不可用，请稍后重试。响应: {body}",
                            request=resp.request, response=resp,
                        )

                    # 500/502 等其他服务端错误也重试
                    if resp.status_code >= 500:
                        delay = 5 * (attempt + 1)
                        logger.warning(f"视频 API 返回 {resp.status_code}，{delay}s 后重试 (attempt {attempt+1}/{max_retries})")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(delay)
                            continue
                        body = resp.text[:300]
                        raise httpx.HTTPStatusError(
                            f"HTTP {resp.status_code}: {body}",
                            request=resp.request, response=resp,
                        )

                    # 4xx 错误：不重试，直接报错
                    if resp.status_code >= 400:
                        body = resp.text[:500]
                        logger.error(f"视频创建 API 错误 {resp.status_code}: {body}")
                        raise httpx.HTTPStatusError(
                            f"HTTP {resp.status_code}: {body}",
                            request=resp.request, response=resp,
                        )

                    return resp.json()

            except httpx.ConnectError as e:
                last_exc = e
                if attempt < max_retries - 1:
                    delay = 2 ** attempt
                    logger.warning(f"视频创建连接失败，{delay}s 后重试: {e}")
                    await asyncio.sleep(delay)
                else:
                    raise RuntimeError(f"视频生成连接失败（已重试3次）: {e}") from e
            except (httpx.HTTPError, httpx.RequestError) as e:
                last_exc = e
                if attempt < max_retries - 1:
                    delay = 2 ** attempt
                    logger.warning(f"视频创建网络错误，{delay}s 后重试: {e}")
                    await asyncio.sleep(delay)
                else:
                    raise

        if last_exc:
            raise last_exc
        raise RuntimeError("视频创建失败：超过最大重试次数")

    @with_retry(max_retries=5, exceptions=(httpx.HTTPError, httpx.RequestError))
    async def _agnes_poll(self, task_id: str) -> Dict[str, Any]:
        url = f"{self.base_url}/videos/{task_id}"
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    def _extract_agnes_video_url(status: Dict[str, Any]) -> str:
        """从 Agnes 轮询响应中提取视频 URL，兼容多种字段位置。"""
        # 优先：metadata.url（新版 API 推荐字段）
        metadata = status.get("metadata", {})
        if isinstance(metadata, dict):
            url = metadata.get("url", "")
            if url:
                return url
        # 兼容：顶层 url_key 字段
        for key in ("video_url", "url", "remixed_from_video_id", "output"):
            val = status.get(key, "")
            if val:
                return val
        return ""

    async def _gen_agnes(
        self, prompt: str, *, image: str = "", width: int, height: int,
        num_frames: int, frame_rate: int, timeout: float = 300.0,
    ) -> str:
        payload: Dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "width": width,
            "height": height,
            "num_frames": num_frames,
            "frame_rate": frame_rate,
        }
        # 图生视频：传入首帧图片 URL
        if image:
            payload["image"] = image

        create_resp = await self._agnes_create(payload)
        task_id = create_resp.get("task_id") or create_resp.get("id")
        if not task_id:
            raise RuntimeError(f"创建视频任务失败，未返回 task_id: {create_resp}")

        elapsed = 0.0
        poll_interval = 5.0
        while elapsed < timeout:
            status = await self._agnes_poll(task_id)
            state = status.get("status")
            if state == "completed":
                video_url = self._extract_agnes_video_url(status)
                if video_url:
                    return video_url
                raise RuntimeError(f"视频已完成但未找到 URL: {status}")
            if state == "failed":
                raise RuntimeError(f"视频生成失败: {status.get('error', '未知错误')}")
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        raise TimeoutError(f"视频生成超时 (task_id={task_id})")

    # ==================== 通用异步轮询 ====================

    @with_retry(max_retries=3, exceptions=(httpx.HTTPError, httpx.RequestError))
    async def _async_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        await _wait_for_video_rate_limit()
        url = f"{self.base_url}{self.create_path}"
        logger.info(f"视频创建请求 URL: {url}, model: {self.model}")
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, json=payload, headers=self._headers())
            # 429 速率限制
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "65"))
                logger.warning(f"视频 API 返回 429，等待 {retry_after}s")
                await asyncio.sleep(retry_after)
            # 503 服务不可用
            if resp.status_code == 503:
                logger.warning("视频 API 返回 503，等待 10s 后重试")
                await asyncio.sleep(10)
            resp.raise_for_status()
            return resp.json()

    @with_retry(max_retries=5, exceptions=(httpx.HTTPError, httpx.RequestError))
    async def _async_poll(self, task_id: str) -> Dict[str, Any]:
        # 支持 {task_id} 占位符或查询参数
        if "{task_id}" in self.poll_path:
            path = self.poll_path.replace("{task_id}", task_id)
        else:
            path = f"{self.poll_path}?task_id={task_id}"
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    def _extract_video_url(status: Dict[str, Any], url_key: str) -> str:
        """从轮询响应中提取视频 URL，兼容多种字段位置。"""
        # 尝试 metadata.url
        metadata = status.get("metadata", {})
        if isinstance(metadata, dict):
            url = metadata.get("url", "")
            if url:
                return url
        # 尝试配置的 url_key 及常见字段名
        for key in (url_key, "video_url", "url", "output", "video", "result"):
            val = status.get(key, "")
            if val:
                return val
        # 尝试 data 嵌套结构
        nested = status.get("data", {})
        if isinstance(nested, dict):
            for key in ("url", "video_url", "video", "output"):
                val = nested.get(key, "")
                if val:
                    return val
        return ""

    async def _gen_async_poll(
        self, prompt: str, *, image: str = "", width: int, height: int,
        duration: float, num_frames: int, frame_rate: int, timeout: float = 300.0,
    ) -> str:
        payload: Dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "width": width,
            "height": height,
        }
        # 图生视频：传入首帧图片 URL
        if image:
            payload["image"] = image
        # 尝试传入常见字段，不支持的 API 会忽略多余字段
        if duration:
            payload["duration"] = duration
        if num_frames:
            payload["num_frames"] = num_frames
        if frame_rate:
            payload["frame_rate"] = frame_rate

        create_resp = await self._async_create(payload)
        task_id = create_resp.get(self.task_id_key) or create_resp.get("id")
        if not task_id:
            raise RuntimeError(f"创建视频任务失败，未返回 {self.task_id_key}: {create_resp}")

        elapsed = 0.0
        poll_interval = 5.0
        while elapsed < timeout:
            status = await self._async_poll(task_id)
            state = str(status.get(self.status_key, "")).lower()
            if state == self.done_value.lower():
                video_url = self._extract_video_url(status, self.url_key)
                if video_url:
                    return video_url
                raise RuntimeError(f"视频已完成但未找到 URL: {status}")
            if state == self.fail_value.lower():
                raise RuntimeError(f"视频生成失败: {status}")
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        raise TimeoutError(f"视频生成超时 (task_id={task_id})")

    # ==================== 同步模式 ====================

    async def _gen_sync(
        self, prompt: str, *, image: str = "", width: int, height: int,
        duration: float, num_frames: int, frame_rate: int,
    ) -> str:
        payload: Dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "width": width,
            "height": height,
        }
        # 图生视频：传入首帧图片 URL
        if image:
            payload["image"] = image
        if duration:
            payload["duration"] = duration
        if num_frames:
            payload["num_frames"] = num_frames
        if frame_rate:
            payload["frame_rate"] = frame_rate

        url = f"{self.base_url}{self.create_path}"
        await _wait_for_video_rate_limit()
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(url, json=payload, headers=self._headers())
            resp.raise_for_status()
            data = resp.json()

        return self._extract_video_url(data, self.url_key)
