# 个人抖音 AI 客服工作台

这是一个 Windows 本地运行的个人抖音私信 AI 回复系统。抖音渠道负责读取和回复私信，现有
FastAPI 后端继续提供 Triage/Handoff Agent、知识库、客户长期记忆、投递租约和人工接管。

## 运行链路

```text
个人抖音私信页
  → 每账号独立 Chrome Profile / Playwright 扫描
  → DouyinAdapter（去重、会话映射、发送回执）
  → ChannelHub / Pipeline
  → FastAPI /v1/chat
  → AI 自动回复或待人工
```

本实现参考 `xyc667/douyin-auto-reply-assistant` 的“登录后监听私信并回复”工作流，但没有复制其
非官方 WebSocket、protobuf、签名脚本或 GUI。当前实现使用可审计的网页 DOM 接入：接收依赖未读
会话和消息气泡，发送后必须在页面中看到同文的本人消息才记为成功。抖音网页 DOM 可能变化，
因此上线前必须使用测试账号验证选择器。

## 安全边界

- `widget_config.yaml` 的 `auto_send` 与账号的 `send_enabled` 必须同时为 `true` 才会自动发送。
- 两个开关在示例配置中都默认关闭；此时系统只生成草稿或进入待人工。
- 每个账号使用独立 Chrome Profile。默认 Profile 位于
  `%LOCALAPPDATA%\AI-Customer-Service\douyin\<account_id>\chrome-profile`，不会放进仓库。
- 不在 YAML、数据库或日志中保存抖音 Cookie。后端密码建议使用 Windows DPAPI 密文。
- 首次登录只显示 16 位身份指纹；把它填入账号的 `expected_identity_fingerprint` 并重启后，
  才会收取或发送消息。登录错 Profile 时会强制锁定。
- 首次成功扫描默认只建立基线，不自动处理已有未读消息；失败扫描不会消耗该保护。
- DOM 点击成功不等于发送成功；只有页面回读确认后才更新投递状态。
- 这是个人账号网页自动化，不是抖音官方开放平台接口，存在页面改版、风控和账号限制风险。

## 配置与启动

在仓库根目录执行：

```powershell
Copy-Item .env.example .env
Copy-Item widget_config.example.yaml widget_config.yaml
Copy-Item douyin_accounts.example.yaml douyin_accounts.yaml
```

用 DPAPI 加密后端登录密码，并把输出填入 `douyin_accounts.yaml` 的 `password_enc`：

```powershell
& .\runtime\python\python.exe -m widget.secret "你的后端密码"
```

然后启动一体化工作台：

```powershell
& .\scripts\Start-AICustomerService.ps1
```

每个启用账号会打开自己的 Chrome 窗口。首次运行请手工登录正确的个人抖音账号，在“抖音私信”
页复制显示的身份指纹，填入 `douyin_accounts.yaml` 后重启。登录状态只保留在该账号的本地
Profile 中。确认账号、收信、去重、草稿和人工回复都正常后，再逐步开启真实发送。

也可以分别启动：

```powershell
& .\runtime\python\python.exe .\scripts\run_backend.py
& .\runtime\python\python.exe .\run_widget.py
& .\runtime\python\python.exe .\run_widget_headless.py
```

## 备用 GLM 模型

客服后端默认仍使用 MiniMax-M3 对话和 embo-01（1536 维）向量化。
根目录 `.env` 的 GLM 配置块保持注释时不会启用 GLM；`.env.example` 只包含占位密钥。
GLM 组合为 `glm-4.7` 对话（包括 Triage/Handoff）和 `embedding-3` 知识库向量化，
因为 `glm-4.7` 本身不是向量模型。GLM 请求关闭思考以延续客服低延迟模式。

以后启用时：

1. 停止后端并备份 `acs.db`。
2. 取消本地 `GLM_API_KEY`、`GLM_API_BASE`、`GLM_MODEL`、`GLM_EMBED_MODEL` 的注释，
   将已有的 `LLM_PROVIDER` 改为 `glm`、`LLM_EMBEDDING_DIMENSION` 改为 `2048`。
   不要留下重复生效的同名配置。
3. 在项目根目录运行 `& .\runtime\python\python.exe -m app.kb.reindex` 重建知识库向量索引。
   旧 MiniMax 向量不能与 GLM 向量混用；切回 MiniMax 时也需要重建或恢复匹配的备份。
4. 用已审核知识库样本重新校准 `RAG_DISTANCE_CUTOFF`，再启动后端。

这些开关控制 `app/` 客服后端。独立 Mem0 服务仍使用自己的 `mem/.env` 配置和现有索引，
不会随根目录 `LLM_PROVIDER` 自动切换。本次添加备用配置无需重建任何索引。

接口参考：[GLM-4.7](https://docs.bigmodel.cn/cn/guide/models/text/glm-4.7)、
[Embedding-3](https://docs.bigmodel.cn/cn/guide/models/embedding/embedding-3)。

## 多账号隔离

在 `douyin_accounts.yaml` 中为每个个人号配置不同的 `account_id`。系统使用
`douyin#<account_id>` 作为渠道键，隔离浏览器 Profile、本地会话状态、后端对话、客户记忆、
人工回复和投递记录。不要把原平台的客户标识改名后继续使用；不同平台身份并不等价。

## 测试

```powershell
& .\runtime\python\python.exe -m pytest .\tests -q
```

自动化测试覆盖配置校验、首次基线、稳定去重、发送双开关、确认回执和状态恢复。自动化测试不能替代
真实抖音账号联调；在真实账号完成收发验证前，只能确认代码链路和安全降级逻辑已经通过测试。
