# 企业级多模态 RAG 系统前后端启动与运行指南

本项目为企业级多模态 RAG 生产系统，架构由 **Java 鉴权中心（Spring Boot OIDC + 短期密钥租约）**、**Python 核心业务后端（FastAPI + LangGraph）** 与 **Vue 3 现代化前端** 协同组成。

---

## 一、系统架构与服务端口拓扑

本地联调共需运行 6 个组件，服务启动顺序必须严格为：**PostgreSQL → Redis → Keycloak → Java → Python 后端 → 前端**。

| 启动顺序 | 服务组件 | 端口 | 核心职责 | 运行环境 |
| :---: | :--- | :---: | :--- | :--- |
| **0** | **PostgreSQL 16 + pgvector** | 5432 | 向量知识库存储与 RBAC 业务表（远程实例） | 独立实例 |
| **0+** | **Redis 7+** | 6379 | Lua 原子滑动窗口限流、并发流护栏与 Streams 削峰队列 | WSL2 / 独立实例 |
| **1** | **Keycloak (Dev IdP)** | 8180 | 企业统一单点登录与 OIDC 授权 | WSL2 / Java 21 |
| **2** | **java-admin-service** | 8080 | 签发业务 JWT、提供 JWKS 动态验签与模型密钥短期租约 | WSL2 / Maven |
| **3** | **FastAPI 业务后端** | **8001** | 多模态入库流水线、LangGraph 复合检索与动态容灾热切换 | WSL2 / Python `.venv` |
| **4** | **Vue 3 前端工程** | 5173 | 现代化工作台交互界面，内置 Vite API 反代规则 | Node.js |

> ⚠️ **端口硬性约束**：Python 后端端口固定为 **8001**（前端 `/api/v1` 代理、Docker 容器与配置默认均绑定 8001）。

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

### 步骤 0：确认 PostgreSQL 16 与 Redis 7+ 在线

在 WSL2 终端中启动并验证本地 Redis 服务（Python 后端启动时执行真实 `PING` 强校验）：

```bash
# 启动 WSL2 Redis 服务并验证连通性
sudo service redis-server start
redis-cli ping   # 预期输出 PONG
```

---

### 步骤 1：启动 Keycloak 8180（开发 IdP）

在 WSL2 终端中执行（首次解压并导入 realm 定义）：

```bash
# 1. 复制仓库内置 realm 定义到 Keycloak 导入目录
mkdir -p ~/keycloak-26.0.0/data/import
cp java-admin-service/keycloak/realm-enterprise-rag.json ~/keycloak-26.0.0/data/import/

# 2. 启动服务（仅首次导入）
~/keycloak-26.0.0/bin/kc.sh start-dev --import-realm --http-port=8180
```

* 验证：`curl -s http://localhost:8180/realms/enterprise-rag/.well-known/openid-configuration | head -c 120`

---

### 步骤 2：启动 java-admin-service 8080（业务鉴权中心）

在 WSL2 终端中执行：

```bash
cd java-admin-service

# 显式对齐本地 Keycloak 端点
export OIDC_TOKEN_URI=http://localhost:8180/realms/enterprise-rag/protocol/openid-connect/token
export OIDC_USERINFO_URI=http://localhost:8180/realms/enterprise-rag/protocol/openid-connect/userinfo
export OIDC_JWK_SET_URI=http://localhost:8180/realms/enterprise-rag/protocol/openid-connect/certs

mvn -q spring-boot:run
```

* 验证：`curl -s http://localhost:8080/.well-known/jwks.json | head -c 120`
* 数据库迁移：启动时由 Flyway 自动执行建表与角色播种；私钥位于 `keys/rsa_private.pem`。

---

### 步骤 3：启动 FastAPI 后端 8001（基于 .venv 虚拟环境）

在 WSL2 终端、项目根目录下执行（**必须使用项目本地虚拟环境 `.venv`**）：

```bash
# 依赖初始化/同步（推荐使用 uv 秒级安装，或 source .venv/bin/activate && pip install）
uv pip install --python .venv -r backend/requirements.txt

# 启动后端（热重载模式）
.venv/bin/uvicorn backend.app.main:app --host 0.0.0.0 --port 8001 --reload
```

* 后端健康检查：[http://localhost:8001/health](http://localhost:8001/health)
* 交互式文档 Swagger：[http://localhost:8001/docs](http://localhost:8001/docs)

---

### 步骤 4：启动前端 5173（Vue 3 + Vite）

在项目根目录下执行（WSL 或 Windows 终端均可）：

```bash
cd frontend
npm install
npm run dev
```

* 访问前端：[http://localhost:5173](http://localhost:5173)（未登录将自动无缝重定向至 Keycloak 登录页）。

---

## 四、全链路自动化测试

自动化测试统一在 **WSL2 终端** 中通过 **`.venv` 环境** 运行：

```bash
# 执行完整测试套件（pyproject.toml 已配置 pythonpath）
.venv/bin/pytest backend/tests/ -v

# 或执行特定阶段测试（例如 Phase-7 容灾熔断测试）
.venv/bin/pytest backend/tests/test_step7_resilient_failover.py -v
```

> 注：若测试涉及跨语言 JWT 验签契约，需先在 `java-admin-service` 目录执行 `mvn -q test` 生成测试夹具。

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
│   ├── app/main.py                # 统一服务启动兼容入口（8001）
│   └── src/
│       ├── rag_kb/                # 知识库管理、RBAC 保护端点、多模态入库图
│       └── rag_search/            # 混合检索流水线、RRF 融合重排与流式生成图
└── frontend/                      # Vue 3 + Vite 现代化前端工程
    ├── vite.config.ts             # 开发反代配置（/api/v1 -> 8001, /api/admin -> 8080）
    └── src/                       # 核心业务组件（问答看板、流转流图、人机审核台）
```
