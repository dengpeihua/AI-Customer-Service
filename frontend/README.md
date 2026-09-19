# 我们的 AI 客服前端

我们使用独立的 Vue 前端连接同级 `backend` 目录中的 Python 后端。

项目目录：

```text
./frontend
```

## 功能

- 使用 Python 后端，适配 `/chat` 的 `conv_id`、`agent_type`、`latency_ms` 等响应字段。
- 支持聊天调试、健康检查、监控摘要、知识库检索、知识库文档导入、文件上传。
- 支持展开查看工具输入与 trace，并运行端到端评测。
- 支持 Docker + Nginx 部署。

## 默认后端地址

| 后端 | 默认地址 |
|------|----------|
| Python | `http://localhost:8000` |

开发模式下，Vite 会代理：

| 前端路径 | 代理到 |
|----------|--------|
| `/api/python` | `http://localhost:8000` |

Docker Compose 模式下，网关将 `/api/python/` 转发到 Python 容器。单独运行前端容器时，内置 Nginx 将该路径转发到宿主机的 Python 服务。

端到端评测包含多次真实模型调用，可能超过一分钟。网关和前端 Nginx 的 API `proxy_send_timeout`、`proxy_read_timeout` 均为 300 秒，避免长评测被默认 60 秒超时截断。

## 本地运行

先启动 Docker Desktop，再在项目根目录启动 Python 后端及 Redis、ChromaDB：

```bash
docker compose -f backend/docker-compose.yml up -d customer-service
```

确认 `http://localhost:8000/health` 可访问后，进入 `frontend` 目录执行下面的命令。`npm run dev` 只启动前端；后端未启动时，Vite 会报告 `ECONNREFUSED`。

安装依赖：

```bash
npm ci
```

`node_modules` 不提交到仓库，`npm ci` 会依据 `package-lock.json` 完整重建依赖。`dist` 也不提交；需要静态构建产物时执行 `npm run build`，Vite 会重新生成该目录。

启动：

```bash
npm run dev
```

访问：

```text
http://localhost:5173
```

如果后端端口不是默认值，可以启动时覆盖：

```bash
VITE_PYTHON_API_URL=http://localhost:8000 \
npm run dev
```

## Docker 部署

在 `frontend` 目录下构建并启动 Python 后端、前端、Redis、ChromaDB 和 Nginx 网关，使用后端 `.env` 中的配置：

```bash
docker compose --env-file ../backend/.env up -d --build
```

需要以下同级目录：

```text
项目根目录/
├── backend/
└── frontend/
```

访问前端：

```text
http://localhost
```

如果只想暴露前端端口，可改 `FRONTEND_PORT`，默认仍可通过 `80` 统一入口访问。

停止：

```bash
docker compose --env-file ../backend/.env down
```

## 后端启动参考

Python 版默认：

```text
http://localhost:8000
```

前端固定使用 Python。旧版保存的其他后端选择会自动迁移到 Python，并清空跨后端的会话 ID；已有 Python 用户配置和会话保持不变。
