# AI 客服系统客户使用说明

本包是 Windows 本地一体运行版：后端服务运行在 `127.0.0.1:8000`，桌面挂件连接本机微信 hook `127.0.0.1:30001`。

## 运行环境

- Windows 10/11 64 位
- 包内已带 Python 3.11 运行环境，客户机器不需要单独安装 Python
- 个人微信固定为 4.1.10.27，并已部署 WeChat-Hook
- 包内已带微信 4.1.10.27 安装包和 hook DLL
- 使用真实 AI 时需要对应服务的 API Key；DeepSeek 模式使用 DeepSeek 对话和 DashScope embedding。

## 一键启动

1. 解压本包到一个固定目录，例如 `D:\AI-Customer-Service`。
2. 双击 `启动AI客服.bat`。
3. 第一次运行会弹出 Windows 管理员确认，用于安装/降级微信、禁用微信自动更新、部署 hook DLL。
4. 脚本会自动启动微信。扫码登录后不要关闭脚本窗口，它会检测 hook 可读库，然后自动启动后端和挂件。

后台管理地址：`http://127.0.0.1:8000/admin`

默认演示账号：`demo / demo12345`

## 配置

一键脚本会在首次运行时自动从模板生成 `.env` 和 `widget_config.yaml`。

- 演示模式：默认 `LLM_PROVIDER=fake`，不需要联网调用大模型。
- Qwen 对话：编辑 `.env`，改成 `LLM_PROVIDER=dashscope`，填写 `DASHSCOPE_API_KEY`。
- DeepSeek 对话：改成 `LLM_PROVIDER=deepseek`，填写 `DEEPSEEK_API_KEY`、`DEEPSEEK_API_BASE`、
  `DEEPSEEK_MODEL`；知识库向量复用 `DASHSCOPE_API_KEY`、`DASHSCOPE_BASE_URL` 和
  `LLM_EMBED_MODEL=text-embedding-v3`，向量维度使用 `LLM_EMBEDDING_DIMENSION=1024`。
  `DEEPSEEK_THINKING=false` 适合低延迟客服；改成 `true` 会启用更慢的思考模式。
- 正式使用前务必更换 `JWT_SECRET`、`BOOTSTRAP_TOKEN`；生产部署还要设置 `APP_ENV=prod` 和 `FIELD_ENC_KEY`。
- `widget_config.yaml` 里 `auto_send` 默认关闭。确认微信号、知识库和回复内容都正确后，再改为 `true`。

### Triage Agent 路由

系统使用包内 `agents`（OpenAI Agents SDK）运行一个 Triage Agent。Triage 会结合当前消息
和同一客户最近的对话历史，只选择一个 Handoff：

- `transfer_to_after_sales_agent`：商品、订单、物流、发票、退款退货规则、换货、维修、保修、
  投诉等需要业务事实的问题。Handoff 回调按当前租户检索 RAG，售后 Agent 只能根据达到相关性
  阈值的知识片段回答；无证据或输出添加了未被知识库支持的数字、政策、库存、时效、承诺时转人工。
- `transfer_to_chitchat_agent`：纯问候、感谢、情绪、爱好和生活分享。闲聊回复不得夹带任何业务事实。
- `transfer_to_human_agent`：客户明确要求真人、需要执行退款/改订单/发货/转账等真实操作、表达无法
  理解、信息不足或存在高风险时，进入现有“待人工”流程。

三个 Handoff 都使用结构化 `reason` 和 `summary`，并过滤路由工具调用后再把对话历史交给目标
Agent。可用 `AGENT_HISTORY_LIMIT` 控制带入的历史消息数，用 `AGENT_MAX_TURNS` 限制单次编排的
模型调用次数。`LLM_PROVIDER=fake` 不支持工具调用，因此只会安全转人工；实际自动路由需使用支持
function calling 的 DashScope/Qwen 或 DeepSeek 配置。

Mem0 仍按租户、渠道和联系人范围召回，只用于关系、偏好、情绪和跟进上下文；产品价格、库存、
政策、时效等事实仍只能来自 RAG 知识库。

## 消息接待与通知

- AI 已经能够回答的消息默认安静处理，不再自动把工作台拉到前台。
- 只有售后知识库 Agent 无法可靠回答、Triage/模型异常或人工关闭 AI 托管时，才进入“待人工”。
- 同一客户连续发送多条待人工消息会合并为一个待办，桌面通知默认 30 秒内只出现一次。
- 在“状态设置”中可以分别控制：待人工通知、AI 已处理通知、待人工时是否自动打开工作台。
- 默认启用“仅接待已选择客户/会话”：先在“会话”页选中真正要接待的私聊或群聊，再勾选“接待此客户”。未勾选会话的未来消息不会进入 AI、不会写入后台会话记录，也不会弹通知；取消勾选即可停止接待。群聊还必须同时满足群触发规则。
- `auto_send=false` 时，知识库能回答的内容会标成“AI 草稿”静默留在待办中，供人工确认，不再误报成“AI 答不上”。完成客户筛选和回复验证后，再开启自动发送。

## 选择性更新知识库

- 企业微信历史自动写入知识库默认关闭，避免把私人聊天、噪声或未经确认的信息批量入库。
- 需要沉淀知识时，在“会话”页选择一个客户，点击“总结并更新知识库/话术库”，阅读确认提示后再写入。
- 只应写入已经由客服验证、可复用的业务事实和标准答复；临时承诺、个人信息和私人聊天不要入库。
- 也可以先把审核过的问答写入 `dataset/*.json`，运行
  `runtime\python\python.exe scripts\import_kb_dataset.py --dataset dataset` 做只读预检，确认后追加
  `--apply` 写入现有 `acs.db`。数据格式与命令见下方“知识库数据集格式”。

## 客户记忆

改造前的用户画像是“逐轮覆盖式摘要”：过滤短消息和寒暄后，把旧摘要、旧标签与本轮问答交给通用 LLM，解析一份新的摘要/标签 JSON，再覆盖 `CustomerProfile`，并额外维护一条 profile 兼容记忆。它没有独立原子记忆、向量召回、冲突历史或跨会话增量游标，因此更像一张不断重写的客户备注。现在改为 Mem0 先维护事实、偏好、需求和承诺等原子记忆，再由这些记忆确定性生成可读画像；画像不再是唯一事实源。

工作台的“记忆”分成四个页面：

- **记忆对话**：读取当前微信/企业微信实例中的真实好友聊天。先选择好友并审阅聊天，再点击“用 Mem0 提取长期记忆”；按钮不会发送微信消息。重复执行只处理上次游标之后的新消息。
- **长期记忆**：查看 Mem0 合并后的事实、偏好、需求、承诺和备注，以及由这些原子记忆确定性生成的用户画像。人工确认的记忆也会同步写入 Mem0，参与真实召回。
- **记忆召回**：输入一条客户当前消息，在选中好友自己的隔离作用域内查询会被自动回复使用的相关长期记忆，展示分数、来源、更新时间和分数解释。该页只验证检索结果，不调用客服模型生成回复，也不会发送微信消息；真正回复由收到客户消息后的客服主链路完成。
- **记忆治理**：集中搜索和审阅低重要度、疑似重复或旧画像兼容项；可修订、置顶、单条删除，也可勾选部分记录或全选当前列表后批量删除。删除 Mem0 记忆时会同步删除远端本地索引，避免下次同步时复活。

LoCoMo 公开数据集实验已归到“功能测试 → LoCoMo 公开集评测”，只用于验证算法和运行环境，不再当作真实客户召回效果。

记忆数据保存在本机 Mem0/Qdrant 和业务数据库中，不使用 Mem0 Cloud。Mem0 在提取和向量化时仍会按 `.env` 中的模型网关配置调用 DeepSeek、DashScope 等模型服务；不要把不允许发送给该模型服务的敏感聊天提交给记忆提取。

记忆按当前登录租户和好友作用域隔离。它只帮助保持称呼、偏好和跟进连续性；价格、库存、政策、时效等业务事实仍必须来自知识库，两者冲突时以知识库为准。

## 日常使用

- 客户给当前微信发私聊消息后，挂件会读取新消息并调用后端。
- 命中知识库时自动回复；无法回答时进入待人工。
- 聊天记录页可查看微信聊天、AI/人工/群发来源标记。
- 双击客户会话可查看客户画像。
- 在“记忆对话”中审阅并提取聊天，再到“长期记忆”查看画像、“记忆召回”验证效果、“记忆治理”修订或删除。
- 群发功能建议先只勾选一个安全测试对象。

## 知识库数据集格式

`dataset` 保存人工审阅的 JSON 知识库源数据。推荐格式：

```json
{
  "documents": [
    {
      "title": "配送规则",
      "qa": [
        {"question": "多久发货？", "answer": "付款后 48 小时内发货。"}
      ]
    },
    {
      "title": "售后政策",
      "content": "这里填写已经审核过的完整政策正文。"
    }
  ]
}
```

也支持顶层数组、单个 `{title, content}` 文档，以及 LoCoMo 风格的 `qa` 数组。只有完整的
`question` 和 `answer` 会进入知识库；对话、证据编号等评测字段不会自动成为客服事实。

先预检，不修改数据库：

```powershell
& .\runtime\python\python.exe .\scripts\import_kb_dataset.py --dataset .\dataset
```

确认标题、文档数和字符数正确后再导入：

```powershell
& .\runtime\python\python.exe .\scripts\import_kb_dataset.py --dataset .\dataset --apply
```

同一租户下标题重复的文档默认跳过。导入前应自行备份 `acs.db`，且不要把客户隐私、临时承诺
或未经核实的聊天内容写入数据集。

## Mem0 独立运行与验证

正常使用请通过 `启动AI客服.bat` 启动整套系统。仅在单独调试记忆服务时，在 PowerShell 中运行：

```powershell
Set-Location 'D:\AI-Customer-Service'  # 改成实际解压目录
& .\mem\.venv\Scripts\python.exe .\scripts\run_mem0.py
```

另开一个 PowerShell 验证服务：

```powershell
Invoke-RestMethod http://127.0.0.1:8888/health
```

LoCoMo、LongMemEval 和 BEAM 的运行器保留在 `mem\evaluation`，结果保存在 `mem\data\results`。
正式实验应先使用单 worker、小样本和 `--predict-only`，确认模型、向量维度、collection 与
Qdrant 数据目录正确后再扩大规模。

## 项目结构

- `app`：FastAPI 后端、对话编排、知识库与客户记忆服务。
- `widget`：Windows 桌面工作台与微信/企微适配。
- `agents`：项目运行所需的精简 OpenAI Agents SDK 源码。
- `mem`：本地 Mem0 SDK、companion 服务、评测运行器、独立环境与现有数据。
- `runtime`、`native`、`assets`：便携 Python、原生组件及微信/企微运行资产。
- `dataset`：经人工审阅的知识库和 LoCoMo 数据。
- `tests`：项目级回归测试；不包含两个内嵌上游仓库的开发测试套件。

## 常见问题

- 后端启动失败：查看 `logs\backend.err.log`；确认 `.env` 存在。
- 没有自动回复：确认后端窗口没有报错，`widget_config.yaml` 的 `backend_base_url` 是 `http://127.0.0.1:8000`。
- hook 读不到库：确认微信已登录；重新双击 `启动AI客服.bat` 会重新部署 hook。微信升级后也需要重新运行。
- 出现生产密钥自检失败：`APP_ENV=prod` 时不能使用默认密钥，必须填写强随机 `JWT_SECRET`、`BOOTSTRAP_TOKEN`、`FIELD_ENC_KEY`。
- 不想自动发给客户：把 `widget_config.yaml` 里的 `auto_send` 改为 `false`。

## 交付包说明

`runtime\python` 是便携 Python 运行环境，`mem\.venv` 是本地记忆服务的独立环境；两者都属于
运行依赖，不是开发缓存。项目不保留上游 SDK 文档站、示例、CI、上游测试、历史备份和缓存。
`acs.db` 是当前业务数据库；删除、替换或迁移前请先在项目目录外制作备份。
