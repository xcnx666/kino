"""模型配置管理：四类模型（LLM / 图片 / 视频 / TTS）独立配置，JSON 持久化。

设计要点：
- 每类模型可添加多个配置项，其中一个是 active（当前使用）。
- 工具运行时根据 active 配置动态构建，不写死任何 provider。
- 配置存储在 config/models.json，支持热更新。
- API key 前端返回时打码，保存时支持"留空则不修改"。
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

# 配置文件路径
_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
_CONFIG_FILE = _CONFIG_DIR / "models.json"

# 四类模型类别
CATEGORIES = ("llm", "image", "video", "tts")

# 每类支持的 provider 及其字段定义
PROVIDER_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "llm": {
        "providers": ["openai_compatible", "anthropic"],
        "fields": ["name", "provider", "api_key", "base_url", "model"],
    },
    "image": {
        "providers": ["openai_compatible", "agnes"],
        "fields": ["name", "provider", "api_key", "base_url", "model", "default_size"],
    },
    "video": {
        "providers": ["agnes", "async_poll", "sync"],
        "fields": ["name", "provider", "api_key", "base_url", "model",
                    "default_width", "default_height", "default_duration",
                    "default_frame_rate", "default_num_frames",
                    "mode", "create_path", "poll_path",
                    "task_id_key", "status_key", "done_value", "fail_value", "url_key"],
    },
    "tts": {
        "providers": ["edge_tts", "openai_compatible", "agnes"],
        "fields": ["name", "provider", "api_key", "base_url", "model", "default_voice"],
    },
}


def _mask_key(key: str) -> str:
    """API key 打码：只显示前 4 位和后 4 位。"""
    if not key or len(key) <= 12:
        return "****" if key else ""
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


def _default_config() -> Dict[str, Any]:
    """生成默认配置（从 .env 读取已有配置）。"""
    from dotenv import load_dotenv
    env_path = _CONFIG_DIR.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    llm_key = os.getenv("api_key", "")
    llm_url = os.getenv("base_url", "")
    llm_model = os.getenv("model", "")

    agnes_key = os.getenv("AGNES_API_KEY", "")

    configs: Dict[str, Any] = {}
    for cat in CATEGORIES:
        configs[cat] = {"active_id": None, "configs": []}

    # LLM 默认配置
    if llm_key and llm_url and llm_model:
        llm_id = str(uuid.uuid4())[:8]
        configs["llm"]["configs"].append({
            "id": llm_id,
            "name": "默认 LLM",
            "provider": "openai_compatible",
            "api_key": llm_key,
            "base_url": llm_url,
            "model": llm_model,
        })
        configs["llm"]["active_id"] = llm_id

    # Image 默认配置（Agnes）
    if agnes_key:
        img_id = str(uuid.uuid4())[:8]
        configs["image"]["configs"].append({
            "id": img_id,
            "name": "Agnes 文生图",
            "provider": "agnes",
            "api_key": agnes_key,
            "base_url": "https://apihub.agnes-ai.com/v1",
            "model": "agnes-image-2.1-flash",
            "default_size": "1024x1024",
        })
        configs["image"]["active_id"] = img_id

        # Video 默认配置（Agnes）
        vid_id = str(uuid.uuid4())[:8]
        configs["video"]["configs"].append({
            "id": vid_id,
            "name": "Agnes 文生视频",
            "provider": "agnes",
            "api_key": agnes_key,
            "base_url": "https://apihub.agnes-ai.com/v1",
            "model": "agnes-video-v2.0",
            "default_width": 1152,
            "default_height": 768,
            "default_duration": 5,
            "default_frame_rate": 24,
            "default_num_frames": 121,
            "mode": "async_poll",
            "create_path": "/videos",
            "poll_path": "/videos/{task_id}",
            "task_id_key": "task_id",
            "status_key": "status",
            "done_value": "completed",
            "fail_value": "failed",
            "url_key": "remixed_from_video_id",
        })
        configs["video"]["active_id"] = vid_id

    # TTS 默认配置（edge-tts 免费）
    tts_id = str(uuid.uuid4())[:8]
    configs["tts"]["configs"].append({
        "id": tts_id,
        "name": "Edge TTS（免费）",
        "provider": "edge_tts",
        "api_key": "",
        "base_url": "",
        "model": "",
        "default_voice": "zh-CN-XiaoxiaoNeural",
    })
    configs["tts"]["active_id"] = tts_id

    return configs


class ModelConfigManager:
    """模型配置管理器：单例模式，读写 config/models.json。"""

    _instance: Optional["ModelConfigManager"] = None

    def __new__(cls) -> "ModelConfigManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._loaded = False
        return cls._instance

    def __init__(self) -> None:
        if not self._loaded:
            self._config: Dict[str, Any] = {}
            self._load()
            self._loaded = True

    def _load(self) -> None:
        """从文件加载配置，不存在则初始化。"""
        if _CONFIG_FILE.exists():
            try:
                self._config = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._config = _default_config()
                self._save()
        else:
            self._config = _default_config()
            self._save()

    def _save(self) -> None:
        """保存配置到文件。"""
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _CONFIG_FILE.write_text(
            json.dumps(self._config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def reload(self) -> None:
        """重新从文件加载（热更新）。"""
        self._load()

    # ==================== 查询 ====================

    def get_all(self, mask: bool = True) -> Dict[str, Any]:
        """获取全部配置。mask=True 时对 api_key 打码。"""
        result = {}
        for cat in CATEGORIES:
            result[cat] = self._get_category(cat, mask=mask)
        return result

    def _get_category(self, category: str, mask: bool = True) -> Dict[str, Any]:
        cat_data = self._config.get(category, {"active_id": None, "configs": []})
        configs = []
        for cfg in cat_data.get("configs", []):
            cfg_copy = dict(cfg)
            if mask and cfg_copy.get("api_key"):
                cfg_copy["api_key"] = _mask_key(cfg_copy["api_key"])
                cfg_copy["api_key_masked"] = True
            configs.append(cfg_copy)
        return {
            "active_id": cat_data.get("active_id"),
            "configs": configs,
        }

    def get_active(self, category: str) -> Optional[Dict[str, Any]]:
        """获取某类模型的当前激活配置（不打码，内部使用）。"""
        cat_data = self._config.get(category, {})
        active_id = cat_data.get("active_id")
        if not active_id:
            return None
        for cfg in cat_data.get("configs", []):
            if cfg["id"] == active_id:
                return dict(cfg)
        return None

    def get_providers(self) -> Dict[str, Any]:
        """获取各类模型支持的 provider 列表。"""
        return PROVIDER_SCHEMAS

    # ==================== 增删改 ====================

    def add(self, category: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """添加一个配置项。"""
        if category not in CATEGORIES:
            raise ValueError(f"未知类别: {category}")

        config_id = str(uuid.uuid4())[:8]
        new_cfg = {"id": config_id}
        for field in PROVIDER_SCHEMAS[category]["fields"]:
            new_cfg[field] = config.get(field, "")

        self._config.setdefault(category, {"active_id": None, "configs": []})
        self._config[category]["configs"].append(new_cfg)

        # 如果是第一个配置，自动激活
        if not self._config[category].get("active_id"):
            self._config[category]["active_id"] = config_id

        self._save()
        return new_cfg

    def update(self, category: str, config_id: str, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """更新配置项。api_key 为空字符串则保留原值。"""
        if category not in CATEGORIES:
            raise ValueError(f"未知类别: {category}")

        cat_data = self._config.setdefault(category, {"active_id": None, "configs": []})
        for cfg in cat_data["configs"]:
            if cfg["id"] == config_id:
                for field in PROVIDER_SCHEMAS[category]["fields"]:
                    if field in config:
                        # api_key 留空则不修改
                        if field == "api_key" and not config[field]:
                            continue
                        cfg[field] = config[field]
                self._save()
                return dict(cfg)
        return None

    def delete(self, category: str, config_id: str) -> bool:
        """删除配置项。"""
        if category not in CATEGORIES:
            raise ValueError(f"未知类别: {category}")

        cat_data = self._config.get(category, {})
        configs = cat_data.get("configs", [])
        cat_data["configs"] = [c for c in configs if c["id"] != config_id]

        # 如果删除的是激活项，自动切换到第一个
        if cat_data.get("active_id") == config_id:
            cat_data["active_id"] = cat_data["configs"][0]["id"] if cat_data["configs"] else None

        self._save()
        return True

    def activate(self, category: str, config_id: str) -> bool:
        """激活某个配置项。"""
        if category not in CATEGORIES:
            raise ValueError(f"未知类别: {category}")

        cat_data = self._config.get(category, {})
        ids = [c["id"] for c in cat_data.get("configs", [])]
        if config_id not in ids:
            return False
        cat_data["active_id"] = config_id
        self._save()
        return True


# 全局单例
def get_manager() -> ModelConfigManager:
    return ModelConfigManager()
