# 🚀 Binance Square 加密热点多模型自动发帖机器人 (Pro 增强版)

> **0 服务器成本 · 0 常驻进程 · GitHub Actions 全自动定时运行 · 多 LLM 容灾故障转移 · 币安广场 OpenAPI 自动发布**

本项目专为加密货币创作者（Binance Square Creator）打造，定时抓取顶级加密快讯，支持通过 **OpenRouter、B.ai、xkiro、aihubmix、inferera、TokenRouter、DeepSeek、硅基流动** 等多模型池进行智能提炼、行情点评与代币标签提取（触发 Write to Earn），并自动发布至币安广场。

---

## 🌟 核心优势与特色

- 🆓 **0 服务器成本**：基于 GitHub Actions 定时触发（默认每 30 分钟），无任何服务器或云函数费用。
- 🔄 **多模型池与自动故障转移 (Auto-Failover)**：
  - 针对免费模型平台（如 OpenRouter、B.ai 等）常见的并发限制、429 Rate Limit、偶发超时等问题，内置**智能容灾切换机制**。
  - 主模型遇到异常时，自动秒级切换至备用模型提供商，确保发帖流程 100% 稳定不中断。
- 🧠 **币安广场 Write to Earn 深度适配**：
  - 自动提炼核心事实（80~150 字），言简意赅。
  - 自动输出 1 句精辟行情与趋势点评。
  - 严格且精准提取 1~2 个大写 `$TOKEN`（如 `$BTC`、`$SOL`），触发币安交易组件与返佣。
  - 结尾附带互动话题问答，提升评论互动率。
  - 内置敏感词与合规风控，过滤“带单/稳赚”等违禁词。
- 🛡️ **严格防重复**：本地 `sent_cache.json` 结合 SHA256 哈希 ID 去重，执行后通过 Git 自动回写提交。
- 📢 **可选消息通知**：支持绑定 Telegram Bot 或 Webhook（钉钉/飞书/企微/Discord）实时推送发帖结果。

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

- **调整执行频率**：修改 `.github/workflows/auto_post.yml` 中的 `cron: '*/30 * * * *'`（默认每 30 分钟）。
- **调整单次发帖上限**：可在 Actions 手动触发时指定 `max_posts` 或设置仓库变量 `MAX_POSTS_PER_RUN`（建议保持 1 篇，避免瞬间刷屏）。
