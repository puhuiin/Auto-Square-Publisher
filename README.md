# 🚀 Binance Square 加密热点全自动发帖机器人

> **0 服务器成本 · 0 常驻进程 · GitHub Actions 全自动定时运行 · DeepSeek/OpenAI 智能提炼 · 币安广场 OpenAPI 自动发布**

本项目是一个专为加密货币内容创作者（币安创作者 / Binance Square Creator）打造的自动化发帖系统。定时抓取海内外顶级加密媒体的最新快讯热点，利用大语言模型（默认 DeepSeek）进行结构化提炼、行情点评、代币标签提取（触发 Write to Earn）并自动发布至币安广场，使用 Git 状态回写机制实现零成本去重持久化。

---

## 🌟 核心特性

- 🆓 **零成本自动化**：依托 GitHub Actions 定时触发（默认每 30 分钟），无需自备云服务器。
- 📡 **多源热点监听**：内置 BlockTempo（动区中文）、Cointelegraph、CoinDesk、Decrypt、Bitcoin Magazine 等优质免鉴权 RSS 源，支持自动降级重试。
- 🧠 **AI 智能提炼**：兼容 OpenAI 接口格式（DeepSeek / GPT-4o / 硅基流动等），严格约束字数（80~150字）、1句核心点评、精准提取 1~2 个 `$TOKEN`（如 `$BTC`、`$SOL`）触发币安交易组件、附带互动讨论提问。
- 🛡️ **严格防重机制**：采用确定性哈希算法与 `sent_cache.json` 跟踪记录，GitHub Actions 执行后自动 `git push` 回写最新状态，杜绝重复发帖。
- 🔧 **安全与调试模式**：支持 `DRY_RUN` 试运行模式，支持在 GitHub Actions 页面一键手动触发（`workflow_dispatch`）。

---

## 📁 目录结构

```text
├── .github/
│   └── workflows/
│       └── auto_post.yml      # GitHub Actions 定时工作流
├── main.py                    # 核心发帖业务逻辑与模块实现
├── requirements.txt           # Python 依赖清单
├── sent_cache.json            # 已发送历史记录与去重缓存（自动回写）
├── .gitignore                 # Git 忽略配置
└── README.md                  # 项目使用指南
```

---

## 🔑 准备工作与 API Key 获取

在开始部署前，请准备好以下两项凭证：

### 1. 币安广场 OpenAPI Key (`SQUARE_API_KEY`)
1. 登录 [币安广场创作者中心 (Binance Square Creator Center)](https://www.binance.com/zh-CN/square)。
2. 进入 **API 管理 (API Management / AI Skills)**。
3. 创建并生成用于发帖的 **Square OpenAPI Key**（此 Key 仅具备广场发帖权限，无法操作资产，请妥善保管）。

### 2. LLM API Key (`LLM_API_KEY`)
1. 推荐使用 [DeepSeek 开放平台](https://platform.deepseek.com/) 获取 API Key（极高性价比且中文表现优异）。
2. 也支持任意兼容 OpenAI 接口规范的模型服务提供商（如 OpenAI、Moonshot、SiliconFlow 等）。

---

## 🚀 GitHub Actions 快速部署（3 分钟搞定）

### 第一步：创建 GitHub 仓库并推送代码
1. 在 GitHub 上新建一个仓库（公开或私有均可，建议 **Private 私有仓库**）。
2. 将本项目所有文件提交并推送到你的 GitHub 仓库主分支（`main`）。

### 第二步：开启 GitHub Actions 读写权限（至关重要 ⚠️）
为了让 Actions 在发帖后能够自动将更新后的 `sent_cache.json` 提交回仓库：
1. 打开 GitHub 仓库页面，点击 **Settings** -> **Actions** -> **General**。
2. 滚动到底部的 **Workflow permissions**。
3. 勾选 **Read and write permissions**，并勾选 **Allow GitHub Actions to create and approve pull requests**。
4. 点击 **Save** 保存。

### 第三步：配置 GitHub Secrets（密钥环境变量）
1. 打开 GitHub 仓库页面，点击 **Settings** -> **Secrets and variables** -> **Actions**。
2. 点击 **New repository secret**，依次添加以下变量：

| Secret 变量名 | 必填 | 说明 | 示例值 |
| :--- | :---: | :--- | :--- |
| `SQUARE_API_KEY` | **是** | 币安广场 OpenAPI Key | `your_square_api_key` |
| `LLM_API_KEY` | **是** | DeepSeek / OpenAI 的 API Key | `sk-xxxxxxxxx` |
| `LLM_BASE_URL` | 否 | LLM 接口 Base URL（默认为 DeepSeek） | `https://api.deepseek.com` |
| `LLM_MODEL` | 否 | 模型名称（默认为 deepseek-chat） | `deepseek-chat` |

### 第四步：手动触发测试运行
1. 进入仓库的 **Actions** 标签页。
2. 在左侧列表中选择 **Binance Square Auto Poster**。
3. 点击右侧的 **Run workflow** 下拉框：
   - 可选择 `dry_run = true` 先进行一次无损模拟发帖测试。
   - 点击绿色的 **Run workflow** 按钮启动任务。
4. 点击任务查看运行日志，确认成功后即可静待每 30 分钟定时自动运行！

---

## 💻 本地测试与调试

如果你希望在本地快速调试或单次运行：

### 1. 克隆项目与安装依赖
```bash
git clone <your-repo-url>
cd <your-repo-dir>

# 创建并激活虚拟环境 (可选)
python -m venv venv
# Windows 激活:
venv\Scripts\activate
# Linux/macOS 激活:
# source venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置本地环境变量运行
```bash
# Windows PowerShell 环境:
$env:SQUARE_API_KEY="your_binance_square_api_key"
$env:LLM_API_KEY="your_llm_api_key"
$env:LLM_BASE_URL="https://api.deepseek.com"
$env:LLM_MODEL="deepseek-chat"
$env:DRY_RUN="true"   # 设置为 true 开启模拟测试模式，不实际调用币安发帖

# 执行脚本
python main.py
```

```bash
# Linux / macOS 环境:
export SQUARE_API_KEY="your_binance_square_api_key"
export LLM_API_KEY="your_llm_api_key"
export LLM_BASE_URL="https://api.deepseek.com"
export LLM_MODEL="deepseek-chat"
export DRY_RUN="true"

# 执行脚本
python main.py
```

---

## ⚙️ 进阶配置与自定义

### 1. 修改定时执行频率
修改 `.github/workflows/auto_post.yml` 中的 `cron` 表达式：
- `*/15 * * * *`：每 15 分钟执行一次
- `*/30 * * * *`：每 30 分钟执行一次（默认推荐）
- `0 * * * *`：每 1 小时执行一次

### 2. 调整单次发帖上限
在 `auto_post.yml` 或 GitHub Repository Variables 中设置 `MAX_POSTS_PER_RUN`（默认建议每次 `1` 篇，避免瞬间多条刷屏）。

### 3. 添加或调整 RSS 数据源
在 `main.py` 的 `RSS_FEEDS` 列表中，可以任意增减你关注的加密新闻 RSS 源。

---

## 📜 免责声明
- 本项目仅供学习与内容自动化辅助使用，请遵守币安广场社区规范与各数据源使用条款。
- 自动化内容生成受大模型幻觉与原始快讯准确度影响，请合理设置发帖频率并定期复核内容质量。
