# 🚀 Binance Square 加密热点多模型自动发帖机器人 (Pro 增强版)

> **0 服务器成本 · 0 常驻进程 · GitHub Actions 全自动定时运行 · 多 LLM 容灾故障转移 · 币安广场 OpenAPI 自动发布**

本项目专为加密货币创作者（Binance Square Creator）打造，定时抓取顶级加密快讯，支持通过 **OpenRouter、B.ai、xkiro、aihubmix、inferera、TokenRouter、DeepSeek、硅基流动** 等多模型池进行智能提炼、行情点评与代币标签提取（触发 Write to Earn），并自动发布至币安广场。

---

## 🌟 核心优势与特色

- 🆓 **0 服务器成本**：基于 GitHub Actions 定时触发（默认每 20 分钟），无任何服务器或云函数费用。
- 🎯 **AI 自动扫描与理解币安官方活动**：
  - 定期自动抓取币安官方最新竞赛、合约上线、新币与理财活动公告。
  - 由 AI 深度理解当期重点扶持代币（如 `$BNB`、`$SOL`、竞赛币）与官方流量标签（`#Write2Earn`、`#BinanceSquare` 等），并将快讯与当期活动有机结合，最大化瓜分创作者奖励！
- 📊 **实时盘面与宏观情绪注入 (Live Market Context)**：
  - 自动注入全网恐慌与贪婪指数（Fear & Greed Index）。
  - 币安官方 `symbols=[...]` 批量行情接口单次拉取全部标的 24H 价格、涨跌幅（失败自动降级逐币查询）。
- 🔥 **重磅热点价值打分算法 (Impact Scorer)**：
  - 引入市场冲击力关键词加权算法（ETF、SEC、降息、上线、Launchpool、爆仓、突破等），ASCII 关键词严格整词匹配（杜绝 ai/ton/sui 误判 inflated 打分），中文词保持子串兼容。
  - **突发热点顶格追**：`breaking`/`just in`/`急报`/`突发` 等突发标题词享有最高档权重（+14/+10），配合新鲜度加权确保最新热点永远最先处理。
  - **新鲜度加权**：3 小时内突发 +10、12 小时内 +6、24 小时内 +3，真正的突发热点永远排在最前。
  - **源健康自检**：任一 RSS 源故障自动记录；全源（9 个）同时故障时立即报警并以非零状态退出（Actions 面板直接标红），绝不用”今天没新闻”的假平静掩盖基建故障。
  - 自动叠加"当期币安官方活动重点代币"加分权重，发帖与官方流量池深度对齐；该加权**只影响排序、不影响准入**，低质源蹭活动币也无法越过 `MIN_IMPACT_SCORE` 门槛。
- ⏰ **热点时效与跨源近似去重 (Freshness & Near-Dup Guard)**：
  - 自动解析 RSS 发布时间，超过 `MAX_NEWS_AGE_HOURS`（默认 48 小时）的旧闻直接丢弃。
  - 标题级 Jaccard 相似度去重：同一事件被多家媒体报道时只发一次，彻底杜绝跨源刷屏重复。
  - **跨语言事件指纹**：中英文媒体同时报道同一事件时，标题词集毫无交集导致 Jaccard 失效；此时用"金额/百分比量级指纹 + 大写币种交集"做旁路判定，照样捕获跨语言重复。
  - **指纹强度门槛**：仅共享一个百分比（两条都写「涨 5%」）不再判定为同一事件——百分比是弱信号；必须"共享一个真实金额量级"或"同时命中两个不同百分比"才成立，杜绝高分好稿被静默误杀。
  - **先排序后去重**：同一事件的多篇报道保留热度分最高、要素最全的那篇，而非抓取线程跑得最快的那篇；排序带确定性 tiebreak，同一批输入两次运行结果完全一致。
- 🛑 **发布链路断路器**：币安发帖接口连续 3 次失败（Key 失效/风控/接口故障）立即终止本轮运行并推送报警，避免生成的新闻不断浪费 LLM 额度。
- ⚡ **行情与情绪 TTL 缓存**：同一轮内多条新闻同涉 $BTC 时，行情与恐慌指数 90 秒内只请求一次，减少 HTTP 压力。
- ✅ **币安交易标的防幻觉校验 (Symbol Validator)**：
  - 自动比对币安真实交易对列表，确保提取的 `$TOKEN` 100% 触发 Write to Earn 交易组件与返佣。
  - **歧义代码守护**：`NEAR`/`LINK`/`MASK` 等与英文单词撞名的代币，仅当原文全大写或带 `$` 前缀时采信。
  - **校验结果反哺 Prompt**：从新闻中提取的真实交易标的会注入 LLM 请求，模型只围绕真实存在的币写作。
  - 未识别到任何真实标的的新闻直接跳过，拒绝向无关内容强挂 `$BTC`。（历史遗留的静默 `$BTC` 兜底已移除；如需向后兼容的强制兜底，可设 `BINANCE_FORCE_BTC_FALLBACK=1`，但会重新引入无关曝光，默认关闭。）
  - 发布前动态剥壳非真实标的（如 `$FAKECOIN`），纯数字金额（如 `$120000`）不受影响。
  - **交易挂件保底**：正文若漏写 `$TOKEN`，自动在标签区前插入识别到的首个真实标的，确保返佣组件 100% 渲染。
- 🔄 **多模型池与自动故障转移 (Auto-Failover + Circuit Breaker)**：
  - 针对免费模型平台（如 OpenRouter、B.ai 等）常见的并发限制、429 Rate Limit、偶发超时等问题，内置**智能容灾切换机制**。
  - **本次运行内健康度自适应调度**：连续失败次数越多的提供商自动沉底，避免每条新闻都先撞一次死节点。
  - **成本/延迟感知二次排序**：同健康档内的提供商，按历史平均延迟 + token 成本代理升序优先（承接 metrics.jsonl 遥测）；无遥测时自动回退原配置顺序，零副作用。
  - **跨运行熔断器**：提供商失败进入指数退避冷却（10min → 20min → …→ 封顶 4h），冷却期自动跳过；成功一次立即解除，防止长期失效的平台每天白白浪费几十次超时重试。
  - 模型池连续 3 次全量失败自动触发**熔断**并推送报警，避免空跑浪费 GitHub Actions 时长。
- 📡 **RSS 源健康画像与自动停放**：连续 3 次拉取失败的数据源自动停放 6 小时（不占用本次运行配额），恢复成功立即解除；停放期不影响其他源的抓取速度。
  - **隐性故障识别**：源返回 HTTP 200 但内容不是有效 RSS（被风控/错误页）一样按故障计分，不会被"假成功"掩盖。
- 🌊 **状态跨运行不丢失**：所有运行时状态（断路器/停放/报警节流/兜底图缓存）都用 `_` 前缀键存进 `campaign_intel.json`，git 同步做**深度并集合并**，多并发运行绝不互丢状态。
- 📞 **通知渠道加固**：Bark 的 URL 路径完全编码（中文/特殊字符不再断链）、Telegram 改纯文本发送（Markdown 特殊字符不再 400）、超长消息自动截断附省略标记。
- 🛡️ **AI 输出质量门与注入防护 (Quality Gate & Anti-Injection)**：
  - 生成内容必须满足中文占比与长度硬门槛，跑偏/过短/英文输出自动判定失败并切换下一模型，绝不带病发布。
  - **数字幻觉软校验**：精确小数百分比（`12.53%`）、大额精确金额（`$120,000` / `2.4 亿`）必须能在源文里找到落点；K/M/B 单位缩写会智能还原（`$2.4B` 与 `24 亿美元` 等价），交易员人设的粗略整数（"止损 10%"、"5% 仓位"）不拦截。
  - 自动识别并截断 RSS 摘要中夹带的提示词注入指令（"ignore previous instructions" / "无视之前的规则" 等），防止机器人被劫持发言。
- 💰 **情报资产保护**：币安活动情报仅在 AI 真实分析成功时落盘；分析失败自动沿用上一份历史情报，杜绝低质兜底数据覆写优质资产。
- 🎭 **写派人设轮换 (ShuffleBag)**：三种写派——毒舌老韭菜 / 数据拆解派 / 吃瓜叙事党——每帖随机注入 system prompt，让时间线的"人味"不重样。洗牌袋算法保证任意连续 3 帖恰好三家各一（纯随机在小窗口会扎堆，生产实测踩过），结尾互动套路同样洗牌轮换。
- 🖼️ **动态情绪卡配图**：新闻无原图时不再用单一静态图，而是 PIL 现场渲染 1200×675 市场情绪卡——三种布局随机（分栏/横幅/极简）、底色随真实恐慌贪婪指数变色（贪婪=红/恐惧=绿/中性=蓝）、卡片印真实代币符号与行情，同日缓存复用不重传。
- 🖼️ **发布链路强化**：币安发帖接口遭遇 429/502 等暂态错误自动延迟重试；配图全格式（含 LA/I;16）统一转码标准 JPEG。
- 🧠 **币安广场 Write to Earn 深度适配**：
  - 自动提炼核心事实（160~240 字），言简意赅。
  - 自动输出 1 句精辟行情与趋势点评。
  - 严格且精准提取 1~2 个大写 `$TOKEN`（如 `$BTC`、`$SOL`），触发币安交易组件与返佣。
  - 标明标的合约类型（现货/USDT永续）与链上合约(CA)，结尾附带互动话题问答。
  - 内置敏感词与合规风控，过滤”带单/稳赚”等违禁词。
- 🛡️ **严格防重复**：本地 `sent_cache.json` 结合 SHA256 哈希 ID 去重；回写采用**快照→并集合并→重试推送**流水线，任何一方的已发记录都不丢失，彻底杜绝 Git 冲突与重复发帖。
  - **落盘失败即止损**：已发布但 `sent_cache.json` 写入失败（自动重试一次仍失败）时立即停止本轮后续发帖并推送报警——继续发只会制造更多无法登记的重复帖。
  - **幂等跳过 ≠ 失败**：草稿已存在 / Telegram 已镜像过属于"已投递"，不再计入失败，副平台-only 模式不会因此误报通道熔断。
- 🚦 **24h 发帖配额保护**：默认 12 篇/24 小时滚动上限，达到配额自动静默退出，高频定时也不会因刷屏被币安风控降权。
- 🕘 **活跃时段窗口（可选，支持跨夜）**：通过 `ACTIVE_HOURS_BEIJING=8-23` 或 `22-7`（跨夜）限制仅在北京时间特定时段发帖，避开低流量时段，保持账号互动权重。
- 🖼️ **兜底图当日复用**：恐慌贪婪指数仪表盘配图一天内只上传一次币安 CDN，跨次运行直接复用链接，节省配额并避免重复转码延迟。
- 🔗 **报警一键直达日志**：所有推送通知自动附带本次 GitHub Actions 运行日志链接，出问题时点开即能看到完整日志。
- 📢 **可选消息通知**：支持绑定 Telegram Bot 或 Webhook（钉钉/飞书/企微/Discord）实时推送发帖结果。
- 🌍 **多平台分发**：`PUBLISH_PLATFORMS` 变量组合启用（默认 `binance`）：
  - `binance` — 币安广场官方 OpenAPI 全自动发帖
  - `telegram` — **Telegram 频道镜像**（全自动）：Bot 拉进频道做管理员，设 `TELEGRAM_MIRROR_CHANNEL_ID`（如 `@mychannel`）即可；带图走 sendPhoto，图拉取失败自动降级纯文本。加密社区原生分发渠道，引流利器
  - `okx_draft` — **OKX 广场草稿直出**：OKX 官方暂无发帖 API（V5 仅交易/行情），逆向 cookie 属违反 ToS 有封号风险故不做。折中方案：每篇 AI 生成内容自动落一份"即贴即用"草稿（正文+配图直链+发布清单）到 `drafts/` 目录随 Git 同步并推送提醒，手机打开复制粘贴到 OKX App 广场约 10 秒，可配合 [OKX 星球创作者激励](https://www.okx.com/zh-hans/campaigns/orbit-creator-monetization)（发文赚 USDT）。保留最近 30 份自动清理
  - 仅副平台模式（如只开 `okx_draft` 或 `telegram`）**无需币安 Key**，任一平台完成投递即入缓存；所有平台实现统一 `BasePublisher` 接口，新平台接入只需一个类
  - **报警 12h 节流**：同一标题的错误报警 12 小时内只推送一次，状态随 Git 同步持久化，LLM 池长期失效也不再被消息轰炸；节流在**确认至少一个渠道投递成功后**才登记，全渠道发送失败不会白白吃掉这条报警的额度。
  - **错误精细化诊断**：发布失败自动翻译成可操作的排障指引（401/403 → 请重新生成 Key、20002/20022 → 内容被风控拦截、220094 → Hashtag 超限），无需翻日志。
  - 未配置任何渠道时通知链路完全静默短路，不写任何状态文件。
- 🪙 **单代币 24h 限流**：同一代币（如 $BTC）24 小时内默认最多发 3 篇，避免全账号时间线被单一币种占据导致粉丝疲劳与算法降权；**任一**命中代币触顶即跳过（此前要求全部代币都触顶，导致限流形同虚设）。
- 📌 **依赖版本锁定**：`requirements.txt` 全部声明主版本上限，任何上游 breaking release 都无法自动溜进 CI。
- 🧪 **DRY_RUN 零副作用**：试运行模式下既不真实发帖、也**不写入去重缓存**，可放心反复调试。
- 📋 **Actions 运行报告**：每次运行在 GitHub Actions Summary 页自动生成 Markdown 报告（吞吐漏斗、**每源产出 TOP 排行**、发布明细、命中模型与配图状态、**分阶段耗时画像**），无需翻日志。
- 📉 **失败路径同样留痕**：`metrics.jsonl` 不只记成功——`llm_failed` / `publish_failed` / `*_cache_failed` 全部落盘，可以回答"高热新闻是否被模型池或质量门系统性饿死"，而非只看活下来的稿子。
- 🏥 **一键健康自检**：`python main.py --healthcheck` 探测 LLM 链（含断路状态）、Reasonix 网关、RSS 源健康（含停放中/曾故障明细）、币安现货接口、情绪指数、运行策略，退出码即诊断结论。

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
- **首选+备份自动降级链**：探测到网关后同时注册 3 个模型通道（首选 `auto/best-fast` + 网关实际目录中存在的 2 个备份），首选挂掉自动降级到同网关内其他模型，避免整条链退到外部收费路径
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

# 2. 全链路健康自检（强烈推荐每次部署/改配置后先跑一下）
python main.py --healthcheck
#   → 一键检查 SQUARE_API_KEY、LLM 链路、Reasonix 网关、RSS 源、币安接口、通知渠道
#   → 退出码 0 = 可放心运行，1 = 有硬性故障需先修

# 3. DRY_RUN 完整演练（不发帖不写缓存）
$env:SQUARE_API_KEY="your_square_key"
$env:OPENROUTER_API_KEY="sk-or-v1-xxxx"  # 或者不配，自动用本地 Reasonix 网关
$env:DRY_RUN="true"
python main.py

# 4. 正式运行
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
  | `ARTICLE_PER_DAY` | `1` | 每日深度长文开关：当天首个高热帖升级为长文（contentType=2，TITLE+500~800 字正文），打专业垂直度与长尾流量 |
  | `ARTICLE_MIN_IMPACT` | `20` | 长文选稿门槛：榜首热度分低于此值则当天不发长文（全发短讯） |
- **CI 回归防线**：`tests/test_core.py` 内置百余个离线回归测试（含断路器/源停放/报错分类/通知编码/跨语言去重/行情缓存/同步契约），`.github/workflows/ci.yml` 在每次 push/PR 时自动编译、校验 workflow 语法并跑测试，防止守护逻辑被后续改动悄悄破坏。
  - **workflow 内嵌脚本校验的环境降级**：`scripts/validate_workflows.py` 只在确认本机 bash **真能执行** `bash -n -c true` 时才校验内嵌 shell；PATH 上只有 WSL 启动器、或 bash 被安全策略拒绝时，一律跳过并说明原因，**绝不把环境故障伪装成 workflow 语法错误**。需强制指定时用环境变量 `BASH_PATH=/path/to/bash`。

- **DRY_RUN 语义**：手动触发选择 `dry_run=true` 时，完整跑通抓取/打分/AI/配图流水线，但不真实发帖也**不写入去重缓存**，适合验收。
