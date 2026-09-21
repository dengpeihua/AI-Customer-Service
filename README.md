# Salso：多 Agent 智能客服系统

Salso 是一套智能客服系统，包含 FastAPI 多 Agent 后端和 Vue 3 网页前端。我们把意图识别、领域 Agent、Skills、工具调用、RAG、三级记忆、监控和评测串成了一条可运行的业务链路。

项目地址：[dengpeihua/Salso](https://github.com/dengpeihua/Salso)。

## 项目组成

```text
项目根目录/
├─ backend/      # FastAPI、多 Agent、记忆、知识库、监控和评测
├─ frontend/     # Vue 3 + Vite 客服操作界面
└─ README.md     # 部署、开发和数据恢复说明
```

- [后端说明](backend/README.md)：后端能力、接口、本地开发、测试和故障排查。
- [前端说明](frontend/README.md)：前端开发、代理规则和单独构建方式。

## 整体架构

```text
浏览器
  -> Vue 3 前端
  -> Nginx 网关 /api/python
  -> FastAPI
  -> 意图识别与 Agent 路由
  -> 领域 Agent + Skills + MCP/RAG 工具
  -> Redis 工作记忆 + ChromaDB 情景记忆/用户画像
  -> 响应、监控、追踪与评测
```

当前运行链路只有 Vue 前端和 Python 后端，不依赖 Java 服务。

当前关键约束：情景记忆只在同一用户范围内召回；RAG 由业务 Agent 按需调用共享工具并按稳定文档 ID 去重；正常工具往返会记录 trace；监控告警按 `metric + label` 去重并在持续恢复后关闭；高风险客服场景由确定性收尾规则补齐必要信息和能力边界。

## 仓库边界与本地文件重建

我们只提交能够审查和复现的源码、配置模板与锁文件。密钥、第三方依赖副本、虚拟环境、数据库、日志和构建产物都留在本机或部署环境中，避免泄露凭据，也避免把可重新生成的大文件写入 Git 历史。

| 未提交内容 | 原因 | 重建方式 |
| --- | --- | --- |
| `backend/.env` | 包含 API Key、密码和环境差异配置 | 复制 `backend/.env.example` 为 `backend/.env`，再填写自己的值 |
| `backend/.venv` | 与操作系统和 Python 安装绑定 | `py -3.12 -m venv backend/.venv`，激活后安装 `backend/requirements.txt` |
| `frontend/node_modules` | 可由锁文件确定性恢复 | 在 `frontend` 中执行 `npm ci` |
| `frontend/dist` | 前端构建产物 | 在 `frontend` 中执行 `npm run build` |
| `backend/data`、`backend/logs` | 数据库、评测基线和运行日志 | 首次启动时自动创建；本地运行也可按需创建空目录 |
| Redis、ChromaDB、Prometheus 数据 | 属于运行状态，不属于源码 | Docker Compose 首次启动时自动创建命名卷；迁移时应单独备份和恢复数据卷 |
| `__pycache__`、测试缓存、IDE 配置 | 本机缓存或编辑器状态 | Python、测试工具或 IDE 会自动重新生成 |

快速恢复开发依赖：

```powershell
Copy-Item backend/.env.example backend/.env

py -3.12 -m venv backend/.venv
backend/.venv/Scripts/python.exe -m pip install -r backend/requirements.txt

Set-Location frontend
npm ci
Set-Location ..
```

首次启动不需要手工创建数据库文件。Docker Compose 会创建 Redis、ChromaDB 和应用数据卷；这些新卷是空数据环境，不会包含任何未上传的历史会话、知识库或评测结果。

## 一、安装前准备

最省事的运行方式是 Docker 全栈部署。请先准备：

- Docker Desktop，并确认 `docker compose version` 可以正常执行。
- 一个 Anthropic 官方或 Anthropic 兼容接口的 API Key。
- 空闲端口：80、8000、8001 和 5174。

如果需要本地开发，还需要：

- Python 3.12
- Node.js 22；Vite 7 也支持 Node.js 20.19 及以上版本
- npm

## 二、配置模型

先从安全模板生成本地配置：

```powershell
Copy-Item backend/.env.example backend/.env
```

然后编辑 `backend/.env`，至少填写以下内容：

```dotenv
ANTHROPIC_API_KEY=replace_with_your_api_key
ANTHROPIC_BASE_URL=
ANTHROPIC_MODEL=claude-3-5-sonnet-20241022
REDIS_PASSWORD=replace_with_a_strong_password
FRONTEND_PORT=5174
```

- 使用 Anthropic 官方接口时，`ANTHROPIC_BASE_URL` 可以留空。
- 使用兼容接口时，填写服务商提供的基础地址，并把 `ANTHROPIC_MODEL` 改为该服务实际支持的模型名。
- `FRONTEND_PORT` 用于修改前端直连端口；默认是 5174。
- `backend/.env.example` 只保存变量名和安全示例；不要把真实 API Key、密码或 Webhook 提交到版本库。

## 三、启动完整项目

在项目根目录执行：

```powershell
docker compose --env-file backend/.env -f frontend/docker-compose.yml up -d --build
docker compose --env-file backend/.env -f frontend/docker-compose.yml ps
```

启动后可以访问：

| 用途 | 地址 |
| --- | --- |
| 统一网页入口 | <http://localhost> |
| 前端直连入口 | <http://localhost:5174> |
| 后端 API | <http://localhost:8000> |
| Swagger 文档 | <http://localhost:8000/docs> |
| 健康检查 | <http://localhost:8000/health> |
| ChromaDB | <http://localhost:8001> |

检查后端是否就绪：

```powershell
Invoke-RestMethod http://localhost:8000/health
```

查看日志：

```powershell
docker compose --env-file backend/.env -f frontend/docker-compose.yml logs -f salso-python
```

停止完整项目：

```powershell
docker compose --env-file backend/.env -f frontend/docker-compose.yml down
```

加上 `-v` 会同时删除 Redis、ChromaDB 和应用数据卷，请只在确认不再需要本地数据时使用。

### 日志、缓存与评测数据清理

完整项目使用 Docker 命名卷保存运行数据，`backend/logs` 默认不会保存每次请求的日志。主要数据位置如下：

| 内容 | 保存位置 | 生命周期 |
| --- | --- | --- |
| 后端请求日志、Uvicorn 日志 | Docker 容器标准输出 | 删除并重建容器后清空 |
| 当前会话消息和会话摘要 | Redis 卷 `frontend_redis-data` | 默认 24 小时过期，删除数据卷后立即清空 |
| 用户画像、情景记忆和知识库 | ChromaDB 卷 `frontend_chromadb-data` | 持久保存，删除数据卷后清空 |
| 工具调用 trace、意图缓存、工具缓存和本轮评测历史 | Python 后端进程内存 | 重启或重建后端容器后清空 |
| 最新评测基线 | `frontend_salso-python-data` 卷中的 `/app/data/eval/baseline.json` | 每次评测覆盖，删除数据卷后清空 |
| 页面聊天记录和评测展示 | 浏览器页面内存 | 刷新页面后清空 |

从旧版本升级时，部分 Docker 资源名称和浏览器设置键会变化。已有部署应先备份 Redis、ChromaDB 和评测基线，再启动 Salso 并确认数据恢复；不要用 `down -v` 清理旧数据卷。

页面中的“清空”按钮只会清除当前页面消息并生成新的会话 ID，不会删除 Redis、ChromaDB 或 Docker 日志。

只清除 Docker 日志和后端进程内缓存，同时保留 Redis 对话、ChromaDB 知识库及评测基线：

```powershell
docker compose --env-file backend/.env -f frontend/docker-compose.yml down
docker compose --env-file backend/.env -f frontend/docker-compose.yml up -d
```

彻底恢复为全新数据环境：

```powershell
docker compose --env-file backend/.env -f frontend/docker-compose.yml down -v
docker compose --env-file backend/.env -f frontend/docker-compose.yml up -d --build
```

第二组命令会永久删除当前全栈编排的 Redis 对话、用户画像、情景记忆、已上传知识库和评测基线。执行前应确认这些数据不再需要；源代码、`backend/.env` 和 `backend/skills` 不会被删除。

## 四、仅启动后端

如果只需要 API、Prometheus 和后端 Nginx，在项目根目录执行：

```powershell
docker compose --env-file backend/.env -f backend/docker-compose.yml up -d --build
```

这个编排会额外开放 Redis 6379 和 Prometheus 9090。它与全栈编排使用相同端口和容器名，两者不要同时启动。

停止仅后端编排：

```powershell
docker compose --env-file backend/.env -f backend/docker-compose.yml down
```

## 五、本地开发

### 后端

完整步骤见 [后端 README](backend/README.md#本地开发)。核心命令如下：

```powershell
cd backend
docker compose up -d redis chromadb
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
```

本地连接地址和密码从 `backend/.env` 读取；请确保其中的 `REDIS_URL` 与 `REDIS_PASSWORD` 使用同一个密码。

### 前端

保持后端运行，再打开一个终端：

```powershell
cd frontend
npm ci
npm run dev
```

访问 <http://localhost:5173>。Vite 会把 `/api/python/*` 转发到 `http://localhost:8000/*`。

## 六、验证改动

后端：

```powershell
cd backend
python -m pip install pytest
python -m pytest -q
python -m compileall agents api core evaluation mcp memory monitor
docker compose config --quiet
```

前端：

```powershell
cd frontend
npm test
npm run build
docker compose --env-file ../backend/.env config --quiet
```

Compose 配置检查只验证编排文件可以解析；只有实际启动并访问健康检查或页面，才算完成运行时验证。

### 运行 Agent 效果评测

在项目根目录用 PowerShell 执行。评测会实际调用 `backend/.env` 配置的模型服务，需有可用的 API Key；首次启动还会初始化 Redis 和 ChromaDB。

```powershell
docker compose --env-file backend/.env -f frontend/docker-compose.yml up -d --build redis chromadb salso-python
Invoke-RestMethod http://localhost:8000/health

$report = Invoke-RestMethod -Method Post -Uri http://localhost:8000/eval/run -ContentType application/json -Body '{}' -TimeoutSec 600
New-Item -ItemType Directory -Force backend/data/eval | Out-Null
$report | ConvertTo-Json -Depth 30 | Set-Content backend/data/eval/manual-run.json -Encoding utf8
$report | Select-Object total,passed,pass_rate,avg_scores,regressions,recommendations | Format-List
$report.results | Select-Object test_id,passed,scores | Format-Table -Wrap
$report.results | Where-Object { $_.metadata.judge_failed } | Select-Object test_id,metadata
```

如果 8000 端口正被别的服务使用，可在同一 Compose 网络中运行临时容器，不占用宿主机端口。先执行 `docker compose --env-file backend/.env -f frontend/docker-compose.yml up -d redis chromadb`，然后运行：

```powershell
docker compose --env-file backend/.env -f frontend/docker-compose.yml run -d --no-deps --name salso-eval salso-python
docker inspect --format '{{.State.Health.Status}}' salso-eval  # 等待显示 healthy
New-Item -ItemType Directory -Force backend/data/eval | Out-Null
docker exec salso-eval python -c "import urllib.request; r=urllib.request.Request('http://localhost:8000/eval/run',data=b'{}',headers={'Content-Type':'application/json'},method='POST'); print(urllib.request.urlopen(r,timeout=600).read().decode())" | Set-Content backend/data/eval/manual-run.json -Encoding utf8
$report = Get-Content backend/data/eval/manual-run.json -Raw | ConvertFrom-Json
$report | Select-Object total,passed,pass_rate,avg_scores,regressions,recommendations | Format-List
```

完成后可运行 `docker stop salso-eval` 和 `docker rm salso-eval` 清理这个临时容器；评测报告已保存在本机文件中。

默认用例为 **11 条意图识别样本**及 **5 组客服对话（共 7 轮）**。意图样本合并为报告中的一项 `intent_recognition`，其分数包括 Accuracy 和 Macro-F1；7 个对话轮次分别计入报告，通过 LLM-as-Judge 评分相关性、准确性、完整性和有用性。因此默认报告的 `total` 应为 8，`pass_rate` 是 8 项的通过比例，不是 11 条意图样本的准确率。每项通过阈值为 0.75。若任一对话结果的 `metadata.judge_failed` 为 `true`，本次 Judge 调用失败，该轮的 0.5 默认分不能当作有效质量评分。

报告会保存到本机 `backend/data/eval/manual-run.json`（已被 Git 忽略）。服务还会在数据卷的 `/app/data/eval/baseline.json` 保存最新报告；再次运行时，与上一轮的同名平均指标比较，相对下降超过 5% 会列为 `regressions`。每次运行会覆盖该基线，想做严格的版本对比时应另行保存每次报告，并固定模型、知识库、用例及参数。

可使用自定义用例：向 `/eval/run` 发送包含 `intent_cases`、`dialog_cases` 的 JSON；字段格式见 [后端接口](backend/api/main.py) 中的 `EvalRunInput`。这里的 Precision/Recall 是**意图分类**指标。当前项目没有带相关文档标注的检索评测集，也没有计算检索 Recall@5、MRR、NDCG 或 direct/hybrid/rewrite 对比的脚本；`/search` 可检查单次查询的改写检索结果，但不能据此声称检索效果提升。

不调用外部模型的代码回归测试可在服务启动后单独运行，结果应与上述效果评测分开记录：

```powershell
docker compose --env-file backend/.env -f frontend/docker-compose.yml exec -T salso-python python tests/run_focused_tests.py
```

### 本次评测结果

2026-09-21 在 Docker 容器中执行默认 `/eval/run`，使用 `backend/.env` 配置的 **MiniMax-M2.7**，知识库当时有 **6 个文档片段**。由于本机 8000 端口被另一个容器占用，本次将 `salso-python` 作为不发布端口的临时容器 `salso-eval` 运行，并从容器内部请求 `http://localhost:8000/eval/run`。原始 API 响应保存在本机忽略目录 `backend/data/eval/manual-run.json`，没有提交客服回复原文或密钥。

| 项目 | 实测结果 |
| --- | --- |
| 意图识别 | 11 条中 9 条正确；Accuracy **0.8182**，Macro-F1 **0.7222** |
| 客服对话 | 5 组、7 轮；LLM-as-Judge 四维均分：相关性 **0.9400**、准确性 **0.9714**、完整性 **0.9000**、有用性 **0.9214** |
| 报告通过率 | **8/8 = 1.0000**（1 项汇总意图评测 + 7 个对话轮次；阈值 0.75） |
| Judge 调用失败 | **0/7** |
| 回归检测 | `regressions=[]`；这是新数据卷上的首轮评测，没有可比较的历史基线 |
| 代码回归测试 | `python tests/run_focused_tests.py`：**30/30 通过**，与上述模型效果评测分开统计 |
| 检索烟测 | `/search?query=退款多久到账？&top_k=5` 返回 5 条，第一条为“退款政策”，`reranked=true`；这只证明本次检索链路返回了结果 |

两条意图错分：**“帮我取消订单”** 的标注为 `request`，预测为 `order_status`；**“我要投诉，转人工！”** 的标注为 `human_handoff`，预测为 `complaint`。7 个对话轮次的综合分依次为 **0.9000、0.9075、0.9375、0.8750、1.0000、0.9375、0.9750**。报告建议补充低 F1 意图类别的示例。`avg_scores.accuracy` 是 Judge 对回复准确性的主观评分，**不是**意图识别准确率，也未经过人工答案校准；本次没有检索相关性标注集，因而没有 Recall@5、MRR、NDCG 或策略提升数字。单次小样本结果不代表生产环境表现。

## 七、常见问题

- 后端提示缺少密钥：确认 `backend/.env` 存在且 `ANTHROPIC_API_KEY` 不是空值。
- 模型接口报 401 或模型不存在：重新核对 API Key、基础地址和模型名是否来自同一服务。
- 页面能打开但无法对话：先访问后端健康检查，再查看 `salso-python` 日志。
- Docker 报容器名或端口冲突：先停止另一套编排，再重新启动当前方案。
- 首次构建较慢：后端镜像会安装依赖并预下载 ChromaDB 使用的 ONNX 模型。
- 需要更细的接口、配置或故障排查说明：查看 [后端 README](backend/README.md) 和 [前端 README](frontend/README.md)。
