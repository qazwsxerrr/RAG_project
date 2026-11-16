# 企业级多模态 RAG 系统前后端启动与运行指南

本项目为企业级多模态 RAG 生产系统，架构由 **Java 鉴权中心（Spring Boot OIDC + 短期密钥租约）**、**Python 核心业务后端（FastAPI + LangGraph）** 与 **Vue 3 现代化前端** 协同组成。

---

## 一、系统架构与服务端口拓扑

本地采用 **WSL 原生服务 + Nginx 统一入口**，腾讯云采用 **Docker Compose + Nginx 容器**。

本地先确认 PostgreSQL 在线，再启动 Redis、Keycloak、Java 和 Python，构建前端后由 Nginx 托管。Nginx 负责请求路由，各服务进程仍需分别启动。

| 启动顺序 | 服务组件 | 端口 | 核心职责 | 运行环境 |
| :---: | :--- | :---: | :--- | :--- |
| **0** | **PostgreSQL 16 + pgvector** | 5432 | 向量知识库存储与 RBAC 业务表（远程实例） | 独立实例 |
| **0+** | **Redis 7+** | 6379 | Lua 原子滑动窗口限流、并发流护栏与 Streams 削峰队列 | WSL2 / 独立实例 |
| **1** | **Keycloak (Dev IdP)** | 8180 | 企业统一单点登录与 OIDC 授权 | WSL2 / Java 21 |
| **2** | **java-admin-service** | 8080 | 签发业务 JWT、提供 JWKS 动态验签与模型密钥短期租约 | WSL2 / Maven |
| **3** | **FastAPI 业务后端** | **8001** | 多模态入库流水线、LangGraph 复合检索与流式问答 | WSL2 / Python `.venv` |
| **4** | **Vue 3 前端构建** | — | 生成 `frontend/dist` 静态资源 | WSL2 / Node.js |
| **5** | **Nginx** | 80 | 托管前端，统一代理 Java、Python 和 Keycloak | WSL2 |

> 当前代理配置使用 Python 8001、Java 8080。本地 Keycloak 使用 8180；修改端口时需同步代理及认证配置。Vite 5173 用于可选的前端热更新开发。

---

## 二、环境依赖规范

| 依赖类别 | 要求版本 | 规范与说明 |
| :--- | :--- | :--- |
| **Python** | **3.12** | **强制使用项目独立虚拟环境 `.venv`**（运行于 WSL2），严禁向系统全局安装业务依赖 |
| **Node.js** | >= 18.0.0（推荐 20 LTS） | 前端构建与开发运行环境（支持 npm / pnpm） |
| **JDK** | **OpenJDK 21** | 运行 Keycloak 与 java-admin-service（`pom.xml` 指定 Java 21） |
| **Maven** | 3.8+ | 编译与启动 java-admin-service |
| **数据库** | PostgreSQL 16 | 需安装 `pgvector` 扩展；数据表与角色由程序启动时自动幂等迁移建表 |
| **缓存/队列** | **Redis 7+** | 高并发限流、单用户并发流护栏与异步削峰 Streams 队列（启动强校验） |

---

## 三、本地服务启动流程

以下命令统一在 WSL Ubuntu 中执行。Keycloak、Java、Python 分别占用一个终端。

### 运行前准备

- 配置根目录 `.env`、Java `.env.vault` 和 `keys/` 密钥文件，核对两端的数据库连接及内部服务凭据。
- 确认真实 PostgreSQL 16 + pgvector 可连接，配置 OSS、MinerU、模型、Embedding 与 Rerank 服务。
- 以下 Keycloak 命令要求已安装 `~/keycloak-26.0.0`。
- **本地 Nginx 配置尚待适配**。仓库 `nginx/` 当前使用 Docker 上游名称。启用本地入口前，需将 Java、Python、Keycloak 上游分别配置为 `127.0.0.1:8080`、`127.0.0.1:8001`、`127.0.0.1:8180`，静态目录配置为 `/mnt/d/ai_code/RAG_project/frontend/dist`，并核对 Keycloak 回调白名单、Java 登录及退出跳转地址与本地入口一致。

### 步骤 1 启动 Redis

```bash
sudo service redis-server start
redis-cli ping
```

预期输出 `PONG`。数据库使用已配置实例。

### 步骤 2 启动 Keycloak 8180

```bash
cd /mnt/d/ai_code/RAG_project
mkdir -p ~/keycloak-26.0.0/data/import
cp java-admin-service/keycloak/realm-enterprise-rag.json \
  ~/keycloak-26.0.0/data/import/

~/keycloak-26.0.0/bin/kc.sh start-dev --import-realm --http-port=8180
```

realm 首次启动时导入，已有 realm 的配置变更需另行管理。

### 步骤 3 启动 Java 8080

```bash
cd /mnt/d/ai_code/RAG_project/java-admin-service
~/.local/bin/mvn spring-boot:run
```

本地默认 OIDC 端点为 `localhost:8180`，数据库由 Flyway 执行迁移。

### 步骤 4 启动 Python 8001

首次初始化或依赖变更后同步依赖。

```bash
cd /mnt/d/ai_code/RAG_project
uv pip install --python .venv/bin/python -r backend/requirements.txt
```

启动后端，入库队列 worker 随应用启动。

```bash
cd /mnt/d/ai_code/RAG_project
.venv/bin/uvicorn rag_kb.api.main:app \
  --app-dir backend/src --host 127.0.0.1 --port 8001 --reload
```

### 步骤 5 构建前端

首次安装或锁文件变更后执行 `npm ci`，修改前端代码后执行 `npm run build`。

```bash
cd /mnt/d/ai_code/RAG_project/frontend
npm ci
npm run build
```

### 步骤 6 启动 Nginx

首次安装 Nginx。

```bash
sudo apt update
sudo apt install -y nginx
```

**完成上述本地网关配置并加载到 Nginx 后**，检查配置并启动。

```bash
sudo nginx -t && sudo service nginx start
```

修改配置后检查并重载。

```bash
sudo nginx -t && sudo service nginx reload
```

本地网关配置为监听 80 后，浏览器统一访问 `http://localhost`。首次安装时的默认欢迎页只表示 Nginx 已启动。

### 健康检查

```bash
curl -fsS http://localhost:8180/realms/enterprise-rag/.well-known/openid-configuration
curl -fsS http://localhost:8080/.well-known/jwks.json
curl -fsS http://localhost:8001/health
# 完成本地网关配置后执行
curl -fsS http://localhost/gateway/healthz
curl -fsS http://localhost/health
```

网关自身健康检查仅验证 Nginx，完整链路还需实际验证登录、问答和文档上传。

### 可选的前端热更新开发

Vite 继续负责前端构建，也支持独立热更新开发。需要即时预览前端改动时执行以下命令，并访问 `http://localhost:5173`。当前 Vite 已配置 Java、Python 与 OIDC 回调代理，认证配置需允许此入口。

```bash
cd /mnt/d/ai_code/RAG_project/frontend
npm run dev
```

### 腾讯云 Docker 部署

服务器部署目录为 `/var/www/RAG_project`，统一入口为 `https://rag.sakivo.com`。仓库 Compose 要求外部网络 `rag-net` 已存在，且数据库 `pg-rag:5432` 可在该网络访问；同时需要 `.env`、Java `.env.vault`、密钥及构建好的 `frontend/dist`。

前置条件满足后，在服务器执行以下命令。

```bash
cd /var/www/RAG_project
docker compose up -d --build
docker compose ps
docker compose logs -f rag-frontend rag-backend java-admin-service
```

当前仓库网关监听 80，HTTPS 需由外部 TLS 终结层提供。Java 的 Compose 启动依赖尚未包含 Keycloak 健康检查，启动后需检查认证链路。本节依据仓库配置编写，服务器实际运行状态需现场核验。

---

## 四、全链路自动化测试

自动化测试统一在 **WSL2 终端** 中通过 **`.venv` 环境** 运行：

```bash
# 执行完整测试套件（pyproject.toml 已配置 pythonpath）
.venv/bin/pytest backend/tests/ -v

# 或执行特定阶段测试（例如 Phase-7 容灾熔断测试）
.venv/bin/pytest backend/tests/test_step7_resilient_failover.py -v
```

跨语言 JWT 验签契约使用短有效期 fixture，运行前显式生成。

```bash
(cd java-admin-service && mvn -q -Dtest=JavaIssuedTokenFixtureGenerator test)
.venv/bin/pytest backend/tests/test_step8_auth_permissions.py -v
```

### 流式连接的凭据过期与吊销

- 凭据自然过期时，SSE 发送 `auth_expired`。流水线客户端重新申请短票，并携带 `last_event_id` 续传，换票请求按需刷新业务 JWT。
- 问答流过期后结束本次生成，前端保留已收到的内容，刷新凭据并提示重新发送问题。问答续传需要服务端保存生成进度，当前采用用户确认重发的方式。
- 账号停用、吊销或权限变更时，SSE 发送 `auth_revoked`，客户端结束连接并退出登录。同次检查同时命中过期和吊销时，优先处理吊销。

---

## 五、验收账号与权限矩阵

系统预置了三类典型权限边界账号，密码统一为：**`Rag@123456`**

| 账号 | 角色 | 部门范围 (`dept_path`) | 访问与操作预期权限 |
| :--- | :--- | :--- | :--- |
| `admin` | 超级管理员 (`*`) | `/`（全组织树） | 可查看并管理所有部门知识库及全局配置大盘 |
| `developer` | 研发工程师 | `/总公司/研发中心/技术部` | 仅可见技术部内部知识库及公共知识库（无法跨越至财务部） |
| `accountant` | 财务专员 | `/总公司/职能中心/财务部` | 仅可见财务部内部知识库（无上传权限点 `doc:upload`） |

---

## 六、工程核心目录速查

```text
.
├── .venv/                         # Python 3.12 本地专属虚拟环境（隔离全部后端依赖）
├── keys/                          # RS256 鉴权密钥对（rsa_private.pem 由 Java 持有）
├── java-admin-service/            # Spring Boot 鉴权中心（签发业务 JWT、模型密钥租约）
├── backend/                       # Python FastAPI 异步后端工程
│   └── src/
│       ├── rag_kb/                # 知识库管理、RBAC 保护端点、多模态入库图
│       │   └── api/main.py        # FastAPI 服务入口（8001）
│       └── rag_search/            # 混合检索流水线、RRF 融合重排与流式生成图
└── frontend/                      # Vue 3 + Vite 现代化前端工程
    ├── vite.config.ts             # 开发反代配置（/api/v1 -> 8001, /api/admin -> 8080）
    └── src/                       # 核心业务组件（问答看板、流转流图、人机审核台）
```
