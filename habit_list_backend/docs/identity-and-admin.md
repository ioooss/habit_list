# 身份、会话与管理员安全基线

> 状态：P1 基线已实现。用户端使用 Sign in with Apple，管理员端使用独立账号、密码与 TOTP；两套身份、路由和令牌不可互换。

## 不变量

- 用户 API 位于 `/api/v1`，管理员 API 位于 `/admin/v1`。
- 用户访问令牌以 `it_at_` 开头，管理员令牌以 `it_admin_at_` 开头；中间件按路由平面分别验证。
- 数据库不保存访问令牌、刷新令牌、密码、TOTP 明文或原始 IP/User-Agent。
- 管理员角色不授予读取用户聊天、记忆正文或原始证据的权限。
- 生产环境必须使用 `AUTH_MODE=sessions`；固定 Bearer token 只保留给本地旧原型。

## 用户登录流程

1. 客户端调用 `POST /api/v1/auth/challenges` 获取一次性 `challenge_id` 和高熵 `nonce`。
2. 原生客户端将 nonce 的 SHA-256 值交给 Apple 授权请求，并保留原始 nonce。
3. 客户端把 Apple `identity_token`、原始 nonce、challenge 和设备安装标识提交给 `POST /api/v1/auth/apple`。
4. 后端固定使用 Apple issuer、JWKS 与 `RS256`，验证签名、`iss/aud/exp/iat/sub/nonce`；不信任客户端传入的邮箱或用户 ID。
5. 首次登录创建 `users` 与 `user_identities`；同一 Apple subject 在 PostgreSQL 中通过事务级 advisory lock 串行创建。
6. 返回短期访问令牌与可轮换刷新令牌。邮箱仅在 Apple 标记已验证时保存，并使用 Fernet 加密、HMAC 建索引。

访问令牌默认 15 分钟。刷新令牌默认 30 天、每次使用立即轮换；旧刷新令牌再次出现会视为重放并撤销整个 token family。用户可列出设备会话、撤销指定会话或全部退出。

| Method | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/api/v1/auth/challenges` | 公开 | 创建一次性 Apple nonce |
| POST | `/api/v1/auth/apple` | 公开 | 交换 Apple identity token |
| POST | `/api/v1/auth/refresh` | 公开 | 轮换 refresh/access token |
| GET | `/api/v1/auth/me` | 用户 | 当前用户与会话 |
| GET | `/api/v1/auth/sessions` | 用户 | 活跃设备会话 |
| DELETE | `/api/v1/auth/sessions/{id}` | 用户 | 撤销本人指定会话 |
| POST | `/api/v1/auth/logout` | 用户 | 退出当前会话 |
| POST | `/api/v1/auth/logout-all` | 用户 | 撤销本人全部会话 |

## 管理员身份与 RBAC

管理员账号不能通过公开 HTTP 接口自助注册。首次管理员只能在受控服务器终端交互创建，密码不会进入 argv 或日志：

```bash
python -m app.admin bootstrap \
  --username primary.admin \
  --display-name "Primary Admin" \
  --role super_admin
```

命令只在创建成功时显示一次 TOTP secret 与 provisioning URI。密码使用 Argon2id，TOTP secret 使用独立 Fernet key 加密；验证码允许前后一个时间窗口，但同一时间步不能重放。连续失败达到阈值后临时锁定账号，新的成功登录会撤销该管理员此前的活动会话。

| 角色 | 主要边界 |
|---|---|
| `super_admin` | 管理管理员、配置发布与审计 |
| `product_operator` | Prompt/模型/开关/任务，不读用户正文 |
| `safety_reviewer` | 匿名化安全案例与策略 |
| `support` | 账号状态、隐私工单状态，不读用户正文 |
| `analyst` | 仅聚合指标 |

当前管理员 API：

- `POST /admin/v1/auth/login`：密码 + TOTP。
- `GET /admin/v1/auth/me`：当前角色与权限。
- `POST /admin/v1/auth/logout`：撤销当前管理员会话。
- `GET /admin/v1/audit-events`：需要 `audit.read`，游标分页，不返回原始 IP/User-Agent。

系统角色与权限由代码版本控制并在启动时幂等写入；创建管理员时只能引用已存在的角色。

## 生产秘密与轮换

- `AUTH_TOKEN_PEPPER`：至少 32 个高熵字符；轮换会立即使现有用户和管理员会话失效。
- `PII_ENCRYPTION_KEY`：Fernet key；当前不支持无停机自动重加密，轮换前必须先实现双 key 解密迁移。
- `ADMIN_MFA_ENCRYPTION_KEY`：必须与 PII key 不同；轮换同样需要先迁移管理员 TOTP ciphertext。
- `APPLE_CLIENT_IDS`：填写真实 iOS Bundle ID/Services ID，多个 audience 用逗号分隔。

两个 Fernet key 可在受控终端分别生成：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

秘密只写入服务器权限为 `0600` 的 `.env.production`，不得进入客户端、日志、截图或 Git。

## 已知后续门槛

- 在边缘层加入按 IP、设备和账号维度的 challenge/login/refresh 限流。
- 增加管理员恢复码或 WebAuthn、管理员生命周期管理与高风险操作二次确认。
- 完成 Apple 凭据生产联调、账号注销、数据导出与全链路删除验证。
- 为 Fernet key 建立版本字段、双 key 读取和可恢复轮换任务。
- 对异常登录、refresh 重放、管理员锁定和审计写入失败接入告警。
