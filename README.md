# 企业级多模态 RAG 系统前后端启动与运行指南

本文档详细说明本项目的前后端环境准备、本地开发需要启动的全部服务、数据库初始化、服务启动步骤以及联调测试方法。

> **Phase-6 起，本项目不再是「前端 + 后端」两进程架构**。业务请求必须携带 Java 鉴权中心签发的 RS256 业务 JWT，模型凭据必须向 Java 侧换取短期租约。因此本地联调需要额外启动两个服务：**Keycloak（企业 IdP，提供登录）** 与 **java-admin-service（签发业务 JWT + 模型密钥租约）**。

---

## 目录结构总览 (Project Directory Tree)

本项目遵循严谨的领域驱动与高内聚解耦架构，代码库仅保留生产业务核心源码：

```text
.
├── .env                           # 本地/生产环境变量（不入库）
├── .env.example                   # 环境变量配置模板（含 JWT / 鉴权中心 / 模型租约参数）
├── README.md                      # 项目快速启动与运行指南
├── pyproject.toml                 # 现代化 Python 构建配置（pytest pythonpath 亦在此）
├── requirements.txt               # 生产环境依赖清单 (严格排除臃肿 PyTorch 本地大包)
│
├── keys/                          # RS256 业务 JWT 密钥对（rsa_private.pem 仅 Java 侧持有）
├── java-admin-service/            # Phase-6 Java 鉴权中心 (Spring Boot + Spring Security OIDC + Flyway)
│   ├── keycloak/
│   │   └── realm-enterprise-rag.json  # Dev IdP 的 realm 导入定义（client/用户/组/dept_path 映射）
│   ├── src/main/resources/application.yml
│   ├── src/main/resources/db/migration/  # sys_users / sys_roles 建表与角色播种
│   └── .env.vault                 # 模型密钥保险箱（明文 API Key 只允许出现在这里，已 gitignore）
│
├── backend/                       # Python 异步高性能后端工程 (FastAPI + LangGraph)
│   ├── Dockerfile                 # 后端生产环境镜像构建文件（EXPOSE 8001）
│   ├── requirements.txt           # 生产环境依赖清单
│   ├── app/
│   │   └── main.py                # 容器与既有服务无缝适配层（uvicorn backend.app.main:app 入口）
│   └── src/
│       ├── rag_kb/                # 知识库管理、入库图编排与资产持久化
│       │   ├── api/               # 现代化 RESTful 业务路由（全量端点受 RBAC 保护）
│       │   ├── core/              # 配置驱动、数据库连接、OSS 客户端与模型租约客户端
│       │   ├── db/                # SQLAlchemy ORM 数据库实体层 (pgvector + BM25)
│       │   ├── graph/             # 基于 LangGraph 的 11 节点多模态入库图编排引擎
│       │   ├── nodes/             # 入库图原子计算节点 (MinerU解析/VLM/表格/分块/向量化)
│       │   └── services/          # 知识库服务支撑层
│       └── rag_search/            # 基于 LangGraph 的多阶段检索与生成图引擎
│           ├── answer.py          # 流式生成与角标溯源
│           ├── build.py           # 检索计算图构建器
│           ├── fusion.py          # RRF 倒数融合 (Dense+Sparse+BM25+HyDE)
│           ├── mmr.py             # MMR 最大边际相关性去重
│           ├── nodes.py           # 检索图原子计算节点
│           ├── rerank.py          # BGE-Reranker-Large 交叉编码深度精排
│           └── state.py           # 检索状态定义 (SearchState)
└── frontend/                      # 现代化 Vue 3 + TS + Vite 前端工程
    ├── package.json               # 前端工程与依赖描述 (集成 mermaid, markdown-it 等)
    ├── vite.config.ts             # Vite 构建与开发反代配置
    ├── tailwind.config.js         # Tailwind CSS 主题配置
    └── src/
        ├── App.vue                # 布局主框架 (常驻侧边栏与会话切换)
        ├── api/                   # 前后端通信客户端 (含 原生 Fetch SSE 长连接解析)
        ├── components/
        │   ├── documents/         # 11 节点流转看板、切片多模态资产详情(Mermaid/Table/Image)、人机审核工作台
        │   └── retrieval/         # 动态思维链、流式气泡渲染与 PDF 原文高亮溯源弹窗
        ├── stores/                # Pinia 响应式状态管理 (业务 JWT 仅存内存，不落 localStorage)
        ├── types/                 # TypeScript 强类型接口契约
        └── views/                 # 核心功能页面 (文档列表 / 提交入库 / 智能问答)
```

---

## 本地开发启动清单（先看这一节）

### 1. 一共要启动什么

| 顺序 | 服务 | 本地端口 | 作用 | 不启动的后果 |
| :---: | :--- | :---: | :--- | :--- |
| 0 | **PostgreSQL 16 + pgvector** | 5432（远程 `121.41.118.56`） | 业务库 + RBAC 表 | 前后端均无法启动 |
| 1 | **Keycloak（Dev IdP）** | 8180 | 提供企业单点登录 | 打不开登录页，拿不到业务 JWT |
| 2 | **java-admin-service** | 8080 | 签发业务 JWT、模型密钥租约、JWKS | 前端换票失败；Python 模型调用（LLM/向量/重排）全部失败 |
| 3 | **FastAPI 后端** | **8001** | 业务 API + 检索/入库图 | 前端页面无数据 |
| 4 | **Vite 前端** | 5173 | 开发服务器 + 反代 | 无界面 |

> ⚠️ **后端必须是 8001，不是 8000**。`vite.config.ts`（`/api/v1`）、`backend/Dockerfile`（`CMD --port 8001`）、`docker-compose.yml`（`PORT=8001`）与 `config.py`（`PORT` 默认 8001）全部以 **8001** 为准。用 8000 启动会导致前端所有请求 502/ECONNREFUSED。

### 2. 前置条件

| 依赖 | 版本 | 说明 |
| :--- | :--- | :--- |
| Python | 3.10 ~ 3.12 | 后端运行环境 |
| Node.js | >= 18（推荐 20 LTS） | 前端运行环境 |
| **JDK** | **21**（`pom.xml` 中 `java.version=21`） | 运行 java-admin-service 与 Keycloak |
| Maven | 3.8+ | 编译/启动 java-admin-service |
| Docker | 可选 | 有 Docker 时可用 `docker compose` 起 Keycloak / Java |

**Windows 用户注意**：本项目实测环境为「Windows + WSL2」。Windows 侧默认没有 JDK/Maven，WSL 内已有 `openjdk 21` + `maven 3.9`。下文命令中的 Java 与 Keycloak 部分默认在 **WSL 里执行**，Python/前端可在 Windows 侧执行（WSL2 会自动把 8080 / 8001 / 8180 转发到 `127.0.0.1`）。

### 3. 步骤 1：启动 Keycloak（Dev IdP）

```bash
# WSL 内执行，仅首次需要下载
cd ~
curl -LO https://github.com/keycloak/keycloak/releases/download/26.0.0/keycloak-26.0.0.tar.gz
tar xzf keycloak-26.0.0.tar.gz

# 把仓库里的 realm 定义放进导入目录（client / 3 个用户 / 组 / dept_path 映射器 全在其中）
# 注意：路径按你本机的仓库位置替换
mkdir -p ~/keycloak-26.0.0/data/import
cp /mnt/d/ai_code/RAG_project/java-admin-service/keycloak/realm-enterprise-rag.json \
   ~/keycloak-26.0.0/data/import/

# 启动（--import-realm 只在 realm 不存在时导入一次）
~/keycloak-26.0.0/bin/kc.sh start-dev --import-realm --http-port=8180
```

验证：

```bash
curl -s http://localhost:8180/realms/enterprise-rag/.well-known/openid-configuration | head -c 120
```

有 Docker 的环境也可以直接：

```bash
docker network create rag-net      # 仅首次
docker compose up -d keycloak
```

### 4. 步骤 2：启动 java-admin-service

```bash
cd /mnt/d/ai_code/RAG_project/java-admin-service

# 关键：Java 跑在宿主机/WSL 而非容器内，必须把 3 个服务端端点从容器名改回 localhost
export OIDC_TOKEN_URI=http://localhost:8180/realms/enterprise-rag/protocol/openid-connect/token
export OIDC_USERINFO_URI=http://localhost:8180/realms/enterprise-rag/protocol/openid-connect/userinfo
export OIDC_JWK_SET_URI=http://localhost:8180/realms/enterprise-rag/protocol/openid-connect/certs

mvn -q spring-boot:run
```

说明：

- `authorization-uri` 默认已是 `http://localhost:8180/...`，无需覆盖；只有在 Docker 内运行时才使用 `keycloak:8080`。
- 请用 `mvn spring-boot:run`，**不要**用 `java -jar target/*.jar` —— 后者跑的是旧 jar 里打包的 `application.yml`。
- 数据库连接、RSA 私钥路径（`../keys/rsa_private.pem`）、模型密钥（`java-admin-service/.env.vault`）都已在 `application.yml` 里配好，无需额外传参。
- 首次启动会由 **Flyway** 自动建 `sys_users` / `sys_roles` 并播种 4 个系统角色。

验证：

```bash
curl -s http://localhost:8080/.well-known/jwks.json | head -c 120
```

### 5. 步骤 3：启动 FastAPI 后端（8001）

```bash
# 在项目根目录下执行
uvicorn backend.app.main:app --host 0.0.0.0 --port 8001 --reload
```

> `backend.app.main:app` 是兼容层入口，会自动把 `backend/src` 加入 `sys.path`，从仓库根目录直接运行即可。

验证：

- 健康检查：<http://localhost:8001/health>
- Swagger：<http://localhost:8001/docs>
- Redoc：<http://localhost:8001/redoc>

启动时 `verify_jwt_setup_on_startup()` 会强校验验签公钥（或远程 JWKS）有效性；**缺失/格式非法会直接抛 `RuntimeError` 拒绝启动**（不会静默降级），因此启动成功即代表校验通过。

### 6. 步骤 4：启动前端（5173）

```bash
cd frontend
npm run dev
```

打开 <http://localhost:5173>，应自动跳转到 Keycloak 登录页。

### 7. 启动顺序与登录验收

启动顺序：**PostgreSQL → Keycloak(8180) → Java(8080) → 后端(8001) → 前端(5173)**。

登录后可用以下三个内置账号验证「不同员工看到不同知识库内容」：

| 账号 | 密码 | 角色 | `dept_path` | 预期可见范围 |
| :--- | :--- | :--- | :--- | :--- |
| `admin` | `Rag@123456` | `admin`（`*`） | `/` | 全部文档 |
| `developer` | `Rag@123456` | `developer` | `/总公司/研发中心/技术部` | 本部门树 + `/public` |
| `accountant` | `Rag@123456` | `accountant` | `/总公司/职能中心/财务部` | 本部门树 + `/public` |

验收要点：

1. 用 `developer` 登录 → `GET /api/v1/auth/me` 返回的 `dept_path` 必须是技术部。
2. 用 `developer` 检索财务部文档 → 检索不到；主动在请求体传 `department_scope: ["/总公司/职能中心/财务部"]` → **403**。
3. 用 `accountant` 上传文档 → **403**（缺 `doc:upload` 权限点）。

### 8. 本地常见坑

1. **后端端口写成 8000**：前端全部 502。必须 8001。
2. **Java 启动会对 `.env` 里配置的数据库执行 DDL**：`SPRING_DATASOURCE_URL` 默认指向 `121.41.118.56:5432/rag_enterprise`（与 Python 同一个库）。不想动这个库就自己起一个本地 PostgreSQL 并覆盖该环境变量。
3. **Keycloak 起来了但登录报 `invalid_redirect_uri`**：说明浏览器访问前端的地址与 realm 中登记的 redirect URI 不符。`realm-enterprise-rag.json` 已登记 `http://localhost:5173` / `http://localhost:8080` 两种情况；换端口或换域名需同步登记。
4. **`/api/admin/*` 走 8080、`/api/v1/*` 走 8001**：两个端口都不能少，`vite.config.ts` 已分别配置代理。
5. **模型调用全部报 401/失败**：Java 服务没起或 `.env.vault` 缺失。Python 端已不再读取任何本地明文模型 Key，必须向 Java 换短期租约。

---

## 一、环境与前置依赖要求

| 组件类别        | 推荐规格 / 版本                                                      | 说明                |
| :----------- | :-------------------------------------------------------------- | :----------------- |
| **操作系统**    | Linux (Ubuntu 22.04+) / macOS / Windows WSL2                   | 生产与开发环境           |
| **Python**  | Python 3.10 ~ 3.12 (推荐 3.12)                                   | 后端运行环境            |
| **Node.js** | Node.js &gt;= 18.0.0 (推荐 20.x LTS) + npm / pnpm                | 前端运行环境            |
| **JDK**     | OpenJDK 21                                                       | Java 鉴权中心与 Keycloak |
| **数据库**     | PostgreSQL 16 + **pgvector** 扩展                                | 严格单真实实例，禁止 SQLite |
| **身份认证**   | Keycloak 26（开发）/ 企业统一身份认证（生产）                     | OIDC + PKCE(S256) 单点登录 |
| **对象存储**    | 阿里云 OSS (私有/公共读 Bucket)                                        | 用于原件、版面切图与多模态资产归档 |
| **模型接口**    | MinerU API + SiliconFlow (BAAI/bge-m3, bge-reranker) + LLM/VLM | 多模态解析与向量计算；密钥由 `.env.vault` 托管 |

---

## 二、数据库与数据表初始化

> **不需要手动执行建表命令。**
> - Python 侧：后端启动时（`main.py` 的 lifespan）自动执行幂等的 `init_db()`，创建 `vector` 扩展、业务表、HNSW/GIN 索引并播种部门树。
> - Java 侧：启动时由 Flyway 自动建 `sys_users` / `sys_roles` 并播种系统角色。

请仅确认数据库可达、且已安装 `pgvector`：

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

---

## 三、后端服务启动 (Backend)

### 1. 安装后端 Python 依赖

```bash
# 建议在虚拟环境中安装
pip install -r backend/requirements.txt
```

### 2. 启动 FastAPI 后端服务

#### 方式 A：Uvicorn 命令行热重载启动（推荐开发使用）

```bash
uvicorn backend.app.main:app --host 0.0.0.0 --port 8001 --reload
```

#### 方式 B：Python 模块方式启动（读取 `.env` 中的 `HOST` / `PORT` / `DEBUG`）

```bash
PYTHONPATH=backend/src python -m rag_kb.api.main
```

### 3. 验证后端服务状态

- **后端健康检查**：浏览器访问 [http://localhost:8001/health](http://localhost:8001/health)（返回 `{"status": "ok", ...}`）
- **Swagger 交互式文档**：访问 [http://localhost:8001/docs](http://localhost:8001/docs)
- **Redoc 接口文档**：访问 [http://localhost:8001/redoc](http://localhost:8001/redoc)

---

## 四、前端服务启动 (Frontend)

前端采用 Vue 3 + Vite + Tailwind CSS + Pinia 构建，开发服务器内置代理规则：

| 代理前缀 | 目标 | 用途 |
| :--- | :--- | :--- |
| `/api/v1` | `http://localhost:8001` | Python 业务 API |
| `/api/admin` | `http://localhost:8080` | Java 鉴权中心（换票 / 用户 / JWKS） |
| `/oauth2`、`/login/oauth2` | `http://localhost:8080` | OIDC 授权与回调（保持同源，`changeOrigin: false`） |

### 1. 进入前端目录并安装依赖

```bash
cd frontend
npm install
```

### 2. 启动前端 Vite 开发服务器

```bash
npm run dev
```

启动完成后终端输出示例如下：

```text
  VITE v5.4.21  ready in 450 ms

  ➜  Local:   http://localhost:5173/
  ➜  Network: http://0.0.0.0:5173/
  ➜  press h + enter to show help
```

### 3. 访问前端界面

在浏览器中打开：[http://localhost:5173](http://localhost:5173)（未登录会自动跳转 Keycloak）

### 4. 前端打包与构建预览（可选）

```bash
# 类型检查与打包
npm run build

# 本地静态预览生产构建产物
npm run preview
```

---

## 五、全链路快速测试与验证

为验证前后端、数据库与鉴权链路，可在项目根目录运行自动化测试套件（`pyproject.toml` 已配置 `pythonpath`，无需额外设置 `PYTHONPATH`）：

```bash
pytest backend/tests/ -v
```

其中跨语言 JWT 契约用例依赖 Java 侧生成的夹具，需先执行一次：

```bash
cd java-admin-service && mvn -q test   # 生成 backend/tests/fixtures/java_issued_tokens.json
```

---

## 六、生产部署差异（简述）

| 项 | 本地开发 | 生产环境 |
| :--- | :--- | :--- |
| 身份认证 | Keycloak（`localhost:8180`） | 企业统一 IdP（`OIDC_AUTH_URI` 等变量覆盖） |
| 服务寻址 | 全部 `localhost` | 容器名互访（`keycloak:8080`、`java-admin-service:8080`、`rag-backend:8001`） |
| 反代 | Vite dev proxy | Nginx（`nginx/default.conf`），Cloudflare Tunnel 映射 |
| 端口 | 8080 / 8001 / 8180 / 5173 | 仅 Nginx 暴露 80/443，其余绑定 `127.0.0.1` |

> 反向代理场景下务必保证 `X-Forwarded-Proto` 与浏览器实际协议一致，否则 OIDC 的 `redirect_uri` 会退化成 `http://`，导致 `invalid_redirect_uri`。

---

## 七、常见问题排查 (FAQ)

1. **前端上传或问答接口 500 / 报错连接失败**
   - 检查后端 **8001** 端口是否已启动，访问 `http://localhost:8001/health` 查看是否正常。
   - 检查 `frontend/vite.config.ts` 中的代理配置是否指向当前后端的正确端口（`/api/v1` → 8001，`/api/admin` → 8080）。
2. **页面卡在登录页 / 换票失败**
   - 确认 Keycloak 已在 8180 启动且 realm `enterprise-rag` 已导入；
   - 确认 java-admin-service 已在 8080 启动，`http://localhost:8080/.well-known/jwks.json` 可访问。
3. **所有接口返回 401**
   - 业务 JWT 只存在 Pinia 内存中（不落 localStorage），刷新页面会静默重新换票；若换票失败会跳转 SSO。检查 Java 侧会话 Cookie 是否正常下发。
4. **模型调用报错 `【模型凭据托管失败】无法从 Java 鉴权中心获取 [...] 凭据`**
   - Java 服务未启动，或 `java-admin-service/.env.vault` 中缺少对应厂商 `*_API_KEY`。Python 端已禁止读取本地明文模型 Key，无降级路径。
5. **数据库报错 `type "vector" does not exist`**
   - PostgreSQL 实例未安装 `pgvector` 插件。请先在数据库执行 `CREATE EXTENSION vector;`。
6. **流水线执行中断如何恢复**
   - 本系统内置高可用断点续跑引擎与 Tenacity 3 次指数退避微重试机制。
   - 若遇到网络瞬断失败，在前端流转看板中直接点击【**重试当前步骤 (Step X)**】按钮即可从最近断点（如 Step 9 切片）秒级复原续跑，无需重复消耗 MinerU 与 VLM Token。
