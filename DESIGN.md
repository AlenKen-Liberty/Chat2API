# Chat2API — 设计文档

> 将 Gemini CLI 和 Codex 的 OAuth 账号池包装为标准 OpenAI-compatible API，
> 本机自用，调用方只需知道模型名，无需关心账号细节。

---

## 1. 项目目标

| 目标 | 描述 |
|------|------|
| **统一 API** | 提供 OpenAI Chat Completions 兼容接口 (`/v1/chat/completions`)，支持流式和非流式 |
| **账号透明** | 调用方不感知底层账号池，请求自动路由到最优账号 |
| **跨 Provider 降级** | 按能力分档（高/中/低），配额耗尽时跨 provider 降级到同档或下一档可用模型 |
| **反检测** | 模拟真实 CLI 客户端行为，最大程度降低被封号风险 |
| **本机自用** | 仅监听 127.0.0.1，无需认证，无需 API key |
| **独立代码** | 借鉴 code-orchestra/models 的思想，在本项目中重写，不依赖外部路径 |

**当前阶段 Provider：** Gemini CLI + Codex
**未来扩展：** Antigravity、GitHub Copilot、Perplexity

---

## 2. 系统架构

```
┌─────────────────────────────────────────────────────┐
│                   调用方 (任意程序)                    │
│         POST /v1/chat/completions                    │
│         {"model": "gemini-2.5-pro", ...}             │
└──────────────────────┬──────────────────────────────┘
                       │ HTTP (127.0.0.1:8000)
                       ▼
┌─────────────────────────────────────────────────────┐
│                   Chat2API Server                     │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────┐  │
│  │ Router      │→ │ AccountPool  │→ │ TierMapper │  │
│  │ (FastAPI)   │  │ (选账号)      │  │ (分档降级)  │  │
│  └─────────────┘  └──────────────┘  └────────────┘  │
│  ┌──────────────────────────────────────────────────┐│
│  │              Provider Backends                    ││
│  │  ┌─────────────────┐  ┌────────────────────┐    ││
│  │  │ GeminiBackend   │  │ CodexBackend       │    ││
│  │  │ (CLI Protocol)  │  │ (Codex Protocol)   │    ││
│  │  └─────────────────┘  └────────────────────┘    ││
│  └──────────────────────────────────────────────────┘│
│  ┌─────────────────┐  ┌───────────────────────────┐  │
│  │ QuotaTracker    │  │ AntiDetection Layer      │  │
│  │ (实时配额追踪)   │  │ (指纹/速率/行为模拟)      │  │
│  └─────────────────┘  └───────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

---

## 3. 模型路由与跨 Provider 降级

### 3.1 核心发现（2026-03-18 实测）

**Gemini CLI: 所有 7 个模型共享同一个配额池**
```
gemini-3.1-pro-preview        ┐
gemini-3-pro-preview          │
gemini-2.5-pro                │
gemini-3-flash-preview        ├── 100% 同一配额，同一重置时间
gemini-2.5-flash              │   在这些模型之间降级毫无意义！
gemini-3.1-flash-lite-preview │
gemini-2.5-flash-lite         ┘
```

**Codex: 所有模型共享一个 weekly 配额池**
```
gpt-5.4              prio=0   旗舰      ┐
gpt-5.4-mini         prio=2   小旗舰    │
gpt-5.3-codex        prio=3   上代优化  │
gpt-5.2-codex        prio=7             ├── weekly 配额共享
gpt-5.2              prio=8             │   在这些模型之间降级也无意义！
gpt-5.1-codex-max    prio=9   深度推理  │
gpt-5.1-codex-mini   prio=18  轻量     │
gpt-oss-120b/20b     隐藏     开源模型  ┘
```

### 3.2 设计思路转变

原设计假设同 provider 内不同模型有不同配额组（如 Pro 组 vs Flash 组），可以在同 provider 内降级。

**实测结论：每个 provider 只有一个配额池。** 因此：
- 同 provider 内部不需要降级（没意义，随便用最好的）
- **真正有意义的降级只有：Provider A 的所有账号耗尽 → 切到 Provider B**
- 用户请求一个模型时，应该**总是用该 provider 最好的版本**（配额相同，何不用最好的？）

### 3.3 新的路由策略：跨 Provider 等价映射

```
用户请求的模型 → 确定 provider → 尝试该 provider 所有账号
                               → 全部耗尽 → 切到对等 provider 的等价模型
```

**跨 Provider 能力等价表：**

| 能力档位 | Gemini (首选模型) | Codex (首选模型) | 说明 |
|---------|-------------------|------------------|------|
| **旗舰** | gemini-3.1-pro-preview | gpt-5.4 | 最强推理能力 |
| **强力** | gemini-3-pro-preview | gpt-5.3-codex | 上一代旗舰 |
| **标准** | gemini-2.5-pro | gpt-5.2 | 稳定可靠 |
| **快速** | gemini-3-flash-preview | gpt-5.4-mini | 速度优先 |
| **轻量** | gemini-2.5-flash | gpt-5.1-codex-mini | 简单任务 |
| **超轻** | gemini-2.5-flash-lite | gpt-oss-120b | 最低成本 |

### 3.4 resolve_model 逻辑（重写）

```python
# 每个 provider 只有一个配额池 (quota_group)
PROVIDER_QUOTA_GROUPS = {
    "gemini": "gemini-all",    # 所有 Gemini 模型共享一个日配额
    "codex":  "codex-weekly",  # 所有 Codex 模型共享一个周配额
}

# 跨 provider 等价映射（按能力档位）
CROSS_PROVIDER_FALLBACK = {
    # Gemini → Codex
    "gemini-3.1-pro-preview":        "gpt-5.4",
    "gemini-3-pro-preview":          "gpt-5.4",
    "gemini-2.5-pro":                "gpt-5.3-codex",
    "gemini-3-flash-preview":        "gpt-5.4-mini",
    "gemini-2.5-flash":              "gpt-5.4-mini",
    "gemini-3.1-flash-lite-preview": "gpt-5.1-codex-mini",
    "gemini-2.5-flash-lite":         "gpt-5.1-codex-mini",

    # Codex → Gemini
    "gpt-5.4":              "gemini-3.1-pro-preview",
    "gpt-5.4-mini":         "gemini-3-flash-preview",
    "gpt-5.3-codex":        "gemini-2.5-pro",
    "gpt-5.2-codex":        "gemini-2.5-pro",
    "gpt-5.2":              "gemini-2.5-pro",
    "gpt-5.1-codex-max":    "gemini-3.1-pro-preview",
    "gpt-5.1-codex-mini":   "gemini-2.5-flash",
    "gpt-oss-120b":         "gemini-2.5-flash-lite",
    "gpt-oss-20b":          "gemini-2.5-flash-lite",
}

# 能力降级链（当等价模型的 provider 也耗尽时）
CAPABILITY_DOWNGRADE = {
    "gemini-3.1-pro-preview": "gemini-3-pro-preview",  # 无意义（同配额），仅作映射用
    "gpt-5.4": "gpt-5.3-codex",                        # 同上
    # 降级链的真正作用：当两个 provider 都耗尽 tier N 时，降到 tier N-1
}

def resolve_model(requested: str, exhausted_providers: set) -> (str, Account, Backend):
    """
    1. 识别 requested 属于哪个 provider
    2. 遍历该 provider 的所有账号，找到有配额的
    3. 该 provider 全部耗尽 → 查 CROSS_PROVIDER_FALLBACK，切到对等模型
    4. 对等 provider 也耗尽 → 向下降级一档，重复 2-3
    5. 全部耗尽 → 503
    """

def _find_model_or_suggest(requested: str) -> ModelEntry:
    """
    精确匹配 → 返回 ModelEntry
    模糊匹配（前缀/别名）→ 返回最接近的
    完全未知 → 400 + 建议：
      {"error": "Unknown model 'gemin-2.5-pro'. Did you mean 'gemini-2.5-pro'?"}
    """
```

### 3.5 "总是用最好的" 策略

由于同 provider 内所有模型共享配额，我们提供一个智能升级选项：

```yaml
# config.yaml
routing:
  auto_upgrade: true  # 当用户请求 gemini-2.5-flash，自动升级到 gemini-3.1-pro-preview
```

当 `auto_upgrade: true` 时：
- 用户请求任何 Gemini 模型 → 自动使用 gemini-3.1-pro-preview（配额相同，用最好的）
- 用户请求任何 Codex 模型 → 自动使用 gpt-5.4
- 响应头 `X-Chat2API-Actual-Model` 告知实际使用的模型

当 `auto_upgrade: false` 时：
- 尊重用户的模型选择（有些场景故意用小模型求快速响应）

### 3.6 降级信息反馈

降级时在响应头中告知调用方：
```
X-Chat2API-Requested-Model: gemini-2.5-pro
X-Chat2API-Actual-Model: gpt-5.4
X-Chat2API-Degraded: true
X-Chat2API-Degraded-Reason: gemini-all-exhausted
```

### 3.7 未来：CLI 排名编辑器

```bash
chat2api models rank          # 显示当前排名
chat2api models rank --edit   # 交互式编辑（上下键调整顺序）
chat2api models list          # 从所有 provider 拉取实际可用模型
```

---

## 4. 账号池管理

### 4.1 代码策略

借鉴 `code-orchestra/models` 的思想和算法，在本项目中重写：

- **账号存储**：复用相同的 `~/.gemini/accounts/` 和 `~/.codex/accounts/` 存储路径和 JSON 格式
  - 这样已有的账号数据可以直接使用，不需要重新登录
  - 但代码本身独立，不 import code-orchestra
- **OAuth 刷新**：重写 token refresh 逻辑（Gemini 用 Google OAuth2，Codex 用 OpenAI OAuth）
- **配额查询**：重写配额 API 调用（Gemini 的 `retrieveUserQuota`，Codex 的 `wham/usage`）
- **评分算法**：借鉴 QuotaManager 的 `score = remaining + waste_urgency + safety_bonus + inertia` 公式

**OAuth 凭据配置：**
对于 Gemini，出于安全/隐私考虑，我们不在代码中硬编码任何 Google OAuth 凭据。用户必须在 `.env` 中提供自己的凭据或公开的 OEM 凭据才能成功执行 token refresh：
- `GEMINI_CLIENT_ID`
- `GEMINI_CLIENT_SECRET`

**首次登录职责边界：**

Chat2API **不负责** 首次 OAuth 登录（浏览器授权流）。用户必须先通过以下方式创建好本地凭证文件：
- Gemini: 运行 `gemini` CLI 完成登录 → 生成 `~/.gemini/` 下的 token 文件
- Codex: 运行 `codex --full-setup` → 生成 `~/.codex/auth.json`

Chat2API 只做 **token refresh**。当 refresh 彻底失败时（refresh_token 过期或被撤销）：
1. 从 AccountPool 中临时剔除该账号（标记 `disabled`）
2. API 层若所有同 provider 账号都失效，返回明确错误：
   ```json
   {"error": {"message": "All Gemini accounts need re-login. Run 'gemini' CLI to refresh.", "type": "auth_error"}}
   ```
3. 后台定期重试 refresh（每 10 分钟），成功后自动恢复

### 4.2 AccountPool — 模型级账号选择

```python
class AccountPool:
    """
    按模型选择最优账号（区别于 QuotaManager 的全局账号切换）。

    核心逻辑：
    1. 收到请求 model_id → 确定需要哪个 provider + quota_group
    2. 遍历该 provider 所有账号，查询该 quota_group 的剩余配额
    3. 用评分公式选出最优账号
    4. 429 时标记该账号该 quota_group 耗尽，切到下一个
    """
```

### 4.3 并发安全与 Busy-Skip

单账号单并发锁使用 **try_acquire（非阻塞）** 而不是排队等待：

```python
class AccountLock:
    """每个账号一把 asyncio.Lock，非阻塞获取"""

    async def try_acquire(self, email: str) -> bool:
        """尝试获取锁，不等待。返回 False 表示该账号正忙。"""
        lock = self._locks[email]
        return lock.locked() is False and await lock.acquire()
```

**行为：** 当 Account A 正在处理请求时，新请求不会排队等待，而是：
1. 标记 A 为 busy，跳过
2. 尝试同 tier/group 的下一个空闲账号 B
3. 同 group 全部 busy → 降级到下一 tier
4. 全部 busy → 返回 503（"All accounts busy, retry later"）

这避免了排队导致的严重阻塞，同时也不会因快速消耗备用账号引发伪 429（因为一个请求完成后锁立即释放，对 provider 来说每个账号始终是单线程的）。

其他安全措施：
- 配额缓存 TTL: 60 秒（避免每次请求都查 API）
- 429 错误时立即标记账号，触发 re-route 到下一个

---

## 5. Provider Backend 协议

### 5.1 Gemini Backend (模拟 Gemini CLI)

**关键 endpoint：**
```
POST https://cloudcode-pa.googleapis.com/v1internal/projects/{project_id}/locations/global/agents/codegen:streamGenerateContent
```

**请求格式（模拟 Gemini CLI 3.x）：**
```json
{
  "model": "models/gemini-2.5-pro-exp-03-25",
  "contents": [
    {"role": "user", "parts": [{"text": "..."}]}
  ],
  "generationConfig": {
    "temperature": 1.0,
    "topP": 0.95,
    "topK": 64,
    "maxOutputTokens": 65536,
    "responseMimeType": "text/plain",
    "thinkingConfig": {
      "thinkingBudget": 32768
    }
  },
  "systemInstruction": {
    "parts": [{"text": "..."}]
  }
}
```

**Headers（必须精确匹配 Gemini CLI）：**
```
Authorization: Bearer {access_token}
Content-Type: application/json
User-Agent: GeminiCLI/1.0.0
x-goog-api-client: GeminiCLI/1.0.0
```

**响应处理：**
- 流式 SSE 响应 → 转换为 OpenAI `data: {...}` SSE 格式
- 非流式 → 提取 `candidates[0].content.parts[0].text` → 包装为 OpenAI response

### 5.2 Codex Backend (模拟 Codex CLI)

**关键 endpoint：**
```
POST https://api.openai.com/v1/responses
```

**请求格式（Codex responses API）：**
```json
{
  "model": "codex-mini-latest",
  "input": [
    {"role": "user", "content": "..."}
  ],
  "stream": true
}
```

**Headers：**
```
Authorization: Bearer {access_token}
ChatGPT-Account-Id: {account_id}
User-Agent: codex-cli/1.0.0
Content-Type: application/json
```

---

## 6. 反检测设计

### 6.1 为什么模拟 CLI 会被检测？

通过研究已有项目（Antigravity Manager, ZeroGravity, CLIProxyAPI 等）和实际封号案例，总结主要检测手段：

#### 6.1.1 TLS 指纹 (JA3/JA4)

| 检测方式 | 说明 |
|----------|------|
| **JA3 hash** | TLS ClientHello 中的 cipher suites、extensions、EC 参数的 hash |
| **JA4 fingerprint** | 升级版，按字母排序 extensions，包含 ALPN、SNI 等信息 |
| **H2 fingerprint** | HTTP/2 的 SETTINGS frame、PRIORITY frame、WINDOW_UPDATE 等参数 |

**问题：** Python 的 `urllib`/`requests`/`aiohttp` 使用 OpenSSL，其 TLS 指纹与 Chrome/Node.js 完全不同。Google 可以轻松区分「来自 Electron 应用的请求」和「来自 Python 脚本的请求」。

#### 6.1.2 请求模式分析

| 检测方式 | 说明 |
|----------|------|
| **请求频率** | 真实 CLI 用户有思考间隔，代理转发通常密集且均匀 |
| **并发模式** | 真实用户单线程使用，代理可能并发多请求 |
| **Session 生命周期** | 真实 IDE 有 warmup、heartbeat、shutdown 序列 |
| **请求大小分布** | 人类输入有自然分布，API 调用通常更大更均匀 |

#### 6.1.3 Client Identity 泄露

| 检测方式 | 说明 |
|----------|------|
| **User-Agent 不匹配** | 发了 Gemini CLI 的 UA 但 TLS 指纹是 Python |
| **缺失 telemetry** | 官方 CLI 发送额外遥测数据，代理通常不发 |
| **OAuth scope 异常** | Token 的 scope 与声称的 client 不匹配 |

#### 6.1.4 账号行为异常

| 检测方式 | 说明 |
|----------|------|
| **IP 跳变** | 同一账号短时间内从不同 IP 发请求 |
| **配额消耗速度** | 非人类的高速配额消耗 |
| **多账号同 IP** | 多个账号从同一 IP 发请求（多账号轮转的特征） |

### 6.2 我们的反检测策略

#### 策略 1: TLS 指纹匹配

```
优先级: 极高
```

**关键问题：UA 与 TLS 指纹一致性**

审阅中指出了一个重要矛盾：如果我们发送 `User-Agent: GeminiCLI/1.0.0` 但 TLS 指纹是 Chrome 124，这个 mismatch 本身就是一个检测特征。真正的 Gemini CLI 基于 Node.js，其 TLS 指纹既不是 Chrome 也不是 Python。

**分级策略：**

| 方案 | 适用场景 | 说明 |
|------|---------|------|
| **A: curl_cffi + Node.js 指纹** | 首选 | curl_cffi 支持 `impersonate="safari"` 等多种指纹，需要抓包确认 Node.js 最接近哪个 |
| **B: Node.js sidecar** | 最精确 | 用 Node.js 子进程发请求，TLS 指纹天然匹配 Gemini CLI |
| **C: 原生 Python** | 兜底 | 不做 impersonate，保持 OpenSSL 原生指纹，反而比硬套 Chrome 更不可疑（不会产生 UA/TLS mismatch） |

**Phase 1 实施计划：**
1. 先用 curl_cffi 默认配置跑通（不 impersonate），确保功能正确
2. 抓包对比真实 Gemini CLI (Node.js) 的 JA3/JA4 指纹
3. 在 curl_cffi 支持的 impersonate 列表中找最接近的匹配
4. 如果差距太大，切换到方案 B（Node.js sidecar）

```python
# Phase 1: 可配置的 TLS 策略
class TLSClient:
    def __init__(self, strategy: str = "native"):
        # "native"    → curl_cffi 不 impersonate（兜底）
        # "node"      → 最接近 Node.js 的 impersonate 指纹
        # "chrome"    → Chrome 指纹（仅用于无 UA 的场景）
        # "sidecar"   → 实际 Node.js 子进程
        self.strategy = strategy
```

#### 策略 2: 速率限速（不阻塞单个请求）

~~原设计包含 pre_request_delay（模拟人类思考间隔），但这会严重影响 API 客户端体验（Chatbox、VSCode 插件等会触发 Timeout）。~~

**改为：** 仅通过 RPM 限速控制整体节奏，不在单个请求前添加人为延迟。

```python
class RateLimiter:
    """
    每账号 RPM 限速，超出时拒绝（返回 429）而不是阻塞等待。
    调用方可以重试，由 AccountPool 路由到其他账号。
    """
    async def check(self, account_email: str) -> bool:
        """返回 True 表示可以继续，False 触发 429"""
```

这样既控制了请求频率（从 provider 侧看不到异常密集的请求），又不破坏 API 客户端的响应体验。

#### 策略 3: 会话粘性

```
本机自用，一个调用方通常只有一个"会话"。
账号绑定到活跃请求流，而不是每次请求都轮转。
```

- 默认使用评分最高的账号，直到配额不足才切换
- 切换时有惯性（inertia），避免在两个差距不大的账号间反复跳

#### 策略 4: 速率控制

```python
RATE_LIMITS = {
    "gemini_cli": {
        "per_account_rpm": 10,      # 真实 CLI 用户的典型速率
        "per_account_daily": 200,   # 远低于官方 1000/day 限制
        "global_rpm": 30,           # 所有账号总计
    },
    "codex": {
        "per_account_rpm": 5,
        "burst_cooldown": 300,      # burst 配额用完后冷却 5 分钟
    }
}
```

#### 策略 5: 单账号单 Session 原则（Busy-Skip）

```
核心原则：在任何时刻，一个账号只服务一个请求。
不会出现同一个 token 同时发出多个请求的情况。
```

这是最重要的反检测措施之一。真实的 CLI 工具是单用户单线程的，如果一个 token 同时出现在多个并发请求中，几乎一定会被标记。

**实现方式：** 非阻塞 try_acquire（详见 4.3 Busy-Skip 机制）。
当账号 A 正忙时，新请求自动路由到空闲的账号 B，而不是排队阻塞。
这既保证了每个账号对 provider 来说是单线程的，又不牺牲 API 客户端的响应速度。

#### 策略 6: IP 管理建议

```
- 理想情况：每个账号绑定固定的出口 IP
- 最低要求：避免多账号共享同一 IP
- 可选：使用住宅代理池，每个账号分配独立代理
```

> 注意：本项目不内置代理功能，但提供配置接口让用户为每个账号指定出口代理。

---

## 7. API 设计

### 7.1 OpenAI-Compatible Endpoints

```
POST /v1/chat/completions          # Chat completion (流式/非流式)
GET  /v1/models                    # 列出可用模型
GET  /v1/models/{model_id}         # 模型详情
```

### 7.2 管理 Endpoints

```
GET  /admin/accounts               # 列出所有账号及配额
GET  /admin/accounts/{email}/quota # 查询单个账号配额
POST /admin/accounts/rotate        # 手动触发账号轮转
GET  /admin/health                 # 健康检查（含配额概览）
GET  /admin/metrics                # Prometheus 指标
```

### 7.3 请求示例

```bash
# 标准调用 — 不需要 API key，本机访问
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-2.5-pro",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": true
  }'

# 如果 client 库要求 Authorization header 不为空，使用占位符即可：
# -H "Authorization: Bearer placeholder"

# 响应 (SSE)
data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","model":"gemini-2.5-pro","choices":[{"delta":{"content":"Hi"},"index":0}]}
data: [DONE]
```

### 7.4 认证

无认证。仅监听 127.0.0.1，本机自用。
如果调用方的 OpenAI client 库要求 `api_key` 不为空，填任意字符串即可（如 `"placeholder"`），服务端不校验。

### 7.5 CORS

MVP 阶段即加入宽泛 CORS 中间件，支持浏览器前端面板（NextChat Web、Dify 本地部署等）的预检请求：

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
```

仅监听 127.0.0.1，宽泛 CORS 无安全风险。

---

## 8. 项目结构

```
Chat2API/
├── chat2api/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app, uvicorn 入口
│   ├── config.py               # 配置加载 (env + yaml)
│   │
│   ├── routing/
│   │   ├── __init__.py
│   │   ├── completions.py      # /v1/chat/completions handler
│   │   ├── models.py           # /v1/models handler
│   │   └── admin.py            # /admin/* handlers
│   │
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── base.py             # Provider 抽象基类
│   │   ├── gemini.py           # Gemini CLI 协议实现
│   │   └── codex.py            # Codex 协议实现
│   │   # 未来扩展：antigravity.py, copilot.py, perplexity.py
│   │
│   ├── account/
│   │   ├── __init__.py
│   │   ├── gemini_account.py   # Gemini 账号管理 + OAuth 刷新 (借鉴 code-orchestra)
│   │   ├── codex_account.py    # Codex 账号管理 + token 刷新
│   │   ├── pool.py             # AccountPool — 按模型选最优账号
│   │   └── quota.py            # 配额查询 + 缓存 + 评分算法
│   │
│   ├── anti_detection/
│   │   ├── __init__.py
│   │   ├── tls_client.py       # TLS 指纹匹配的 HTTP client (curl_cffi)
│   │   └── rate_limiter.py     # 速率控制 + 单账号单并发锁
│   │
│   ├── models/
│   │   ├── __init__.py
│   │   ├── tiers.py            # 模型分档定义 + 降级链
│   │   └── openai_types.py     # OpenAI API 请求/响应 Pydantic 类型
│   │
│   └── protocol/
│       ├── __init__.py
│       ├── sse.py              # SSE 流式转换 (provider → OpenAI format)
│       └── converter.py        # Provider 响应 → OpenAI 响应转换
│
├── config.yaml                 # 运行时配置（模型排名、速率限制等）
├── requirements.txt
├── .env
└── .gitignore
```

---

## 9. 配置文件设计

### config.yaml

```yaml
server:
  host: "127.0.0.1"
  port: 8000

# 模型分档（有序列表，前面的优先级高）
# 用户可通过 CLI 交互式调整顺序
tiers:
  high:  # Tier 1 — 高级推理模型
    - {name: "gemini-3.1-pro",  provider: gemini, quota_group: gemini-pro, model_id: "gemini-3.1-pro-preview"}
    - {name: "gemini-2.5-pro",  provider: gemini, quota_group: gemini-pro, model_id: "gemini-2.5-pro-exp-03-25"}
    # Codex 高级模型待确认实际可用列表后补充

  mid:   # Tier 2 — 中级模型
    - {name: "gemini-2.5-flash", provider: gemini, quota_group: gemini-flash, model_id: "gemini-2.5-flash-preview-04-17"}
    - {name: "codex",            provider: codex,  quota_group: codex-primary, model_id: "codex-mini-latest"}

  low:   # Tier 3 — 轻量模型
    - {name: "gemini-2.0-flash",      provider: gemini, quota_group: gemini-flash-low, model_id: "gemini-2.0-flash"}
    - {name: "gemini-2.0-flash-lite", provider: gemini, quota_group: gemini-flash-low, model_id: "gemini-2.0-flash-lite"}

# 反检测配置
anti_detection:
  tls_impersonate: "chrome124"   # curl_cffi impersonate target
  max_rpm_per_account: 10        # 每账号每分钟最大请求数
  single_concurrency: true       # 每账号同时只处理一个请求

# 配额管理
quota:
  cache_ttl: 60                  # 配额缓存秒数
  safety_threshold: 40           # 低于此值触发切换
  check_interval: 300            # 配额检查间隔秒数
```

---

## 10. 核心流程

### 10.1 请求处理流程

```
1. 收到 POST /v1/chat/completions
   ↓
2. 解析请求，提取 model 名称
   ↓
3. TierMapper.resolve(model)
   → 查找降级链，对每个候选模型：
     → AccountPool.get_best(provider, model_id)
     → 检查配额缓存，找到有配额的账号
     → 返回 (actual_model, account, provider_backend)
   ↓
5. AntiDetection.acquire(account)
   → 获取账号锁（保证单并发）
   → 检查速率限制
   → 可选的请求延迟
   ↓
6. ProviderBackend.chat_completion(account, messages, params)
   → 构造 provider-specific 请求
   → 使用 TLS-impersonated HTTP client 发送
   → 处理流式/非流式响应
   ↓
7. Protocol.convert_to_openai(response)
   → 转换为 OpenAI 格式
   → 流式：逐块转换 + yield SSE events
   → 非流式：组装完整响应
   ↓
8. 返回响应
   → 附加 X-Chat2API-* headers（降级信息等）
   → 更新配额缓存
   → 429 错误时标记账号，重试下一个
```

### 10.2 429 / 配额耗尽时的自动重试

```python
async def chat_completion_with_retry(request):
    tried_accounts = set()

    while True:
        model_id, account, backend = resolve_model(
            request.model, exclude=tried_accounts
        )
        try:
            return await backend.generate(account, request)
        except QuotaExhaustedError:
            tried_accounts.add(account.email)
            # 标记此账号在此模型上的配额为 0
            quota_cache.mark_exhausted(account.email, model_id)
            continue  # 尝试下一个账号或降级模型
        except RateLimitError:
            tried_accounts.add(account.email)
            continue

    # 所有账号和降级模型都尝试过了
    raise HTTPException(503, "All accounts exhausted")
```

---

## 11. 依赖

```
fastapi>=0.110
uvicorn[standard]>=0.27
curl_cffi>=0.7           # TLS 指纹匹配
pyyaml>=6.0              # 配置文件
pydantic>=2.0            # 数据校验
```

不需要额外的 LLM SDK — 我们直接使用 HTTP 请求模拟 CLI 协议。

---

## 12. 关于封号风险的深入分析

### 12.1 "我模拟 CLI 行为，为什么还会被检测？"

根据对已有项目的研究（ZeroGravity, Antigravity Manager, CLIProxyAPI 等），答案是 **多层检测的叠加效应**：

**第 1 层：TLS 指纹（最容易暴露）**

即使你完美复制了 Gemini CLI 的 HTTP headers，Google 在 TCP/TLS 层就能看出差异。Python 的 OpenSSL 和 Node.js 的 BoringSSL 产生的 JA3 hash 完全不同。这就好比你穿了警察制服但说的不是警察的行话。

> ZeroGravity 项目为此专门使用了 BoringSSL（Chrome 的 TLS 库），这是它能长期存活的关键原因之一。

**第 2 层：行为模式（最难完全模拟）**

真实的 CLI 用户有非常明显的行为特征：
- 请求间隔 15-120 秒（人类需要阅读、思考、编辑）
- 每天 20-80 个请求（正常编程节奏）
- 有明显的活跃/不活跃周期（上班/下班）
- Session 有自然的开始和结束

而代理服务的特征是：
- 请求间隔均匀且短
- 24 小时持续请求
- 没有 warmup/shutdown 模式
- 请求大小分布异常均匀

**第 3 层：多账号关联（最致命）**

Google 2026 年 2 月的大规模封号主要是基于这个：
- 多个账号从同一 IP 发请求
- 多个账号的 token 在时间上交替使用（轮转特征明显）
- 多个账号的请求模式高度相似

### 12.2 我们的应对优先级

| 优先级 | 措施 | 效果 | 实现难度 |
|--------|------|------|----------|
| P0 | TLS 指纹匹配 (分级策略) | 消除最明显的机器特征 | 中 |
| P0 | 单账号单并发 (Busy-Skip) | 消除最明显的代理特征 | 低 |
| P1 | RPM 限速（不阻塞单请求） | 避免触发频率异常检测 | 低 |
| P1 | 会话粘性 (inertia) | 减少账号切换频率 | 低 |
| P2 | 每账号独立 IP | 避免多账号关联 | 高（需代理） |
| P3 | Session lifecycle | warmup/heartbeat/shutdown | 高 |

### 12.3 风险评估

**低风险用法（自用，1-3 个账号）：**
- 请求量小，与正常用户无异
- TLS 指纹匹配 + 速率限制即可
- 封号概率极低

**中风险用法（团队使用，5-10 个账号）：**
- 需要会话粘性 + 行为模拟
- 建议每账号独立 IP
- 偶尔可能触发临时限制

**高风险用法（公开服务，10+ 个账号）：**
- 即使所有措施到位，仍有被检测的风险
- Google/OpenAI 可以随时更新检测算法
- 不建议在本框架下做这种用途

---

## 13. 实现路线图

### Phase 1: 核心 MVP
- [ ] FastAPI 框架搭建 + OpenAI 兼容接口（127.0.0.1，无认证）
- [ ] Gemini CLI backend — 账号管理 + OAuth 刷新 + 生成请求
- [ ] Codex backend — 账号管理 + token 刷新 + responses API
- [ ] 基础 SSE 流式转换（provider 格式 → OpenAI 格式）
- [ ] curl_cffi TLS 指纹匹配（从一开始就内置）

### Phase 2: 智能路由 + 降级
- [ ] 多账号配额查询 + 缓存
- [ ] AccountPool — 按模型选最优账号
- [ ] 跨 Provider 分档降级
- [ ] 429 自动重试 + 账号切换
- [ ] 单账号单并发锁 + 速率限制

### Phase 3: CLI 工具
- [ ] `chat2api serve` — 启动服务
- [ ] `chat2api models list` — 从 provider 拉取可用模型
- [ ] `chat2api models rank --edit` — 交互式调整模型排名
- [ ] `chat2api accounts status` — 所有账号配额一览

### Phase 4: 未来扩展
- [ ] Antigravity backend（Claude & GPT via Google）
- [ ] GitHub Copilot backend
- [ ] Perplexity backend
- [ ] 管理 Web UI
