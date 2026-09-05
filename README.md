# 🚀 Binance Square 加密热点多模型自动发帖机器人 (Pro 增强版)

> **0 服务器成本 · 0 常驻进程 · GitHub Actions 全自动定时运行 · 多 LLM 容灾故障转移 · 币安广场 OpenAPI 自动发布**

本项目专为加密货币创作者（Binance Square Creator）打造，定时抓取顶级加密快讯，支持通过 **OpenRouter、B.ai、xkiro、aihubmix、inferera、TokenRouter、DeepSeek、硅基流动** 等多模型池进行智能提炼、行情点评与代币标签提取（触发 Write to Earn），并自动发布至币安广场。

---

## 🌟 核心优势与特色

- 🆓 **0 服务器成本**：基于 GitHub Actions 定时触发（默认每 30 分钟），无任何服务器或云函数费用。
- 🎯 **AI 自动扫描与理解币安官方活动**：
  - 定期自动抓取币安官方最新竞赛、合约上线、新币与理财活动公告。
  - 由 AI 深度理解当期重点扶持代币（如 `$BNB`、`$SOL`、竞赛币）与官方流量标签（`#Write2Earn`、`#BinanceSquare` 等），并将快讯与当期活动有机结合，最大化瓜分创作者奖励！
- 📊 **实时盘面与宏观情绪注入 (Live Market Context)**：
  - 自动注入全网恐慌与贪婪指数（Fear & Greed Index）。
  - 币安官方 `symbols=[...]` 批量行情接口单次拉取全部标的 24H 价格、涨跌幅（失败自动降级逐币查询）。
- 🔥 **重磅热点价值打分算法 (Impact Scorer)**：
  - 引入市场冲击力关键词加权算法（ETF、SEC、降息、上线、Launchpool、爆仓、突破等），ASCII 关键词严格整词匹配（杜绝 ai/ton/sui 误判 inflated 打分），中文词保持子串兼容。
  - **新鲜度加权**：3 小时内突发 +10、12 小时内 +6、24 小时内 +3，真正的突发热点永远排在最前。
  - **源健康自检**：任一 RSS 源故障自动记录；全源（9 个）同时故障时立即报警并以非零状态退出（Actions 面板直接标红），绝不用”今天没新闻”的假平静掩盖基建故障。
  - 自动叠加”当期币安官方活动重点代币”加分权重，发帖与官方流量池深度对齐。
- ⏰ **热点时效与跨源近似去重 (Freshness & Near-Dup Guard)**：
  - 自动解析 RSS 发布时间，超过 `MAX_NEWS_AGE_HOURS`（默认 48 小时）的旧闻直接丢弃。
  - 标题级 Jaccard 相似度去重：同一事件被多家媒体报道时只发一次，彻底杜绝跨源刷屏重复。
  - **跨语言事件指纹**：中英文媒体同时报道同一事件时，标题词集毫无交集导致 Jaccard 失效；此时用"金额/百分比量级指纹 + 大写币种交集"做旁路判定，照样捕获跨语言重复。
- 🛑 **发布链路断路器**：币安发帖接口连续 3 次失败（Key 失效/风控/接口故障）立即终止本轮运行并推送报警，避免生成的新闻不断浪费 LLM 额度。
- ⚡ **行情与情绪 TTL 缓存**：同一轮内多条新闻同涉 $BTC 时，行情与恐慌指数 90 秒内只请求一次，减少 HTTP 压力。
- ✅ **币安交易标的防幻觉校验 (Symbol Validator)**：
  - 自动比对币安真实交易对列表，确保提取的 `$TOKEN` 100% 触发 Write to Earn 交易组件与返佣。
  - **歧义代码守护**：`NEAR`/`LINK`/`MASK` 等与英文单词撞名的代币，仅当原文全大写或带 `$` 前缀时采信。
  - **校验结果反哺 Prompt**：从新闻中提取的真实交易标的会注入 LLM 请求，模型只围绕真实存在的币写作。
  - 未识别到任何真实标的的新闻直接跳过，拒绝向无关内容强挂 `$BTC`。
  - 发布前动态剥壳非真实标的（如 `$FAKECOIN`），纯数字金额（如 `$120000`）不受影响。
  - **交易挂件保底**：正文若漏写 `$TOKEN`，自动在标签区前插入识别到的首个真实标的，确保返佣组件 100% 渲染。
- 🔄 **多模型池与自动故障转移 (Auto-Failover + Circuit Breaker)**：
  - 针对免费模型平台（如 OpenRouter、B.ai 等）常见的并发限制、429 Rate Limit、偶发超时等问题，内置**智能容灾切换机制**。
  - **本次运行内健康度自适应调度**：连续失败次数越多的提供商自动沉底，避免每条新闻都先撞一次死节点。
  - **跨运行熔断器**：提供商失败进入指数退避冷却（10min → 20min → …→ 封顶 4h），冷却期自动跳过；成功一次立即解除，防止长期失效的平台每天白白浪费几十次超时重试。
  - 模型池连续 3 次全量失败自动触发**熔断**并推送报警，避免空跑浪费 GitHub Actions 时长。
- 📡 **RSS 源健康画像与自动停放**：连续 3 次拉取失败的数据源自动停放 6 小时（不占用本次运行配额），恢复成功立即解除；停放期不影响其他源的抓取速度。
  - **隐性故障识别**：源返回 HTTP 200 但内容不是有效 RSS（被风控/错误页）一样按故障计分，不会被"假成功"掩盖。
- 🌊 **状态跨运行不丢失**：所有运行时状态（断路器/停放/报警节流/兜底图缓存）都用 `_` 前缀键存进 `campaign_intel.json`，git 同步做**深度并集合并**，多并发运行绝不互丢状态。
- 📞 **通知渠道加固**：Bark 的 URL 路径完全编码（中文/特殊字符不再断链）、Telegram 改纯文本发送（Markdown 特殊字符不再 400）、超长消息自动截断附省略标记。
- 🛡️ **AI 输出质量门与注入防护 (Quality Gate & Anti-Injection)**：
  - 生成内容必须满足中文占比与长度硬门槛，跑偏/过短/英文输出自动判定失败并切换下一模型，绝不带病发布。
  - 自动识别并截断 RSS 摘要中夹带的提示词注入指令（"ignore previous instructions" / "无视之前的规则" 等），防止机器人被劫持发言。
- 💰 **情报资产保护**：币安活动情报仅在 AI 真实分析成功时落盘；分析失败自动沿用上一份历史情报，杜绝低质兜底数据覆写优质资产。
- 🖼️ **发布链路强化**：币安发帖接口遭遇 429/502 等暂态错误自动延迟重试；配图全格式（含 LA/I;16）统一转码标准 JPEG。
- 🧠 **币安广场 Write to Earn 深度适配**：
  - 自动提炼核心事实（80~150 字），言简意赅。
  - 自动输出 1 句精辟行情与趋势点评。
  - 严格且精准提取 1~2 个大写 `$TOKEN`（如 `$BTC`、`$SOL`），触发币安交易组件与返佣。
  - 标明标的合约类型（现货/USDT永续）与链上合约(CA)，结尾附带互动话题问答。
  - 内置敏感词与合规风控，过滤”带单/稳赚”等违禁词。
- 🛡️ **严格防重复**：本地 `sent_cache.json` 结合 SHA256 哈希 ID 去重；回写采用**快照→并集合并→重试推送**流水线，任何一方的已发记录都不丢失，彻底杜绝 Git 冲突与重复发帖。
- 🚦 **24h 发帖配额保护**：默认 12 篇/24 小时滚动上限，达到配额自动静默退出，高频定时也不会因刷屏被币安风控降权。
- 🕘 **活跃时段窗口（可选，支持跨夜）**：通过 `ACTIVE_HOURS_BEIJING=8-23` 或 `22-7`（跨夜）限制仅在北京时间特定时段发帖，避开低流量时段，保持账号互动权重。
- 🖼️ **兜底图当日复用**：恐慌贪婪指数仪表盘配图一天内只上传一次币安 CDN，跨次运行直接复用链接，节省配额并避免重复转码延迟。
- 🔗 **报警一键直达日志**：所有推送通知自动附带本次 GitHub Actions 运行日志链接，出问题时点开即能看到完整日志。
- 📢 **可选消息通知**：支持绑定 Telegram Bot 或 Webhook（钉钉/飞书/企微/Discord）实时推送发帖结果。
  - **报警 12h 节流**：同一标题的错误报警 12 小时内只推送一次，状态随 Git 同步持久化，LLM 池长期失效也不再被消息轰炸。
  - **错误精细化诊断**：发布失败自动翻译成可操作的排障指引（401/403 → 请重新生成 Key、20002/20022 → 内容被风控拦截、220094 → Hashtag 超限），无需翻日志。
  - 未配置任何渠道时通知链路完全静默短路，不写任何状态文件。
- 🪙 **单代币 24h 限流**：同一代币（如 $BTC）24 小时内默认最多发 3 篇，避免全账号时间线被单一币种占据导致粉丝疲劳与算法降权。
- 📌 **依赖版本锁定**：`requirements.txt` 全部声明主版本上限，任何上游 breaking release 都无法自动溜进 CI。
- 🧪 **DRY_RUN 零副作用**：试运行模式下既不真实发帖、也**不写入去重缓存**，可放心反复调试。
- 📋 **Actions 运行报告**：每次运行在 GitHub Actions Summary 页自动生成 Markdown 报告（吞吐漏斗、发布明细、命中模型与配图状态），无需翻日志。

---

## 🧩 常用支持模型接口清单 (扫描适配)

系统原生兼容以下常用平台的 OpenAI 格式接口及免费模型（可同时配置多个，自动轮询容灾）：

| 提供商名称 | Base URL | 常用/推荐免费模型 | 专用 Secret 变量名 |
| :--- | :--- | :--- | :--- |
| **OpenRouter** | `https://openrouter.ai/api/v1` | `minimax/minimax-m3:free`<br>`qwen/qwen-2.5-72b-instruct:free`<br>`deepseek/deepseek-r1:free`<br>`google/gemini-2.0-flash-exp:free` | `OPENROUTER_API_KEY` |
| **B.ai** | `https://api.b.ai/v1` | `deepseek-v4-flash`<br>`glm-5.3-flash`<br>`qwen3.8-flash` | `BAI_API_KEY` |
| **xkiro** | `https://api.xkiro.com/v1` | `qwen/qwen3.8-max:free`<br>`minimax/minimax-m3:free` | `XKIRO_API_KEY` |
| **aihubmix** | `https://aihubmix.com/v1` | `coding-glm-5.3-flash-free`<br>`gemini-3.7-flash-free`<br>`minimax-m3-free` | `AIHUBMIX_API_KEY` |
| **inferera** | `https://api.inferera.com/v1` | `coding-kimi-k3-free`<br>`gemini-3.7-flash-free`<br>`minimax-m3-free` | `INFERERA_API_KEY` |
| **TokenRouter** | `https://api.tokenrouter.com/v1` | `qwen/qwen3.8-max-free`<br>`z-ai/glm-5.3-free` | `TOKENROUTER_API_KEY` |
| **DeepSeek 官方** | `https://api.deepseek.com` | `deepseek-chat` | `LLM_API_KEY` |
| **SiliconFlow (硅基流动)** | `https://api.siliconflow.cn/v1` | `deepseek-ai/DeepSeek-V3`<br>`Qwen/Qwen2.5-7B-Instruct` | `SILICONFLOW_API_KEY` |
| **🏠 Reasonix 本地网关** ⭐ 本地首选 | `http://localhost:20140/v1` | `auto/best-fast`（自动路由） | **无需 Key**（本地自动发现） |

### 🏠 Reasonix 本地免费模型网关（本地开发首选）

如果你本机跑着 [Reasonix Gateway](https://github.com/your-reasonix)（聚合 OmniRoute/g4f/OpenCode/OVH/OpenRouter 等 90+ 免费上游的本地统一网关），**什么都不用配置**：发帖机器人在本地启动时会自动探测 `http://localhost:20140/v1`，在线则置顶为首选，离线的 CI 环境自动跳过零干扰：

- **零成本模型池**：网关自动聚合几十路免费上游并做内部容错，本地开发/调试/DRY_RUN 不再消耗任何 API Key 额度
- **探测开销极低**：约 2 秒一次 ping，失败静默跳过；`GITHUB_ACTIONS=true` 时自动跳过
- **代理安全**：本地通道强制 `trust_env=False`，规避 Windows TUN/Clash 拦截 localhost 的老坑
- **推理预算**：网关被识别为推理型模型通道，自动扩容 `max_tokens=1500`（其他渠道仍按 600 节省成本）
- **手动关闭**：环境变量 `REASONIX_GW_OFF=1` 可强制禁用；`REASONIX_GW_URL` 可换自定义地址

---

## 🚀 GitHub Actions 快速部署

### 第一步：设置仓库 Actions 权限（必须）
1. 进入 GitHub 仓库 **Settings** -> **Actions** -> **General**。
2. 滚动到底部 **Workflow permissions**，选择 **Read and write permissions**，并点击 **Save**。

### 第二步：配置 GitHub Secrets（密钥）
进入仓库 **Settings** -> **Secrets and variables** -> **Actions** -> 点击 **New repository secret**：

#### 必填项：
- `SQUARE_API_KEY`: 币安创作者中心生成的 Square OpenAPI Key（[获取地址](https://www.binance.com/zh-CN/square) -> API 管理）。

#### 模型密钥（任选其一或配置多个实现自动容灾）：

**方式 A：简单配置（单 Key 或常用预置 Key）**
- `OPENROUTER_API_KEY`: 你的 OpenRouter Key（自动使用内置免费模型池）
- `BAI_API_KEY`: 你的 B.ai API Key
- `LLM_API_KEY`: 你的 DeepSeek / 其他 OpenAI 兼容 API Key
- `LLM_BASE_URL` *(可选)*: 自定义接口地址（默认 `https://api.deepseek.com`）
- `LLM_MODEL` *(可选)*: 自定义模型名称（默认 `deepseek-chat`）

**方式 B：高级多模型容灾池配置（`LLM_PROVIDERS_CONFIG`）**
如果你有多个 Key 想按顺序故障转移，直接添加一个 Secret 变量 `LLM_PROVIDERS_CONFIG`，内容为 JSON 数组：
```json
[
  {
    "name": "B.ai-DeepSeek",
    "base_url": "https://api.b.ai/v1",
    "api_key": "sk-你的BaiKey",
    "model": "deepseek-v4-flash"
  },
  {
    "name": "OpenRouter-Free",
    "base_url": "https://openrouter.ai/api/v1",
    "api_key": "sk-or-v1-你的OpenRouterKey",
    "model": "minimax/minimax-m3:free"
  },
  {
    "name": "xkiro-Free",
    "base_url": "https://api.xkiro.com/v1",
    "api_key": "sk-你的xkiroKey",
    "model": "qwen/qwen3.8-max:free"
  }
]
```

---

## 💻 本地测试与运行

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 设置测试环境变量并运行 (以 PowerShell 为例)
$env:SQUARE_API_KEY="your_square_key"
$env:OPENROUTER_API_KEY="sk-or-v1-xxxx"
$env:DRY_RUN="true" # 开启模拟模式，不实际发布

python main.py
```

---

## ⚙️ 定制与优化建议

- **调整执行频率**：修改 `.github/workflows/auto_post.yml` 中的 `cron: '*/20 * * * *'`（默认每 20 分钟）。
- **调整单次发帖上限**：可在 Actions 手动触发时指定 `max_posts`（建议保持 1 篇，避免瞬间刷屏）。
- **运维调优参数**：在仓库 **Settings → Secrets and variables → Actions → Variables** 中新增以下变量即可生效（全部无需改代码）：

  | 变量名 | 默认值 | 说明 |
  | :--- | :--- | :--- |
  | `MAX_NEWS_AGE_HOURS` | `48` | 新闻最大时效（小时），超过视为旧闻直接丢弃 |
  | `DUP_SIMILARITY_THRESHOLD` | `0.65` | 跨源近似标题去重阈值（0~1，越小越严格） |
  | `MIN_IMPACT_SCORE` | `0` | 最低热度分门槛（0 表示不过滤，建议 10~15 只发大新闻） |
  | `MAX_DAILY_POSTS` | `12` | 24 小时发帖配额上限，防刷屏保账号权重（0 表示不限） |
  | `ACTIVE_HOURS_BEIJING` | 空 | 北京时间活跃窗口，支持跨夜，例 `8-23` 或 `22-7`（空 = 全天） |
  | `TOKEN_DAILY_LIMIT` | `3` | 同一代币 24h 内最多发帖篇数（0 = 不限） |
- **CI 回归防线**：`tests/test_core.py` 内置 70 个离线回归测试（含断路器/源停放/报错分类/通知编码/跨语言去重/行情缓存），`.github/workflows/ci.yml` 在每次 push/PR 时自动编译并跑测试，防止守护逻辑被后续改动悄悄破坏。

- **DRY_RUN 语义**：手动触发选择 `dry_run=true` 时，完整跑通抓取/打分/AI/配图流水线，但不真实发帖也**不写入去重缓存**，适合验收。
