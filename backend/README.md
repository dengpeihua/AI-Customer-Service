# 我们的智能客服后端

我们用 FastAPI 构建了一个可部署、可观测、可评测的多 Agent 智能客服后端。系统先识别用户意图和紧急程度，再把请求路由到通用、技术、账单或人工转接节点；处理过程中可以调用知识库检索、业务 Skills、三级记忆和监控评测能力。

## 核心能力

- 多 Agent 路由：通用客服、技术支持、账单支持和人工转接各自拥有独立职责与工具边界。
- 三级记忆：Redis 保存工作记忆，ChromaDB 保存情景记忆和用户画像；情景记忆严格限制在同一用户内。
- RAG 知识库：由业务 Agent 按需调用共享工具，支持文本写入、文件上传、查询改写、稳定 ID 去重和结果重排。
- Skills：启动时加载 `skills/*/SKILL.md`，也可以通过接口热重载。
- 可观测与评测：提供健康检查、Prometheus 指标、完整工具调用追踪、告警去重/恢复和端到端评测接口。
- Anthropic 兼容模型接口：既可以连接 Anthropic 官方接口，也可以连接实现了相同协议的服务。

## 请求链路

```text
客户端请求
  -> FastAPI
  -> 意图识别与紧急度判断
  -> AgentOrchestrator
  -> 领域 Agent
  -> Skills / MCP 工具 / RAG
  -> Redis + ChromaDB 记忆
  -> 统一响应、监控指标与调用追踪
```

## 环境要求

推荐使用 Docker 运行：

- Docker Desktop 或 Docker Engine
- Docker Compose v2
- 一个 Anthropic 官方或 Anthropic 兼容接口的 API Key

本地运行还需要 Python 3.12。

## 配置

从模板创建本机专用配置：

```powershell
Copy-Item .env.example .env
```

然后编辑 `.env`。最小配置如下：

```dotenv
ANTHROPIC_API_KEY=replace_with_your_api_key
ANTHROPIC_BASE_URL=
ANTHROPIC_MODEL=claude-3-5-sonnet-20241022
REDIS_PASSWORD=replace_with_a_strong_password
```

配置说明：

| 变量 | 是否必填 | 说明 |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | 是 | 模型服务密钥。 |
| `ANTHROPIC_BASE_URL` | 否 | Anthropic 官方接口可留空；兼容服务填写其基础地址。 |
| `ANTHROPIC_MODEL` | 是 | 必须填写当前服务实际支持的模型名。 |
| `REDIS_PASSWORD` | 否 | Docker 环境默认使用 `customer-service123`。生产环境应修改。 |
| `LOG_LEVEL` | 否 | 日志级别，默认 `INFO`。 |
| `MONITOR_INTERVAL` | 否 | 性能监控采样间隔，默认 10 秒。 |
| `ALERT_WEBHOOK_URL` | 否 | 异常告警 Webhook；留空则不发送。 |

不要提交包含真实密钥的 `.env`。仓库中的 `.env.example` 只用于说明变量；`.env`、`.venv`、`data`、`logs`、Python 缓存和本地数据库都由 `.gitignore` 排除。

这些未提交内容可以随时重建：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
New-Item -ItemType Directory -Force data/chroma,data/eval,logs | Out-Null
```

Docker 用户不需要手工创建数据库文件；`docker compose up` 会自动创建 Redis、ChromaDB 和 Prometheus 数据卷。新建数据卷是空环境，如需保留历史会话或知识库，应从自己的备份恢复，而不是把数据库提交到 Git。

## Docker 启动

在 `backend` 目录执行：

```powershell
docker compose up -d --build
docker compose ps
```

首次构建会安装 Python 依赖并下载 ChromaDB 使用的 ONNX 模型，因此耗时会比后续构建长。

常用地址：

| 服务 | 地址 |
| --- | --- |
| Nginx 统一入口 | <http://localhost> |
| 后端 API | <http://localhost:8000> |
| Swagger 文档 | <http://localhost:8000/docs> |
| 健康检查 | <http://localhost:8000/health> |
| ChromaDB | <http://localhost:8001> |
| Prometheus | <http://localhost:9090> |

查看日志和停止服务：

```powershell
docker compose logs -f customer-service
docker compose down
```

如需同时启动网页前端，请从项目根目录按根目录 [README](../README.md) 的全栈方式启动，不要与本编排同时运行，以免容器名和端口冲突。

## 本地开发

先启动 Redis 和 ChromaDB：

```powershell
docker compose up -d redis chromadb
```

再创建 Python 环境并启动 API：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
```

本地连接地址、密码和模型配置会从 `.env` 自动加载。请确保其中的 `REDIS_URL` 与 `REDIS_PASSWORD` 使用同一个密码。

## 主要接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/chat` | 多 Agent 对话入口 |
| `GET` | `/skills` | 查看已加载 Skills |
| `POST` | `/skills/reload` | 热重载 Skills |
| `POST` | `/search` | 知识库检索 |
| `POST` | `/knowledge/add` | 写入知识片段 |
| `POST` | `/knowledge/upload` | 上传知识文件 |
| `GET` | `/knowledge/stats` | 查看知识库统计 |
| `GET` | `/monitor` | 查看运行监控摘要 |
| `GET` | `/metrics` | Prometheus 指标 |
| `GET` | `/trace/tool/{request_id}` | 查询单次工具调用链 |
| `GET` | `/trace/tools` | 查询近期工具调用链 |
| `POST` | `/eval/run` | 运行端到端评测 |

请求和响应字段以 Swagger 文档为准。

## 测试与检查

```powershell
python -m pip install pytest
python -m pytest -q
python tests/run_focused_tests.py
python -m compileall agents api core evaluation mcp memory monitor
docker compose config --quiet
```

`run_focused_tests.py` 不依赖 pytest，但需要先安装 `requirements.txt`；它可验证记忆隔离、RAG 去重/重排、trace、回答完整性和告警生命周期。其余命令检查完整 pytest、Python 语法和 Compose 配置。真正的模型调用还需要有效 API Key 和可访问的模型服务。

## 目录结构

```text
backend/
├─ agents/       # Agent 定义、路由与响应合成
├─ api/          # FastAPI 入口和接口模型
├─ core/         # 意图识别与 Skill 加载
├─ evaluation/   # 端到端评测
├─ mcp/          # 工具管理与知识库
├─ memory/       # 工作记忆、情景记忆和用户画像
├─ monitor/      # 性能监控与指标
├─ skills/       # 业务 Skills
├─ tests/        # 后端测试
├─ config/       # Nginx 与 Prometheus 配置
├─ Dockerfile
└─ docker-compose.yml
```

## 常见问题

- 启动时报 `未设置 ANTHROPIC_API_KEY`：检查 `.env` 是否位于 `backend` 目录，且密钥不是空值。
- 模型返回 401 或模型不存在：核对 API Key、`ANTHROPIC_BASE_URL` 和 `ANTHROPIC_MODEL` 是否属于同一服务。
- MiniMax 等推理型兼容模型会先消耗 thinking token；意图识别和 LLM Judge 的结构化调用已预留 1024 个输出 token，避免 256 token 只返回 thinking、没有最终 JSON。该值是上限，不代表每次都会消耗满额。
- 8000、8001、9090 或 80 端口被占用：先停止占用端口的服务，或修改 `docker-compose.yml` 的宿主机端口。
- Docker 服务一直不健康：运行 `docker compose ps` 和 `docker compose logs -f customer-service` 查看具体错误。
- 本地开发无法连接 Redis 或 ChromaDB：确认依赖容器已启动，并使用宿主机地址 `localhost:6379` 和 `localhost:8001`。
