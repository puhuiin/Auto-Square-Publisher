# 🚀 Binance Square 加密热点多模型自动发帖机器人 (Pro 增强版)

> **0 服务器成本 · 0 常驻进程 · GitHub Actions 全自动定时运行 · 多 LLM 容灾故障转移 · 币安广场 OpenAPI 自动发布**

本项目专为加密货币创作者（Binance Square Creator）打造，定时抓取顶级加密快讯，支持通过 **OpenRouter、B.ai、智谱 Z.ai、xkiro、aihubmix、inferera、TokenRouter、SiliconFlow、bluesminds** 等多模型池进行智能提炼、行情点评与代币标签提取（触发 Write to Earn），并自动发布至币安广场。

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
  - **失败也有负缓存**：K 线拉取失败按 120s 负缓存，避免一次故障期内每帖每标的都重打（每标的 2 主机 × 2 次重试 × 5s）。
  - **缺失数据不伪装成合理数字**：拿不到 `lastPrice` 时该标的**整条行情行丢弃**，不再渲染 `$0.000000 (24H: +0.00%)` 并当作"实时盘面"喂进 prompt；批量接口只回一部分时会明确记录缺失标的（区分"没有"与"拿不到"）。
  - **标的表降级留痕**：`exchangeInfo` 拉取失败时降级到内置兜底池（47 币）——该轮新币新闻会被记为 `no_token` 跳过，所以降级原因会写进遥测与报表（`symbols_degraded`），避免被归因成"这些新闻没有标的"。降级时不把半成品集合留在缓存里。
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
    - **显式优先级不被成本分撤销**：本地免费网关靠 `priority` 保住"置顶"语义。此前靠 `insert(0)` 置顶，但网关通道在仓库里的遥测为空、成本分恒为 +∞，只要任一付费通道有历史就会被排到后面——"本地开发零成本"的设计意图被静默撤销。健康度仍优先于优先级（失败过的通道照样让位）。
    - **聚合路由别名识别**：`auto/best-fast`、`omni/auto/best-free`、`omni/auto/coding:free` 这类路由别名此前只认 `/free` 后缀，一次 404 就吃 24h permanent 冷却；现在按路径段识别 `auto`/`router`。具体模型（含 `:free` 限定与 `-free` 后缀）仍走永久快道，避免下架模型每 10 分钟重试空烧。
  - **跨运行熔断器**：提供商失败进入指数退避冷却（10min → 20min → …→ 封顶 4h），冷却期自动跳过；成功一次立即解除，防止长期失效的平台每天白白浪费几十次超时重试。
    - **冷却只延长不缩短**：30s 的 429 限流冷却不会覆盖服务端要求的 4h Retry-After，更不会覆盖模型下架的 24h 长冷却。
    - **状态自愈**：断路器状态里的畸形/脏条目（手改、旧版本遗留）会被修成一个**有界**冷却并写回——既不再 fail-open 让已下架的通道每轮白撞，也不会变成永久死锁。
    - **文风差 ≠ 通道坏**：质量门拒稿单独计数，只影响运行内让位，不推高跨运行熔断（此前两次"文风不合格"加一次空回就会把一个健康通道冷却 4 小时）。
    - **同名不串线**：`LLM_PROVIDERS_CONFIG` 的重名/缺名配置会自动去重，客户端缓存按"名字+端点+Key 指纹"隔离，两条不同端点的通道不会共用同一个客户端。
  - 模型池连续 3 次全量失败自动触发**熔断**并推送报警，避免空跑浪费 GitHub Actions 时长。
- 📡 **RSS 源健康画像与自动停放**：连续 3 次拉取失败的数据源自动停放 6 小时（不占用本次运行配额），恢复成功立即解除；停放期不影响其他源的抓取速度。
  - **读写不放大**：源健康状态每轮只读一次快照（此前逐源各读一次整文件）；成功源在无记录时**不写盘**（此前每轮固定 9 次"读+整文件写"，全是空操作）。实测每轮 18 读 + 9 写 → 10 读 + 0 写。
  - **隐性故障识别**：源返回 HTTP 200 但内容不是有效 RSS（被风控/错误页）一样按故障计分，不会被"假成功"掩盖。
- 🌊 **状态跨运行不丢失**：所有运行时状态（断路器/停放/报警节流/兜底图缓存）都用 `_` 前缀键存进 `campaign_intel.json`，git 同步做**深度并集合并**，多并发运行绝不互丢状态。
  - **遥测文件有硬上限**：`metrics.jsonl` 每轮收尾轮转裁剪，且**合并侧也按同一上限收敛**——纯并集合并会把刚裁掉的历史行从远端"复活"，让轮转形同虚设；现在上限由合并侧兜底保证，不依赖某一侧是否跑过轮转。
  - **跨帖记忆口径一致**：情绪锚点计数与禁令滞回共用同一个"最近 N 篇带正文快照的回执"窗口，禁令状态可复现（此前两者样本集不同，同一份遥测重算会得出不同结论）。
  - **同 id 取较新记录**：`sent_cache` 合并时同一 id 取 `sent_at` 较新者（旧实现"本地快照必胜"，会让配额/单币限流的 24h 窗口回退）。
  - **情报刷新走持锁读-改-写**：刷新落盘不再拿函数入口的旧快照回灌整文件，刷新期间其它状态写入不会被旧值覆盖。
  - **推送失败可恢复**：状态回写连续失败时，把快照整段转储进运行日志并明确报错"下一轮会重复发布"，人工可从日志恢复（不引入额外 action 的前提下唯一能把"静默丢失"变成"可恢复"的手段）。
- 📞 **通知渠道加固**：Bark 的 URL 路径完全编码（中文/特殊字符不再断链）、Telegram 改纯文本发送（Markdown 特殊字符不再 400）、超长消息自动截断附省略标记。
- 🛡️ **AI 输出质量门与注入防护 (Quality Gate & Anti-Injection)**：
  - 生成内容必须满足中文占比与长度硬门槛，跑偏/过短/英文输出自动判定失败并切换下一模型，绝不带病发布。
  - **数字幻觉软校验**：精确小数百分比（`12.53%`）、大额精确金额（`$120,000` / `2.4 亿`）必须能在源文里找到落点；K/M/B 单位缩写会智能还原（`$2.4B` 与 `24 亿美元` 等价），交易员人设的粗略整数（"止损 10%"、"5% 仓位"）不拦截。
  - **白名单含活动情报**：实际注入给模型的当期活动 guidance 也在核对白名单内——模型忠实引用情报里的奖池金额/费率不会再被判成"编造数据"（此前是系统性误杀）。
  - 自动识别并截断 RSS 摘要中夹带的提示词注入指令（"ignore previous instructions" / "无视之前的规则" 等），防止机器人被劫持发言。
- 🎨 **每日深度长文（contentType=2）**：每个 UTC 日首个高热帖（`ARTICLE_MIN_IMPACT` 门槛，默认 20 分）自动升级为 TITLE + 500~800 字深度复盘（纯文本小标题分段、多空两面、结尾给跟踪变量不喊单），短讯抢时效、长文打专业垂直度与长尾流量；生成失败自动降级短讯重试同一条，额度只在真实发布成功后核销。生成截断（finish=length）一律预算扩容重试，残句到预算顶仍截断即拒稿——绝不发半句话。
  - **长度上限按模式区分**：长文 2500 字、短讯 900 字。此前两者共用 900 硬编码，凡通过长文门（≤2500）的稿子在发布时都会被腰斩；且旧的截断写法 `content[:850].rsplit("\n",1)[0]` 在"正文是一整段无换行"时会把最后一个换行之前的全部内容当正文——长文首行是 TITLE，于是整篇正文被丢掉（实测 1380 字长文 → 43 字）。现在只在尾部 30% 内找边界，且越界时优先压缩正文、**保住末尾标签行**。
  - **TITLE 行容忍 Markdown**：模型把标题写成 `**TITLE: xxx**` 时不再误判"缺 TITLE 行"整篇拒稿（解析前先剥 `**`/`__`，与净化同规则、只是提前到门之前）。
- 🚫 **AI 腔检测门**：标志性机器人文风（拭目以待/未来可期/值得注意的是/综上所述/让我们一起 等）出现即废稿换模型重写；软特征（赋能/标志着/不仅…更…/首先…其次）累计 ≥2 才拦；双破折号"——"是硬命中（AI 写作最可靠指纹）。
- 🏷️ **挂件提取三层防线**：预清洗（剥 HTML/URL/裸域名——图片域名 `ctmedia.io` 曾每轮制造 25 个假 $IO）→ 通用大写闸（一切标的需要 `$` 或全大写，小写英文词/Title Case 不再误判）→ 严格词表（AI/BANK/HOME/OG/ACT 等英语常用词撞名币必须 `$` 显式引用）。全语料扫描验证：提取代币 14/14 全真，"AI 技术新闻硬挂 $AI 币"式事实错乱叙事绝迹。
- 🖼️ **配图眼钩升级**：无原图时首选 48H 真实 K 线走势卡（涨绿跌红渐变面积+大字现价涨幅），次选 bars 涨跌条市场情绪卡（真实行情行解析），FNG 仪表盘外链降为最后兜底——每帖一图不重样。
  - **卡片只画 ASCII**：GitHub runner 镜像**不含任何 CJK 字体**（官方镜像的字体包只有 `fonts-noto-color-emoji`），中文标签会渲染成方框。现在卡片文本在渲染入口统一 ASCII 化（情绪词映射成英文、其余非 ASCII 剔除，且剔除时告警一次），标签本身也改为英文——不赌运行环境装没装中文字体。
  - **字体解析可观测**：`_font` 候选表带绝对路径（Linux 上 PIL 的按名搜索覆盖不到 `fonts-noto-cjk` 的安装位），解析结果进健康自检；一个 TTF 都找不到时明确告警，并改用 `load_default(size)` 按尺寸加载内置字体，避免 52px 标题被画成极小一行。
  - **拿不到情绪读数时不画假数字**：情绪指数接口失败时返回显式"未知"（不再返回与真实读数同形的 `50/100 (中立)`），卡片画 `--` 而不是默认 50——用编造的行情数据发帖比不发更糟。
- 🧾 **发布回执**：每帖记录币安 `contentId`（帖子永久标识）与净化/织挂件/标签注入后的**最终发布文本**（质量门只见原稿，实际发出去的是改写稿），运行摘要附帖子直链可人工复核。
- 🎯 **情绪锚点检测收紧**：判定"这篇是否又在拿贪婪指数当梗"的正则此前间隙允许逗号与拉丁字母，`市场情绪偏谨慎，BTC 24 小时涨了 3%` 这类**正常行情句**也会命中——后果是误武装情绪锚点禁令（盘面情绪行被剥离、prompt 注入"严禁提及"），报表还把合规内容记成违规。收紧为"间隙只允许中文/空格、数字紧跟"，真阳性全部保留。
- 🐕 **调度看门狗**：调度静默黑洞（实录 4.5 小时 13 个调度点零投递）时机器人无法自报警——看门狗在每轮真实运行开头查 GitHub 运行历史，上一轮**调度触发**（schedule/repository_dispatch 任一，R221 修正：此前只认已降级为偶发的 schedule，dispatch 健康时会连续误报、dispatch 真死时反而漏报；push 是运行结果不计入）距今超 50 分钟即推送报警并给出检查外部回调的指引（`scripts/schedule_watchdog.py`，判定逻辑可离线单测）。
  - **只报警、绝不阻塞发帖**：脚本全程 try 兜底，workflow 侧另加 `continue-on-error`，报警通道故障也不会连累当轮发帖。
  - **取"上一轮"的口径**：只统计已完成的运行并排除本次自身，手动触发（workflow_dispatch/push）不会让间隔被多算一整个槽位，也不会因 schedule 样本不足而漏报。
  - **不受活跃窗口影响**：cron 是 24/7 的，窗口外只是 main.py 静默退出、心跳照常落点，故看门狗不因 `ACTIVE_HOURS_BEIJING` 关闭检测。
- 💰 **情报资产保护**：币安活动情报仅在 AI 真实分析成功时落盘；分析失败自动沿用上一份历史情报，杜绝低质兜底数据覆写优质资产。
- 🎭 **写派人设轮换 (ShuffleBag)**：三种写派——毒舌老韭菜 / 数据拆解派 / 吃瓜叙事党——每帖随机注入 system prompt，让时间线的"人味"不重样。洗牌袋算法保证任意连续 3 帖恰好三家各一（纯随机在小窗口会扎堆，生产实测踩过），结尾互动套路同样洗牌轮换。
- 🖼️ **发布链路强化**：币安发帖接口遭遇 429/502 等暂态错误自动延迟重试；配图全格式（含 LA/I;16）统一转码标准 JPEG。
  - **解码即内容门**：配图 URL 来自外部 RSS（不可信输入），只有**能被 Pillow 解码成图片**的字节才会被接受并转码为 JPEG；解码失败一律丢弃、改用情绪卡兜底，绝不把原始字节托管到币安 CDN（此前会把 SVG/脚本/二进制垃圾原样上传）。
  - **SSRF 加固**：只放行 http(s) 且解析到公网地址的目标；IPv4-mapped IPv6（`[::ffff:10.0.0.1]`）会先归一成 IPv4 再判，IPv6 ULA（`fc00::/7`）与文档保留段一并拒绝；重定向逐跳复检，最多 3 跳。
    - *已知残余风险（如实标注）*：校验与真实请求是两次独立 DNS 解析，TTL=0 的攻击域理论上可在两次解析之间翻到内网（DNS rebinding）。彻底关闭需把 IP 钉到连接层，属独立改造，当前未做——代码注释里也不再声称已防住。
  - **连接池不泄漏**：带重试的请求在重试前显式关闭上一次响应，避免 `stream=True` 把连接滞留在池里。
  - **抓取有全局 deadline**：`FETCH_DEADLINE_SEC`（默认 300s）到点即放弃迟到源、用已完成候选继续——单源 socket 超时挡不住 drip-feed 型服务器，而一轮卡住会把后续 cron 全部排到后面。
  - **HTML 清洗先截断再清洗**：超长 summary（数十 KB）先截到 20000 字再跑 NFKC 与全局正则，而调用方最终只消费前 1000 字——这段开销落在抓取关键路径上（20 条 × 9 源）。
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
- 🧯 **兜底通报按失败步骤分叉**：主流程异常退出时，纯标准库脚本（不依赖项目依赖）直接推送。它会区分三种情形——发帖成功但**状态回写失败**（提示"下一轮会重复发布，去查 Workflow permissions"）、发帖步骤失败、其它步骤失败——不再笼统断言"未产生任何状态变更"把排障方向带偏。
- 📢 **可选消息通知**：支持绑定 Telegram Bot 或 Webhook（钉钉/飞书/企微/Discord）实时推送发帖结果。
- 🌍 **多平台分发**：`PUBLISH_PLATFORMS` 变量组合启用（默认 `binance`）：
  - `binance` — 币安广场官方 OpenAPI 全自动发帖
  - `telegram` — **Telegram 频道镜像**（全自动）：Bot 拉进频道做管理员，设 `TELEGRAM_MIRROR_CHANNEL_ID`（如 `@mychannel`）即可；带图走 sendPhoto，图拉取失败自动降级纯文本。加密社区原生分发渠道，引流利器
  - `okx_draft` — **OKX 广场草稿直出**：OKX 官方暂无发帖 API（V5 仅交易/行情），逆向 cookie 属违反 ToS 有封号风险故不做。折中方案：每篇 AI 生成内容自动落一份"即贴即用"草稿（正文+配图直链+发布清单）到 `drafts/` 目录随 Git 同步并推送提醒，手机打开复制粘贴到 OKX App 广场约 10 秒，可配合 [OKX 星球创作者激励](https://www.okx.com/zh-hans/campaigns/orbit-creator-monetization)（发文赚 USDT）。保留最近 30 份自动清理
  - 仅副平台模式（如只开 `okx_draft` 或 `telegram`）**无需币安 Key**，任一平台完成投递即入缓存；所有平台实现统一 `BasePublisher` 接口，新平台接入只需一个类
  - **报警 12h 节流**：同一标题的错误报警 12 小时内只推送一次，状态随 Git 同步持久化，LLM 池长期失效也不再被消息轰炸；节流在**确认至少一个渠道投递成功后**才登记，全渠道发送失败不会白白吃掉这条报警的额度。
  - **错误精细化诊断**：发布失败自动翻译成可操作的排障指引（401/403 → 请重新生成 Key、20002/20022 → 内容被风控拦截、220094 → Hashtag 超限），无需翻日志。
  - 未配置任何渠道时通知链路完全静默短路，不写任何状态文件。
- 🪙 **单代币 24h 限流**：同一代币（如 $BTC）24 小时内默认最多发 3 篇，避免全账号时间线被单一币种占据导致粉丝疲劳与算法降权；**任一**命中代币触顶即跳过（此前要求全部代币触顶，导致限流形同虚设）。**高影响放行**：热度分 ≥ `TOKEN_LIMIT_BYPASS_IMPACT`（默认 30，独立门槛）的市场级事件（被盗/ETF/加息等，生产分布 29~34 档）不受该限流约束——报表实录 BTC/XRP/SOL 顶满后 1 日误杀 20 条候选，限流不该吞掉突发；放行与拦截都进运行报告（`token_limit_bypassed` 计数），避免把"限流正常工作"误读成"疯狂拦截"。
- 📌 **依赖版本锁定**：`requirements.txt` 全部声明主版本上限，任何上游 breaking release 都无法自动溜进 CI。
- 🧪 **DRY_RUN 零副作用**：试运行模式下既不真实发帖、也**不写入去重缓存**，可放心反复调试。
- 📋 **Actions 运行报告**：每次运行在 GitHub Actions Summary 页自动生成 Markdown 报告（吞吐漏斗、**每源产出 TOP 排行**、发布明细、命中模型与配图状态、**分阶段耗时画像**），无需翻日志。
- 📉 **失败路径同样留痕**：`metrics.jsonl` 不只记成功——`llm_failed` / `publish_failed` / `*_cache_failed` 全部落盘，可以回答"高热新闻是否被模型池或质量门系统性饿死"，而非只看活下来的稿子。
  - **成功率分母包含投递失败**：`publish_failed` 计入尝试数，不会出现"发布全挂但报表显示 100%"。
  - **分段耗时标注样本量**：LLM/情报/拟人间隔的均值只覆盖"有该字段"的轮次，报表显式标注样本数，避免读出"分段和大于总耗时"的假象。
  - **`--days` 按真实时间比较**：不再用 ISO 字符串字典序（`Z` 结尾的行字典序恒大于 `+00:00`，会让陈旧行永远留在窗口内）。
- 🏥 **一键健康自检**：`python main.py --healthcheck` 探测 LLM 链（含断路状态）、Reasonix 网关、RSS 源健康（含停放中/曾故障明细）、币安现货接口、情绪指数、运行策略，退出码即诊断结论。

---

## 🧩 常用支持模型接口清单 (扫描适配)

系统原生兼容以下常用平台的 OpenAI 格式接口及免费模型（可同时配置多个，自动轮询容灾）：

> 📋 **免费模型名会随站点轮换**：下表标注名按 **R263（2026-09-19）** 双源实测校准——① OpenRouter 官方 `/api/v1/models` 实时目录；② 免费站列表 [awesome-free-ai-coding](https://github.com/mvalentsev/awesome-free-ai-coding)（09-17~19 逐站核验）。遇到 404 先怀疑"平台换名/下架"，去各官方目录核对后再改。

| 提供商名称 | Base URL | 常用/推荐免费模型 | 专用 Secret 变量名 |
| :--- | :--- | :--- | :--- |
| **OpenRouter** | `https://openrouter.ai/api/v1` | `openrouter/free`（聚合路由，默认）<br>`qwen/qwen3.8-27b:free`<br>`z-ai/glm-5.2:free`<br>`deepseek/deepseek-v4-flash-0731:free` | `OPENROUTER_API_KEY` |
| **B.ai** | `https://api.b.ai/v1` | `glm-5.3-flash`（生产在跑）<br>`deepseek-v4-flash`<br>`qwen3.8-flash` | `BAI_API_KEY` |
| **智谱 Z.ai** | `https://api.z.ai/api/paas/v4` | `glm-4.7-flash`（默认，注册赠额度）<br>`glm-4.5-flash`<br>`glm-4.6v-flash`（视觉） | `ZAI_API_KEY` |
| **xkiro** | `https://api.xkiro.com/v1` | `qwen/qwen3.6-plus:free`<br>`minimax/minimax-m3:free` | `XKIRO_API_KEY` |
| **aihubmix** | `https://aihubmix.com/v1` | `coding-glm-5.3-flash-free`（默认，500 次/天）<br>`gemini-3.7-flash-free`<br>`minimax-m3-free` | `AIHUBMIX_API_KEY` |
| **inferera** | `https://api.inferera.com/v1` | `coding-kimi-k3-free`（⚠️ 暂无第二来源核验）<br>`gemini-3.7-flash-free`<br>`minimax-m3-free` | `INFERERA_API_KEY` |
| **TokenRouter** | `https://api.tokenrouter.com/v1` | `nemotron-3-nano-omni`<br>`z-ai/glm-5.3-free` | `TOKENROUTER_API_KEY` |
| **DeepSeek 官方** | `https://api.deepseek.com` | `deepseek-chat` | `LLM_API_KEY` |
| **SiliconFlow (硅基流动)** | `https://api.siliconflow.cn/v1` | `qwen3-8b`（¥0 免实名后免费）<br>`glm-4-9b-0414`<br>`StepFun-xing4.0-29b` | `SILICONFLOW_API_KEY` |
| **bluesminds** | `https://api.bluesminds.com/v1` | `glm-4-flash`（注册赠试用额度）<br>`kimi-k2`<br>`deepseek-chat` | `BLUESMINDS_API_KEY` |
| **🏠 Reasonix 本地网关** ⭐ 本地首选 | `http://localhost:20140/v1` | `auto/best-fast`（自动路由） | **无需 Key**（本地自动发现） |
| **阶跃星辰 Step Plan** 💳 订阅制 | `https://api.stepfun.com/step_plan/v1` | `step-5-preview`（默认，旗舰推理）<br>`step-3.7-flash`<br>`step-3.5-flash` | `STEPFUN_API_KEY` |

### 🏠 Reasonix 本地免费模型网关（本地开发首选）

如果你本机跑着 [Reasonix Gateway](https://github.com/your-reasonix)（聚合 OmniRoute/g4f/OpenCode/OVH/OpenRouter 等 90+ 免费上游的本地统一网关），**什么都不用配置**：发帖机器人在本地启动时会自动探测 `http://localhost:20140/v1`，在线则置顶为首选，离线的 CI 环境自动跳过零干扰：

- **零成本模型池**：网关自动聚合几十路免费上游并做内部容错，本地开发/调试/DRY_RUN 不再消耗任何 API Key 额度
- **首选+备份自动降级链**：探测到网关后同时注册 3 个模型通道（首选 `auto/best-fast` + 网关实际目录中存在的 2 个备份），首选挂掉自动降级到同网关内其他模型，避免整条链退到外部收费路径
- **探测开销极低**：约 2 秒一次 ping，失败静默跳过；`GITHUB_ACTIONS=true` 时自动跳过
- **代理安全**：本地通道强制 `trust_env=False`，规避 Windows TUN/Clash 拦截 localhost 的老坑
- **推理预算**：网关被识别为推理型模型通道，自动扩容 `max_tokens=1500`（聚合路由别名如 `openrouter/free`、`auto/best-fast` 同样按推理配给——路由实际落地模型静态不可见、免费池以思考型为主，按非推理短配会系统性空回；具体模型名仍按 600 节省成本）
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
- `ZAI_API_KEY`: 智谱 Z.ai Key（自动使用 `glm-4.7-flash` 免费层）
- `BLUESMINDS_API_KEY`: bluesminds Key（注册赠试用额度，自动使用 `glm-4-flash`）
- `STEPFUN_API_KEY`: 阶跃星辰 Step Plan 订阅 Key（自动使用 `step-5-preview`，见下方订阅说明）
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
    "model": "qwen/qwen3.8-27b:free"
  },
  {
    "name": "xkiro-Free",
    "base_url": "https://api.xkiro.com/v1",
    "api_key": "sk-你的xkiroKey",
    "model": "qwen/qwen3.6-plus:free"
  }
]
```

#### 💳 阶跃星辰 Step Plan 订阅通道（`STEPFUN_API_KEY`）

Step Plan 是阶跃星辰的**订阅制**服务（¥49~699/月 Credit 池，与按量计费的普通 API 额度独立）：

1. 在 [platform.stepfun.com](https://platform.stepfun.com) 订阅 Step Plan 并创建 **Step API Key**（中国区 key 配 `.com` 域名；国际版 key 配 `.ai`，两者不通用）。
2. 仓库 Secret 添加 `STEPFUN_API_KEY` = 该 Key；`STEPFUN_MODEL` 可选覆盖旗舰通道（默认 `step-5-preview`）。
3. 同一把订阅 Key 会自动再开一条 **`step-3.7-flash` 快速通道**（与 `step-5-preview` 共用 Credit 池、共用 `/step_plan/v1` 端点）：`STEPFUN_FLASH_MODEL` 可选覆盖该模型（默认 `step-3.7-flash`），想固定到 `step-3.5-flash` 等其它同订阅模型时设置。
4. `STEPFUN_PRIORITY`（默认 `1`）把两条订阅通道整体抬到免费池之上——`step-3.7-flash` 再高一档，故冷启动（无延迟遥测）时**先试更快的 flash、再落 step-5、最后才免费池**；设 `0` 关闭促销、退回纯延迟成本排序。
5. 本项目走 OpenAI 协议端点 `https://api.stepfun.com/step_plan/v1`——与 Claude Code 等工具用的 Anthropic 端点 `https://api.stepfun.com/step_plan/v1/messages` 是**同一订阅额度的另一协议面**，计费口径相同。
6. ⚠️ **URL 里的 `/step_plan` 不可删除**：删掉会静默切换到按量计费的普通 API 通道（另一套计费体系，调用成功也不代表在花订阅 Credit）。
7. 本地运行：`$env:STEPFUN_API_KEY="sk-xxxx"; python main.py`。实弹验证 flash 通道可跑 `$env:STEPFUN_API_KEY="sk-xxxx"; python scripts/probe_stepfun.py`（对比 step-5-preview 与 step-3.7-flash 的时延/首句/用量）。

---

## 💻 本地测试与运行

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 全链路健康自检（强烈推荐每次部署/改配置后先跑一下）
python main.py --healthcheck
#   → 一键检查 SQUARE_API_KEY、LLM 链路、Reasonix 网关、RSS 源、币安接口、通知渠道
#   → 含 24h 发帖配额状态与下一槽释放估算（配额满时直接告诉你还要等多久）
#   → 退出码 0 = 可放心运行，1 = 有硬性故障需先修

# 3. DRY_RUN 完整演练（不发帖不写缓存）
$env:SQUARE_API_KEY="your_square_key"
$env:OPENROUTER_API_KEY="sk-or-v1-xxxx"  # 或者不配，自动用本地 Reasonix 网关
$env:DRY_RUN="true"
python main.py

# 4. 正式运行
python main.py

# 5. 运营驾驶舱：成本/延迟/形态对比/拒稿漏斗/人设分布一屏看全
python scripts/cost_analysis.py --days 2

# 6. 遥测简报：发布成功率/运行摘要/内容合规巡检（FNG 锚定/禁用装置/AI 腔）
python scripts/metrics_report.py --days 1
#   --days N 只看最近 N 天（全量口径会稀释近期改善信号）
#   配额满时显示"⏳ 下一配额槽: HH:MM UTC（约 N 分钟）"
#   🔭 开场指纹预警：近 10 帖开场共享前缀 ≥3 即报（新模板指纹成形期可见）
#   ⏱️ 单轮耗时：平均/最长（逼近 20 分钟回调节奏时告警）
#   ⚠️ 全文零有效挂件 / 全文零标签：Write2Earn 返佣生命线失守的直接信号
#   结尾套路分布：验证互动句式轮换均匀性
#   发布成功率为故事口径（同题 failover 多行去重）并列 failover 救回数
```

### 🎬 手动视频发布

把视频文件放到仓库 `assets/videos/` 目录，然后在 GitHub → Actions → **Video Publish (Manual)** → Run workflow：

- 填入视频路径（如 `assets/videos/lp-pool-explainer.mp4`）
- 标题和正文留空则用脚本内置默认文案
- 勾选 `dry_run` 可先只上传验证转码，不发帖
- SQUARE_API_KEY 自动从 Secrets 读取
- 双发守卫：同标题帖子已存在时自动拦截（`--force` 跳过）

本地运行：`SQUARE_API_KEY=xxx python scripts/publish_video.py assets/videos/lp-pool-explainer.mp4`

---

## 📊 内容浏览数据（浏览量归因）

每篇发布帖的回执都带 `content_id`（币安帖子永久标识），但它本身不含浏览量。把账号侧数据接进来后，报表才能回答"哪类帖有流量"：

1. 登录[币安创作者中心](https://www.binance.com/zh-CN/square) → 内容管理 → 导出内容数据 CSV（需含帖子 ID 列，浏览/点赞/评论列可选）
2. 把 CSV 放到 `stats/content_stats.csv`（或用 `CONTENT_STATS_CSV` 指定路径），执行：
   ```bash
   python scripts/import_content_stats.py
   ```
3. 规整结果写入 `content_stats.jsonl`（按帖子 ID 去重、各指标取最大观测——浏览量单调递增，可每周重复导出增量更新）
4. 运行 `python scripts/metrics_report.py`，新增「内容数据」面板：均浏览/点赞/评论 + 时段/体裁/来源三维均浏览（样本 <3 的桶标注小样本）

> 若后续核实到币安 Square OpenAPI 提供内容统计查询接口，只需替换第 2 步的数据来源，`content_stats.jsonl` 的形状与消费面不变。

## ⚙️ 定制与优化建议

- **调整执行频率**：外部 cron（repository_dispatch，20 分钟一次，当前生产形态）或 `.github/workflows/auto_post.yml` 的 `schedule` 兜底。GitHub 自带 schedule 有静默吞投递的风险（看门狗会报警），生产建议保持外部回调。
- **调整单次发帖上限**：可在 Actions 手动触发时指定 `max_posts`（建议保持 1 篇，避免瞬间刷屏）。
- **运维调优参数**：在仓库 **Settings → Secrets and variables → Actions → Variables** 中新增以下变量即可生效（全部无需改代码）：

  | 变量名 | 默认值 | 说明 |
  | :--- | :--- | :--- |
  | `MAX_NEWS_AGE_HOURS` | `48` | 新闻最大时效（小时），超过视为旧闻直接丢弃 |
  | `DUP_SIMILARITY_THRESHOLD` | `0.65` | 跨源近似标题去重阈值（0~1，越小越严格） |
  | `MIN_IMPACT_SCORE` | `0` | 最低热度分门槛（0 表示不过滤，建议 10~15 只发大新闻） |
  | `MAX_DAILY_POSTS` | `12` | 24 小时发帖配额上限，防刷屏保账号权重（0 表示不限） |
  | `ACTIVE_HOURS_BEIJING` | 空 | 北京时间活跃窗口，支持跨夜，例 `8-23` 或 `22-7`（空 = 全天） |
  | `TOKEN_DAILY_LIMIT` | `3` | 同一代币 24h 内最多发帖篇数（0 = 不限）。热度 ≥ `TOKEN_LIMIT_BYPASS_IMPACT` 的高影响故事可绕过 |
  | `TOKEN_LIMIT_BYPASS_IMPACT` | `30` | 单币限流的高影响放行门槛：触顶代币的热度分达到此值仍放行（对齐真实事件档 29~34；20~26 的常规行情帖回到限流，防止高频标的无限穿透） |
  | `MAX_TOKENS_PER_POST` | `3` | 单帖 $ 挂件标的上限（清单式行情日评可提取 9+ 币，截断保留显著度前 N） |
  | `ARTICLE_PER_DAY` | `1` | 每日深度长文开关：当天首个高热帖升级为长文（contentType=2，TITLE+500~800 字正文），打专业垂直度与长尾流量 |
  | `ARTICLE_MIN_IMPACT` | `20` | 长文选稿门槛：榜首热度分低于此值则当天不发长文（全发短讯）。与限流放行门槛 `TOKEN_LIMIT_BYPASS_IMPACT` 相互独立 |
  | `PUBLISH_PLATFORMS` | `binance` | 发布平台组合（逗号分隔）：`binance` 官方 API / `okx_draft` OKX 草稿直出 / `telegram` 频道镜像 |
  | `FETCH_DEADLINE_SEC` | `300` | 单轮 RSS 抓取的全局 deadline（秒）：到点放弃迟到源、用已完成候选继续，避免卡住的源拖满 workflow 并挤掉后续 cron |
  | `LOG_LEVEL` | `INFO` | 日志级别（排障时可设 `DEBUG`） |
  | `MAX_POSTS_PER_RUN` | `2` | 单次运行最大发帖数（workflow 运行参数；配额剩余不足时自动收敛） |
- **模型覆盖变量**（可选，默认用各平台的聚合路由模型）：`OPENROUTER_MODEL` / `BAI_MODEL` / `ZAI_MODEL` / `XKIRO_MODEL` / `AIHUBMIX_MODEL` / `INFERERA_MODEL` / `TOKENROUTER_MODEL` / `SILICONFLOW_MODEL` / `STEPFUN_MODEL` / `STEPFUN_FLASH_MODEL` / `BLUESMINDS_MODEL`——想把某平台固定到指定模型时设置。
- **报警通知渠道**（全部可选，多渠道并发）：`SERVERCHAN_KEY`（Server酱微信）/ `PUSHPLUS_TOKEN`（PushPlus 微信）/ `BARK_KEY`（iOS Bark）/ `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`（Telegram）/ `WEBHOOK_URL`（钉钉/飞书/企微/Discord 通用）。同一错误报警 12 小时同题节流，投递成功才计额度。
- **CI 回归防线**：`tests/test_core.py` 内置百余个离线回归测试（含断路器/源停放/报错分类/通知编码/跨语言去重/行情缓存/同步契约），`.github/workflows/ci.yml` 在每次 push/PR 时自动编译、校验 workflow 语法并跑测试，防止守护逻辑被后续改动悄悄破坏。
  - **workflow 内嵌脚本校验的环境降级**：`scripts/validate_workflows.py` 只在确认本机 bash **真能执行** `bash -n -c true` 时才校验内嵌 shell；PATH 上只有 WSL 启动器、或 bash 被安全策略拒绝时，一律跳过并说明原因，**绝不把环境故障伪装成 workflow 语法错误**。需强制指定时用环境变量 `BASH_PATH=/path/to/bash`。

- **DRY_RUN 语义**：手动触发选择 `dry_run=true` 时，完整跑通抓取/打分/AI/配图流水线，但不真实发帖也**不写入去重缓存**，适合验收。（DRY 遥测行自动打标隔离，报表与调度评分默认排除。）
- **热点感知四路信号**：① 新闻时效与关键词打分（基础排序）；② 币安官方活动重点代币加权（+8，参与创作者激励的入口）；③ CoinGecko 全网热搜加权（+6，市场"正在搜什么"的实时注意力信号，5 分钟缓存、失败静默降级）；④ 全网实时热点钩子（+4，HN 前页 RSS，15 分钟缓存——加密稿标题/摘要命中专有名词或 $TICKER 时前移排序，prompt 注入当轮热点标题供跨域角度，**无关则禁止生硬提及**）。标的识别带全名别名召回（Bitcoin/比特币/比特幣→$BTC 等无歧义全名直接映射，覆盖英文媒体正文只用全名的场景，简繁两种字形都收——动区 BlockTempo 等繁体源写"比特幣/以太幣"）与四层防误报防线（预清洗/大写闸/严格词表/别名）。
- **运行漏斗遥测**：每次运行恰好一条 `run_summary` 遥测行——候选数、发布数、六类跳过计数、未处理数、源健康快照与当轮热搜快照，`python scripts/metrics_report.py` 一键直读。
  - **覆盖全部退出路径**：活跃窗口外 / 配额饱和 / 零候选 / 正常收尾，以及**三条硬退出**（缺 `SQUARE_API_KEY`、无任何 LLM 提供商、未捕获崩溃）——此前硬退出一条遥测都不写，报表的运行轮数会系统性少算，与 Actions 实际 dispatch 数对不上。
  - **抓取前跳过不伪装成正常轮**：配额饱和轮的 Actions 报告显示「本轮跳过: 配额满跳过抓取（12/12）」，不再把跳过原因塞进"全网情绪指数"位、也不再输出"扫描 0 条"的全零吞吐。
