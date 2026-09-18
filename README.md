# 个人抖音 AI 客服工作台

这是一个在 Windows 本地运行的个人抖音私信 AI 回复系统。抖音渠道负责读取和回复私信，
FastAPI 后端提供 Triage/Handoff Agent、知识库、客户长期记忆、投递租约和人工接管。

## 运行链路

```text
个人抖音私信页
  → 每账号独立 Chrome Profile / Playwright 扫描
  → DouyinAdapter（去重、会话映射、发送回执）
  → ChannelHub / Pipeline
  → FastAPI /v1/chat
  → AI 自动回复或待人工
```

当前接入使用可审计的网页 DOM：接收依赖未读会话和消息气泡，发送后必须在页面中看到同文的
本人消息才记为成功。抖音网页 DOM 可能变化，上线前必须使用测试账号验证选择器。

## 从 GitHub 恢复并运行

GitHub 保存的是可公开的源码、依赖清单、数据库迁移和安全配置模板，不保存真实密钥、账号、
客户数据库、浏览器登录态或本机 Python 环境。全新克隆后运行一次安装脚本，即可重建这些可重建
部分；真实 API Key 和管理员密码会隐藏输入，只写入被 Git 忽略的本地文件。

### 前置条件

- Windows 10/11 64 位；
- 可访问 GitHub、PyPI 和模型供应商；
- Windows Package Manager（`winget`）；
- Google Chrome（个人抖音网页接入使用系统 Chrome）。

### 全新安装

```powershell
git clone https://github.com/dengpeihua/AI-Customer-Service.git
Set-Location .\AI-Customer-Service
Set-ExecutionPolicy -Scope Process Bypass
& .\scripts\Setup-AICustomerService.ps1
```

安装脚本会：

1. 通过 `winget` 安装 `uv`（已安装时复用），并下载项目专用 Python 3.11；
2. 创建 `.venv` 和 `mem/.venv`，安装两个已验证的 Windows/Python 3.11 依赖锁定文件；
3. 从示例文件创建 `.env`、`mem/.env`、`widget_config.yaml`、`douyin_accounts.yaml`；
4. 自动生成 JWT、Bootstrap 和字段加密密钥，密钥不会显示在终端；
5. 隐藏询问 MiniMax/DeepSeek API Key；
6. 执行全部 Alembic 数据库迁移；新数据库会隐藏询问管理员密码，并用 Windows DPAPI 保存登录凭据；
7. 验证 FastAPI、SQLAlchemy、PySide6、Playwright 和本地 Agents 包可以导入。

API Key 属于外部账户凭据，GitHub 无法也不应该替你保存。若安装时暂时留空，可稍后编辑
`.env`，然后重新运行配置检查：

```powershell
& .\.venv\Scripts\python.exe .\scripts\configure_local.py --check-only
```

出现 `configuration=READY` 后启动：

```powershell
& .\scripts\Start-AICustomerService.ps1
```

启动器兼容两种环境：本地交付包优先使用 `runtime/python`，GitHub 源码安装使用 `.venv`。

## GitHub 没有上传什么

这些路径由 `.gitignore` 明确排除。它们并非漏传源码，而是密钥、机器环境、运行状态或用户数据。

| 本地路径 | 内容 | 删除后如何恢复 |
|---|---|---|
| `.env` | 模型 API Key、JWT/Bootstrap/字段加密密钥 | 从 `.env.example` 重建；API Key 需从供应商或密码管理器重新取得 |
| `mem/.env` | 本地记忆服务可选覆盖项 | 从 `mem/.env.example` 重建；默认复用根目录 `.env` |
| `widget_config.yaml` | 后端地址、管理员账号、DPAPI 密文、发送开关 | 从模板重建；换机器后重新加密管理员密码 |
| `douyin_accounts.yaml`、`*_accounts.yaml` | 平台账号映射、身份指纹和本地开关 | 从示例重建，或从离线加密备份恢复 |
| `acs.db`、`*.bak` | 租户、用户、会话、客户、知识库和投递记录 | 新安装会创建空库；历史业务数据只能从备份恢复 |
| `data/`、`mem/data/` | 渠道状态、本地向量和记忆索引 | 可重新生成的部分会按运行流程创建；历史状态需备份 |
| `runtime/`、`.venv/`、`mem/.venv/`、`mem/.python/` | Python 解释器和第三方包 | 运行 `Setup-AICustomerService.ps1` 重建 |
| `logs/`、`__pycache__/`、`.pytest_cache/` | 日志和缓存 | 无需备份，运行时自动生成 |
| `%LOCALAPPDATA%\AI-Customer-Service\...\chrome-profile` | 每个抖音账号的 Chrome 登录态 | 通常重新登录最安全；它本来就不在仓库目录内 |
| `*.key`、`*.pem`、证书和凭据导出 | 私钥材料 | 只能从安全离线备份或签发方恢复，禁止提交 Git |

## 配置说明

### `.env`

默认组合是 MiniMax-M3 对话与 `embo-01` 向量化：

- `LLM_PROVIDER=minimax`
- `MINIMAX_API_KEY`：必填；
- `MINIMAX_API_BASE`、`MINIMAX_EMBEDDING_BASE_URL`：保持官方 HTTPS 地址；
- `MINIMAX_MODEL=MiniMax-M3`、`MINIMAX_EMBED_MODEL=embo-01`；
- `DEEPSEEK_API_KEY`：本地 Mem0 记忆抽取需要，缺失时主客服仍可运行但记忆服务会降级；
- `JWT_SECRET`、`BOOTSTRAP_TOKEN`、`FIELD_ENC_KEY`：安装脚本自动生成，不要复制到文档、聊天或 Git；
- `DATABASE_URL`：默认使用项目根目录的 `acs.db`；
- `ADMIN_COOKIE_SECURE=false`：仅适用于当前 `127.0.0.1` 本地运行方式。

生产或真实客户使用时，应设置 `APP_ENV=prod`。后端会拒绝使用公开占位值或过弱密钥启动。

### `widget_config.yaml`

- `backend_base_url`：默认 `http://127.0.0.1:8000`；
- `tenant_id`、`login`：安装脚本在创建新管理员后自动填写；
- `password_enc`：Windows DPAPI 密文，只能由同一 Windows 用户在同一台机器解密；
- `auto_send`：全局自动发送开关，首次配置必须保持 `false`；
- `scope`、`notifications`：控制接待范围和通知行为。

需要重新生成管理员密码密文时，使用隐藏输入模式：

```powershell
& .\.venv\Scripts\python.exe -m widget.secret
```

不要把密码作为命令行参数，因为它会出现在进程列表或终端历史中。

### `douyin_accounts.yaml`

每个账号至少配置：

- 唯一的 `account_id`；
- 与后端一致的 `tenant_id`、`login`、`password_enc`；
- 独立 `profile_dir` 或留空使用默认目录；
- 首次登录后显示的 `expected_identity_fingerprint`；
- `enabled` 与 `send_enabled`。示例默认都关闭，完成测试账号联调后再逐个开启。

不要填写或导出 Cookie。登录状态只保存在本地 Chrome Profile 中。

### `mem/.env`

默认不重复保存模型密钥。记忆服务先读取根目录 `.env`，`mem/.env` 只用于覆盖
`MEM0_*`、`EMBEDDING_DIMS`、`COLLECTION_NAME` 等本地记忆配置。`scripts/run_mem0.py`
会把数据目录固定到当前源码目录的 `mem/data`。

## 删除本地项目前的两种选择

### 只需要以后重新使用功能

确认 GitHub 已包含最新提交后，可以删除本地目录。以后按“全新安装”操作，会得到新的数据库、
新的管理员和新的浏览器登录态，不会恢复旧客户与会话数据。

### 需要保留当前业务状态

删除前停止程序，并把下面内容复制到加密磁盘或加密备份中；不要上传 GitHub：

```text
.env
mem/.env
widget_config.yaml
*_accounts.yaml
acs.db
acs.db*.bak
data/
mem/data/
%LOCALAPPDATA%\AI-Customer-Service\
```

恢复时先克隆源码，再把备份复制回相同位置，然后运行安装脚本。脚本不会覆盖已存在的配置或
数据库，只会补环境、依赖并执行向前兼容的数据库迁移。跨机器恢复时，DPAPI 密文和 Chrome
登录态通常不可用，需要重新输入管理员密码或重新登录平台账号。

## 安全边界

- `widget_config.yaml` 的 `auto_send` 与账号的 `send_enabled` 必须同时为 `true` 才会自动发送；
- 示例中的两个开关都为 `false`；
- 首次成功扫描默认只建立基线，不处理已有未读消息；
- DOM 点击不等于发送成功，只有页面回读确认后才更新投递状态；
- 不在 YAML、数据库或日志中保存抖音 Cookie；
- 这是个人账号网页自动化，不是抖音官方开放平台接口，存在页面改版、风控和账号限制风险。

## 备用 GLM 模型

启用 GLM 时：

1. 停止后端并备份 `acs.db`；
2. 在本地 `.env` 中配置 `GLM_API_KEY`、`GLM_API_BASE`、`GLM_MODEL=glm-4.7`、
   `GLM_EMBED_MODEL=embedding-3`；
3. 将 `LLM_PROVIDER` 改为 `glm`，把 `LLM_EMBEDDING_DIMENSION` 改为受支持的维度；
4. 运行 `& .\.venv\Scripts\python.exe -m app.kb.reindex` 重建知识库向量索引；
5. 用已审核知识库样本重新校准 `RAG_DISTANCE_CUTOFF`。

不同模型的向量不能混用；切换回来时同样需要重建或恢复匹配的索引备份。

## 测试

源码安装完成后运行：

```powershell
& .\.venv\Scripts\python.exe -m pytest .\tests -q
```

本地交付包也可以使用：

```powershell
& .\runtime\python\python.exe -m pytest .\tests -q
```

自动化测试覆盖配置校验、首次基线、去重、发送双开关、确认回执和状态恢复，但不能替代真实
抖音账号联调。
