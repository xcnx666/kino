# Kino

Kino 是一个开源的 AI 视频 Agent 平台。通过与 AI 导演对话，自动完成内容分析、剧本生成、分镜规划、关键帧生成、视频生成、语音合成与 FFmpeg 合成，输出完整 MP4 视频。

> **Kino** = **Ki**netic **No**nlinear — 动态非线性视频创作

## 快速开始

### 环境要求

- Python 3.10+
- FFmpeg（视频合成必需）
- 至少一个 LLM API Key（OpenAI 兼容 或 Anthropic）

### Docker 部署（推荐）

```bash
git clone https://github.com/xcnx666/kino.git
cd kino
docker-compose up -d --build
```

启动后访问 http://localhost:8000 即可使用。Docker 镜像已内置 FFmpeg，`uploads/`、`output/`、`config/`、`sessions/` 目录通过 volume 挂载持久化。

```bash
# 查看日志
docker-compose logs -f

# 停止服务
docker-compose down
```

### 本地部署

```bash
git clone https://github.com/xcnx666/kino.git
cd kino
pip install -r requirements.txt

# 配置环境变量（可选，也可在 Web 界面配置）
cp .env.example .env  # 编辑 .env 填入 API Key

# 启动服务
python3 run_web.py
```

浏览器打开 http://localhost:8000

##生成案例
![生成案例](script/e7f16182e96110a4f9d1b76532bb6651_raw.mp4)


## 核心特性

**1. 对话式 AI 导演** — 以聊天界面为核心，通过自然语言描述需求，Agent 自主规划并执行视频生成全流程。Agent 具备导演思维：分析素材、撰写剧本、设计分镜、编写专业 Prompt、调度生产流水线。

**2. 多模型独立配置** — LLM、文生图、文生视频、TTS 四类模型独立管理，前端设置页面自由添加/切换。支持 OpenAI 兼容 API、Anthropic Claude、Agnes、Edge TTS 等多种服务。

**3. Agent 工具系统** — 所有 AI 能力封装为独立 Tool（文生图、文生视频、TTS、FFmpeg 合成、文件操作、Bash 等），Agent 通过 function-calling 自主调用，支持错误重试与自动修正。

**4. 流式响应与实时反馈** — WebSocket 实时推送 LLM 生成的 token 和工具调用状态，工具调用以卡片形式可视化展示，支持中断生成。

**5. 素材管理与视频库** — 每个会话独立管理上传素材（图片/视频/文本），Agent 自动读取素材内容用于创作。所有生成的素材自动保存到视频库，按项目文件夹组织。

**6. 多会话并行** — 支持同时运行多个会话，切换会话不会中断正在进行的生成任务。对话历史自动持久化，关闭重开不丢失。

## 使用指南

### 基本流程

1. **新建对话**：点击侧边栏「+ 新建对话」
2. **上传素材**（可选）：切换到素材库标签，拖拽上传参考图片、剧本文本等
3. **描述需求**：在输入框中描述你想要的视频内容，例如：
   - "帮我生成一段关于太空探索的短视频，包含火箭发射和星际旅行"
   - "制作一个 30 秒的产品宣传片，主题是智能手表"
   - "上传的图片是产品参考图，请基于这个风格生成广告视频"
4. **查看过程**：Agent 会流式输出思考过程，工具调用以卡片形式展示
5. **持续对话**：可以追问、修改需求，Agent 会根据上下文继续工作
6. **查看视频库**：点击侧边栏「🎬 视频库」查看所有生成的素材

### 输入操作

| 快捷键 | 功能 |
|--------|------|
| Enter | 发送消息 |
| Shift+Enter | 换行 |

### 模型配置

点击左下角「⚙️ 模型配置」，在设置页面添加/编辑/删除/激活配置。

| 类型 | 支持的 Provider | 说明 |
|------|----------------|------|
| LLM 大模型 | OpenAI 兼容 / Anthropic | GPT 系列或 Claude 系列模型 |
| 文生图 | OpenAI 兼容 / Agnes | 标准 `/images/generations` 接口，兼容 DALL-E、Stability、CogView 等 |
| 文生视频 | Agnes / 通用异步轮询 / 通用同步 | 兼容 Agnes、MiniMax、Runway 等服务 |
| TTS 语音 | Edge TTS / OpenAI 兼容 / Agnes | Edge TTS 免费，无需 API Key |

## 架构设计

### 核心流程

```
用户输入 → 素材分析 → 剧本生成 → 分镜规划 → Prompt 生成
  → 关键帧生成（文生图）→ 视频生成（图生视频）→ 语音合成（TTS）
  → 资产下载 → FFmpeg 合成 → 输出 MP4
```

### Agent 循环

```
用户消息 → LLM 推理 → 有工具调用？
  ├─ 是 → 执行工具 → 结果加入历史 → 继续推理
  └─ 否 → 输出最终回复
```

### 技术栈

| 层级 | 技术 |
|------|------|
| 前端 | 原生 HTML/CSS/JS，WebSocket 流式通信 |
| 后端 | FastAPI + Uvicorn，WebSocket 实时推送 |
| LLM | OpenAI 兼容 API + Anthropic Messages API |
| 媒体生成 | 通用 OpenAI 兼容接口（文生图）、通用异步/同步接口（文生视频）、Edge TTS |
| 视频合成 | FFmpeg |
| 部署 | Docker + Docker Compose |

### 项目结构

```
kino/
├── agent/              # Agent 核心（Agent 循环、工具执行）
├── core/               # 核心基础设施（配置管理、工具基类）
├── llm/                # LLM 客户端（OpenAI 兼容 + Anthropic）
├── media_tools/        # 媒体工具（文生图/文生视频/TTS/FFmpeg/帧提取/文件操作）
├── pipeline/           # 视频生成流水线编排
├── prompt/             # 提示词管理（系统提示词、ReAct/Plan 模板）
├── providers/          # 第三方 API 客户端
├── tools/              # 基础工具（bash/read/write/edit）
├── web/                # Web 服务（FastAPI 后端 + 前端界面）
├── config/             # 模型配置（JSON 持久化）
├── uploads/            # 用户上传素材
├── output/             # 生成的视频输出
├── sessions/           # 会话历史持久化
├── Dockerfile          # Docker 构建文件
├── docker-compose.yml  # Docker Compose 编排
└── requirements.txt    # Python 依赖
```

## API 文档

启动服务后访问 http://localhost:8000/docs 查看完整 API 文档。

### 主要接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/chat/sessions` | 创建聊天会话 |
| GET | `/api/chat/sessions` | 列出所有会话 |
| GET | `/api/chat/sessions/{id}` | 获取会话详情 |
| DELETE | `/api/chat/sessions/{id}` | 删除会话 |
| WS | `/ws/chat/{id}` | 聊天 WebSocket（流式推送） |
| GET | `/api/models` | 获取模型配置 |
| POST | `/api/models/{category}` | 添加模型配置 |
| PUT | `/api/models/{category}/{id}` | 更新模型配置 |
| DELETE | `/api/models/{category}/{id}` | 删除模型配置 |
| POST | `/api/models/{category}/{id}/activate` | 激活模型配置 |
| POST | `/api/upload` | 上传素材文件 |
| GET | `/api/library` | 获取视频库内容 |
| GET | `/api/health` | 健康检查 |

### WebSocket 消息格式

客户端发送：
```json
{ "content": "用户消息内容" }
```

服务端推送：
```json
{ "type": "token", "content": "流式文本片段" }
{ "type": "tool_call", "name": "generate_image", "arguments": {...} }
{ "type": "tool_result", "name": "generate_image", "success": true, "data": {...} }
{ "type": "done", "content": "完整回复内容" }
{ "type": "error", "message": "错误信息" }
```

## 环境变量配置

创建 `.env` 文件（首次启动会自动读取并生成 `config/models.json`）：

```env
# LLM 配置（OpenAI 兼容，必需其一）
api_key=your_llm_api_key
base_url=https://api.example.com/v1
model=your-model-name

# Anthropic 配置（可选，用于 Claude 系列模型）
ANTHROPIC_API_KEY=your_anthropic_api_key

# Agnes API（文生图/文生视频，可选）
AGNES_API_KEY=your_agnes_api_key
```

也可启动后在 Web 界面的「模型配置」页面中添加。

## Contributing

欢迎提交 Issue 和 Pull Request。

## License

MIT
