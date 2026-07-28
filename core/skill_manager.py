"""Skill 技能管理：可复用能力模块的 CRUD、持久化与动态工具构建。

设计要点：
- Skill 不绑定具体模型：执行时使用「当前激活」的 LLM 配置，换模型无需改 Skill。
- 每个启用的 Skill 在工具注册中心里表现为一个 `skill_<name>` 工具，Agent 可自动调用。
- Skill 本体内存放 prompt 模板与参数 schema；执行 = 参数注入模板 + LLM 生成。
- 数据存储在 config/skills.json，支持热更新。
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.tools.base import ToolBase, ToolResult
from logger import logger

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
_SKILLS_FILE = _CONFIG_DIR / "skills.json"


def build_active_llm():
    """根据当前激活的 LLM 配置构建客户端（模型无关）。

    Skill 与 Agent 都通过该函数获取 LLM，更换模型只改配置，不改代码。
    """
    from core.config_manager import get_manager
    from llm import LLM, AnthropicLLM

    cfg = get_manager().get_active("llm")
    if not cfg:
        return None
    provider = cfg.get("provider", "openai_compatible")
    if provider == "anthropic":
        return AnthropicLLM(
            api_key=cfg.get("api_key", ""),
            base_url=cfg.get("base_url", ""),
            model=cfg.get("model", ""),
        )
    return LLM(
        api_key=cfg.get("api_key", ""),
        base_url=cfg.get("base_url", ""),
        model=cfg.get("model", ""),
    )


# ==================== 内置 Skill ====================

BUILTIN_SKILLS: List[Dict[str, Any]] = [
    {
        "name": "storyboard_generator",
        "title": "分镜生成",
        "description": "将创意描述拆解为结构化分镜脚本（JSON），包含镜头类型、画面描述、旁白、时长、情绪点与转场标记，可直接用于后续逐镜头生产。",
        "builtin": True,
        "inputs": [
            {"name": "concept", "description": "创意描述/剧本内容", "required": True},
            {"name": "shot_count", "description": "镜头数量，如 6", "required": True},
            {"name": "duration", "description": "总时长（秒），如 30", "required": True},
        ],
        "outputs": [
            {"name": "storyboard", "description": "分镜 JSON 数组"},
        ],
        "tools": ["generate_image", "generate_video", "extract_last_frame"],
        "prompt": """你是一位专业分镜师。根据以下创意描述，将其拆解为 {shot_count} 个镜头的分镜脚本（总时长约 {duration} 秒）。

创意描述：
{concept}

输出要求（严格遵守）：
1. 只输出合法的 JSON 数组，不要输出任何解释或 Markdown 代码块。
2. 每个元素格式：
[{"shot": 1, "type": "wide|medium|close-up|aerial|macro", "visual": "画面内容的英文描述（可直接用于文生图提示词扩写）", "narration": "旁白文本（与创意描述同语言）", "duration": 5, "emotion": "该镜头的情绪点", "transition": "CONTINUOUS|SCENE_CHANGE|MATCH_CUT"}]
3. 第 1 个镜头必须是 hook（前 3-5 秒抓住注意力）。
4. 相邻镜头同场景连续动作标 CONTINUOUS，换场景标 SCENE_CHANGE。
5. 所有镜头 duration 之和约等于总时长。""",
    },
    {
        "name": "character_bible",
        "title": "角色一致性",
        "description": "根据角色描述/参考图特征生成「角色圣经」锁定文本（Bible B），用于在所有镜头的提示词中逐字复用，保证角色外观跨镜头一致。",
        "builtin": True,
        "inputs": [
            {"name": "character_description", "description": "角色外观描述或参考图特征转写", "required": True},
        ],
        "outputs": [
            {"name": "bible", "description": "角色圣经文本 + 关键词"},
        ],
        "tools": ["generate_image", "generate_video"],
        "prompt": """你是角色设定总监。根据以下角色信息，生成可直接用于 AI 绘图提示词的「角色圣经」(Character Bible)。

角色信息：
{character_description}

输出格式（严格遵守，只输出以下三个部分）：

## Bible B — Character Bible (LOCKED)
一段 40-80 词的英文角色外观描述，包含：年龄、脸型、发型发色、眼睛、体型、每件服装单品（颜色+材质+款式）、1-2 个标志性特征或颜色锚点。描述必须具体到可逐字复用，禁止模糊词汇（如"好看的衣服"）。

## Key visual keywords
3-5 个英文关键词（用于视频生成提示词末尾的外观强化），逗号分隔。

## 使用说明
一句话说明如何在提示词中逐字复用该圣经。""",
    },
    {
        "name": "video_prompt_optimizer",
        "title": "视频提示词优化",
        "description": "将草稿优化为图生视频运动提示词：风格锚定串开头 + 具体运动描述 + 角色关键词结尾，防止视频模型风格漂移。",
        "builtin": True,
        "inputs": [
            {"name": "shot_description", "description": "镜头内容描述", "required": True},
            {"name": "draft_prompt", "description": "草稿提示词（可为空）", "required": False},
            {"name": "style_lock", "description": "风格锚定串原文", "required": True},
            {"name": "character_keywords", "description": "角色关键词（可为空）", "required": False},
        ],
        "outputs": [
            {"name": "prompt", "description": "优化后的视频运动提示词"},
        ],
        "tools": ["generate_video"],
        "prompt": """你是 AI 视频提示词专家。将以下草稿优化为高质量的图生视频运动提示词。

镜头描述：{shot_description}
草稿提示词：{draft_prompt}
风格锚定串（必须逐字放在提示词开头）：{style_lock}
角色关键词（如有，放在提示词结尾）：{character_keywords}

输出要求：
1. 只输出优化后的英文提示词本身，不要任何解释。
2. 结构：风格锚定串原文 + 运动描述 + 角色关键词。
3. 运动描述要具体：明确什么在动、怎么动、镜头如何运动（推/拉/摇/移/跟）。
4. 长度 2-4 句，不要输出字面占位符。""",
    },
    {
        "name": "video_editing",
        "title": "视频剪辑",
        "description": "根据剪辑目标与素材列表产出剪辑方案：片段顺序、转场、音频叠加、字幕，并给出可直接执行的 ffmpeg_compose 参数或 ffmpeg 命令。",
        "builtin": True,
        "inputs": [
            {"name": "editing_goal", "description": "剪辑目标，如'按分镜顺序拼接并叠加旁白'", "required": True},
            {"name": "asset_list", "description": "素材清单（视频/音频路径或 URL）", "required": True},
        ],
        "outputs": [
            {"name": "plan", "description": "剪辑方案 + 可执行参数"},
        ],
        "tools": ["ffmpeg_compose", "bash", "download_file"],
        "prompt": """你是视频剪辑师。根据剪辑目标和素材列表，给出可直接执行的剪辑方案。

剪辑目标：{editing_goal}
素材列表：
{asset_list}

输出要求：
1. 先简要列出剪辑决策（片段顺序、单段时长、转场方式、音频叠加、字幕策略）。
2. 再给出可直接调用 ffmpeg_compose 工具的参数 JSON：
{"clips": ["片段路径或URL"], "audio": "音频路径或空字符串", "output": "output.mp4"}
3. 如需更复杂的操作（裁剪/变速/字幕烧录），给出具体 ffmpeg 命令（可由 bash 工具执行）。
4. 素材路径必须来自给定素材列表，不要编造。""",
    },
]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _slugify(name: str) -> str:
    """将名称转为合法工具名标识（小写字母/数字/下划线）。"""
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", name.strip().lower()).strip("_")
    return slug or f"skill_{uuid.uuid4().hex[:6]}"


# ==================== Skill 动态工具 ====================

class SkillTool(ToolBase):
    """由 Skill 定义动态构建的工具：执行 = 参数注入 prompt 模板 + LLM 生成。

    模型无关：每次执行时根据当前激活的 LLM 配置构建客户端。
    """

    def __init__(self, skill: Dict[str, Any]) -> None:
        self.skill = skill
        self.name = f"skill_{skill['name']}"
        # Skill 只提供能力和规则，工具调用由 Agent 模型自主决定
        self.description = (
            f"[技能] {skill.get('title', skill['name'])}：{skill.get('description', '')}"
        )

        properties: Dict[str, Any] = {}
        required: List[str] = []
        for inp in skill.get("inputs", []):
            properties[inp["name"]] = {
                "type": "string",
                "description": inp.get("description", ""),
            }
            if inp.get("required", True):
                required.append(inp["name"])
        # 没有输入参数时给一个通用 content 参数，避免空 schema 让模型无所适从
        if not properties:
            properties["content"] = {"type": "string", "description": "输入内容"}
        self.parameters = {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    async def execute(self, **kwargs) -> ToolResult:
        prompt = self.skill.get("prompt", "")
        if not prompt:
            return ToolResult(success=False, error=f"技能 {self.skill.get('name')} 未配置 prompt")

        # 参数注入：用 replace 而非 format，避免模板中 JSON 花括号被误解析
        for key, value in kwargs.items():
            prompt = prompt.replace("{" + key + "}", str(value))
        # 未填充的可选参数占位 → 空字符串（保留必填缺失的占位提示）
        for inp in self.skill.get("inputs", []):
            placeholder = "{" + inp["name"] + "}"
            if placeholder in prompt and not inp.get("required", True):
                prompt = prompt.replace(placeholder, "（未提供）")

        llm = build_active_llm()
        if llm is None:
            return ToolResult(success=False, error="LLM 未配置，请在「模型配置」中添加并激活一个 LLM")

        try:
            text = await asyncio.to_thread(
                llm.chat,
                [{"role": "user", "content": prompt}],
            )
        except Exception as e:  # noqa: BLE001
            return ToolResult(success=False, error=f"技能执行失败（LLM 调用异常）: {e}")

        if text is None:
            return ToolResult(success=False, error="技能执行失败：LLM 返回为空")

        content = text if isinstance(text, str) else getattr(text, "content", str(text))
        logger.info(f"技能 {self.skill.get('name')} 执行完成，输出 {len(content)} 字符")
        return ToolResult(
            success=True,
            data={
                "skill": self.skill.get("name"),
                "result": content,
                "suggested_tools": self.skill.get("tools", []),
            },
        )


# ==================== Skill 管理器 ====================

class SkillManager:
    """Skill 管理器：单例模式，读写 config/skills.json。"""

    _instance: Optional["SkillManager"] = None

    def __new__(cls) -> "SkillManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._loaded = False
        return cls._instance

    def __init__(self) -> None:
        if not self._loaded:
            self._skills: Dict[str, Dict[str, Any]] = {}
            self._load()
            self._loaded = True

    def _load(self) -> None:
        """从文件加载；不存在则用内置 Skill 初始化。"""
        if _SKILLS_FILE.exists():
            try:
                data = json.loads(_SKILLS_FILE.read_text(encoding="utf-8"))
                self._skills = data.get("skills", {})
                # 补齐新增的内置 Skill（旧文件里可能没有）
                changed = False
                for s in BUILTIN_SKILLS:
                    if not any(existing.get("name") == s["name"] and existing.get("builtin")
                               for existing in self._skills.values()):
                        sid = f"skill_{uuid.uuid4().hex[:8]}"
                        self._skills[sid] = self._with_meta(s, sid, enabled=True)
                        changed = True
                if changed:
                    self._save()
            except (json.JSONDecodeError, OSError):
                self._skills = {}
                self._init_builtin()
        else:
            self._skills = {}
            self._init_builtin()

    def _init_builtin(self) -> None:
        for s in BUILTIN_SKILLS:
            sid = f"skill_{uuid.uuid4().hex[:8]}"
            self._skills[sid] = self._with_meta(s, sid, enabled=True)
        self._save()

    @staticmethod
    def _with_meta(skill: Dict[str, Any], sid: str, enabled: bool) -> Dict[str, Any]:
        item = dict(skill)
        item.update({
            "id": sid,
            "enabled": enabled,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        })
        return item

    def _save(self) -> None:
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _SKILLS_FILE.write_text(
            json.dumps({"skills": self._skills}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def reload(self) -> None:
        self._load()

    # ==================== 查询 ====================

    def list_skills(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        skills = list(self._skills.values())
        if enabled_only:
            skills = [s for s in skills if s.get("enabled")]
        skills.sort(key=lambda s: (not s.get("builtin", False), s.get("created_at", "")))
        return skills

    def get_skill(self, skill_id: str) -> Optional[Dict[str, Any]]:
        return self._skills.get(skill_id)

    def build_skill_tools(self) -> List[SkillTool]:
        """为所有启用的非内置 Skill 构建动态工具（供注册中心注册）。

        内置 Skill 的 prompt 已合并到系统提示词，不再注册为独立工具。
        """
        return [
            SkillTool(s) for s in self.list_skills(enabled_only=True)
            if not s.get("builtin")
        ]

    # ==================== 增删改 ====================

    _EDITABLE_FIELDS = ("title", "description", "inputs", "outputs", "prompt", "tools", "enabled")

    def add(self, data: Dict[str, Any]) -> Dict[str, Any]:
        name = _slugify(data.get("name") or data.get("title") or "")
        # 同名冲突检查（同名会生成相同工具名）
        for existing in self._skills.values():
            if existing.get("name") == name:
                raise ValueError(f"技能标识 {name} 已存在，请换一个名称")
        sid = f"skill_{uuid.uuid4().hex[:8]}"
        skill = {
            "id": sid,
            "name": name,
            "title": data.get("title") or name,
            "description": data.get("description", ""),
            "builtin": False,
            "source": data.get("source", "custom"),  # imported / custom
            "enabled": bool(data.get("enabled", True)),
            "inputs": data.get("inputs", []),
            "outputs": data.get("outputs", []),
            "prompt": data.get("prompt", ""),
            "tools": data.get("tools", []),
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        self._skills[sid] = skill
        self._save()
        logger.info(f"Skill 新增: {skill['title']} ({name})")
        return skill

    def update(self, skill_id: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        skill = self._skills.get(skill_id)
        if not skill:
            return None
        for field in self._EDITABLE_FIELDS:
            if field in data:
                skill[field] = data[field]
        # name 仅非内置技能可改（改 name 会改变工具名）
        if "name" in data and not skill.get("builtin"):
            new_name = _slugify(data["name"])
            for sid, existing in self._skills.items():
                if sid != skill_id and existing.get("name") == new_name:
                    raise ValueError(f"技能标识 {new_name} 已存在")
            skill["name"] = new_name
        skill["updated_at"] = _now_iso()
        self._save()
        return skill

    def delete(self, skill_id: str) -> bool:
        skill = self._skills.get(skill_id)
        if not skill:
            return False
        if skill.get("builtin"):
            raise ValueError("内置技能不可删除，可选择禁用")
        del self._skills[skill_id]
        self._save()
        return True

    def toggle(self, skill_id: str, enabled: bool) -> Optional[Dict[str, Any]]:
        skill = self._skills.get(skill_id)
        if not skill:
            return None
        skill["enabled"] = enabled
        skill["updated_at"] = _now_iso()
        self._save()
        return skill

    def import_package(self, zip_bytes: bytes) -> Dict[str, Any]:
        """从 zip 压缩包导入 Skill。

        支持两种格式：
        1. skill.json（自定义格式）
        2. SKILL.md（OpenAI Codex Agent Skills 格式，YAML frontmatter + Markdown）
        """
        return self._import_from_zip_bytes(zip_bytes)

    def import_from_github(self, url: str) -> Dict[str, Any]:
        """从 GitHub 仓库 URL 导入 Skill。

        优先使用 GitHub API 下载 zip（更快），失败时回退到 git clone。
        """
        import urllib.request
        import urllib.error

        # 规范化 GitHub URL
        url = url.strip()
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        # 去掉 .git 后缀
        if url.endswith('.git'):
            url = url[:-4]

        # 提取 owner/repo
        match = re.search(r'github\.com/([^/]+)/([^/]+)', url)
        if not match:
            raise ValueError('不是有效的 GitHub 仓库地址')
        owner, repo = match.group(1), match.group(2)

        # 尝试通过 GitHub API 下载 zip
        zip_url = f'https://github.com/{owner}/{repo}/archive/refs/heads/main.zip'
        alt_zip_url = f'https://github.com/{owner}/{repo}/archive/refs/heads/master.zip'

        zip_bytes = None
        for download_url in [zip_url, alt_zip_url]:
            try:
                logger.info(f'尝试从 GitHub 下载: {download_url}')
                req = urllib.request.Request(download_url, headers={'User-Agent': 'Kino/1.0'})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    zip_bytes = resp.read()
                    break
            except (urllib.error.URLError, urllib.error.HTTPError, Exception) as e:
                logger.warning(f'下载失败 {download_url}: {e}')
                continue

        if zip_bytes:
            # 成功下载 zip，解析内容
            return self._import_from_zip_bytes(zip_bytes, github_url=url)

        # 回退到 git clone
        logger.info('GitHub API 下载失败，回退到 git clone')
        tmp_dir = tempfile.mkdtemp(prefix='skill_import_')
        try:
            result = subprocess.run(
                ['git', 'clone', '--depth', '1', url, tmp_dir],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                raise ValueError(f"Git 克隆失败: {result.stderr.strip()}")

            skill_md = None
            skill_json = None
            for root, dirs, files in os.walk(tmp_dir):
                dirs[:] = [d for d in dirs if d != '.git']
                for f in files:
                    if f == 'SKILL.md' and skill_md is None:
                        skill_md = os.path.join(root, f)
                    elif f == 'skill.json' and skill_json is None:
                        skill_json = os.path.join(root, f)

            if skill_json:
                with open(skill_json, 'r', encoding='utf-8') as fp:
                    skill_data = json.loads(fp.read())
                if not skill_data.get('title') and not skill_data.get('name'):
                    raise ValueError('skill.json 缺少 title 或 name 字段')
                if not skill_data.get('prompt'):
                    raise ValueError('skill.json 缺少 prompt 字段')
            elif skill_md:
                with open(skill_md, 'r', encoding='utf-8') as fp:
                    md_content = fp.read()
                skill_data = self._parse_skill_md(md_content)
            else:
                raise ValueError('仓库中未找到 SKILL.md 或 skill.json 文件')

            skill_data['source'] = 'imported'
            skill_data['github_url'] = url
            skill = self.add(skill_data)
            logger.info(f"Skill 从 GitHub 导入成功: {skill.get('title')} ({skill.get('name')})")
            return skill

        except subprocess.TimeoutExpired:
            raise ValueError('Git 克隆超时，请检查网络连接或仓库地址')
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f'导入失败: {e}')
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _import_from_zip_bytes(self, zip_bytes: bytes, github_url: str = '') -> Dict[str, Any]:
        """从 zip 字节流导入（支持 GitHub 下载和直接上传）。"""
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                skill_json_path = None
                skill_md_path = None
                skill_md_root_level = 999  # 记录找到的 SKILL.md 的路径深度

                for name in zf.namelist():
                    basename = name.split("/")[-1] if "/" in name else name
                    # 计算路径深度（斜杠数）
                    depth = name.count("/")

                    if basename == "skill.json" and skill_json_path is None:
                        skill_json_path = name
                    elif basename == "SKILL.md":
                        # 优先选择根目录或最浅层的 SKILL.md
                        if depth < skill_md_root_level:
                            skill_md_path = name
                            skill_md_root_level = depth

                if skill_json_path:
                    with zf.open(skill_json_path) as f:
                        content = f.read().decode("utf-8")
                        skill_data = json.loads(content)
                    if not skill_data.get("title") and not skill_data.get("name"):
                        raise ValueError("skill.json 缺少 title 或 name 字段")
                    if not skill_data.get("prompt"):
                        raise ValueError("skill.json 缺少 prompt 字段")
                elif skill_md_path:
                    with zf.open(skill_md_path) as f:
                        md_content = f.read().decode("utf-8")
                    skill_data = self._parse_skill_md(md_content)
                else:
                    raise ValueError("压缩包中未找到 skill.json 或 SKILL.md 文件")

                skill_data['source'] = 'imported'
                if github_url:
                    skill_data['github_url'] = github_url
                skill = self.add(skill_data)
                logger.info(f"Skill 导入成功: {skill.get('title')} ({skill.get('name')})")
                return skill

        except zipfile.BadZipFile:
            raise ValueError("不是有效的 zip 文件")
        except json.JSONDecodeError as e:
            raise ValueError(f"skill.json 解析失败: {e}")
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"导入失败: {e}")

    @staticmethod
    def _parse_skill_md(md_content: str) -> Dict[str, Any]:
        """解析 SKILL.md 格式（YAML frontmatter + Markdown body）。

        提取 name、description 从 frontmatter，整个 Markdown 内容作为 prompt。
        """
        frontmatter: Dict[str, Any] = {}
        body = md_content

        # 解析 YAML frontmatter（--- 分隔）
        if md_content.startswith('---'):
            parts = md_content.split('---', 2)
            if len(parts) >= 3:
                yaml_str = parts[1].strip()
                body = parts[2].strip()
                # 简单解析 YAML（避免依赖 PyYAML）
                for line in yaml_str.split('\n'):
                    line = line.strip()
                    if ':' in line and not line.startswith('#'):
                        key, _, val = line.partition(':')
                        key = key.strip()
                        val = val.strip().strip('"').strip("'")
                        if key and val:
                            frontmatter[key] = val

        name = frontmatter.get('name', '')
        description = frontmatter.get('description', '')
        title = name or '未命名技能'

        # 从 Markdown body 提取标题作为 title 备选
        for line in body.split('\n'):
            line = line.strip()
            if line.startswith('# ') and not title or title == name:
                title = line[2:].strip()
                break

        if not title:
            title = frontmatter.get('name', '导入的 Skill')

        # 整个 Markdown 内容作为 prompt
        return {
            'name': name or title,
            'title': title,
            'description': description or f'从 SKILL.md 导入的技能：{title}',
            'prompt': body,
            'inputs': [],
            'outputs': [],
            'tools': [],
        }


# 全局单例
def get_skill_manager() -> SkillManager:
    return SkillManager()
