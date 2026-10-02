# 企业级多模态 RAG 系统前后端启动与运行指南

本文档详细说明本项目的前后端环境准备、数据库初始化、服务启动步骤以及联调测试方法。

---

## 目录结构总览 (Project Directory Tree)

本项目遵循严谨的领域驱动与高内聚解耦架构，代码库仅保留生产业务核心源码：

```text
.
├── .env.example                   # 生产环境变量配置模板 (通用 API_KEY / BASE_URL 规范)
├── README.md                      # 项目快速启动与运行指南
│
├── backend/                       # Python 异步高性能后端工程 (FastAPI + Pydantic v2)
│   ├── Dockerfile                 # 后端生产环境镜像构建文件
│   ├── requirements.txt           # 生产环境依赖清单 (严格排除臃肿 PyTorch 本地大包)
│   └── app/
│       ├── main.py                # FastAPI 应用程序入口与 CORS 中间件
│       ├── api/v1/                # RESTful 业务路由接口层
│       │   ├── documents.py       # 文档上传接入、列表查询与组织架构权限管理
│       │   ├── pipeline.py        # 11 节点流水线生命周期控制与断点重试触发
│       │   └── retrieval.py       # 8 阶段漏斗检索调度与 SSE 多路复用流式问答总线
│       ├── core/                  # 基础设施配置与底层驱动
│       │   ├── config.py          # 全局配置驱动管理与模型统一映射
│       │   ├── database.py        # PostgreSQL 16 + pgvector 异步连接池
│       │   ├── init_db.py         # 向量扩展、数据表结构及初始部门树自动化初始化脚本
│       │   └── oss.py             # 阿里云 OSS 真实对象存储客户端
│       ├── models/                # SQLAlchemy ORM 数据库实体层
│       │   ├── chunk.py           # 切片实体 (承载三路混合向量特征、BBox坐标与页码)
│       │   └── document.py        # 文档主表实体 (权限隔离树、哈希防重与 11 节点执行快照)
│       ├── schemas/               # Pydantic v2 强类型请求与响应契约
│       │   ├── document.py        # 文档元数据模型
│       │   ├── pipeline.py        # 流水线阶段状态模型
│       │   └── retrieval.py       # 问答、思考看板 (ThinkingProcess) 与溯源卡片模型
│       └── services/              # 核心业务领域服务层
│           ├── mcp/               # FastMCP 跨平台标准化智能体协议适配
│           │   ├── server.py      # 标准 MCP Server 实例与启动入口
│           │   └── tools_rag.py   # RAG 检索能力工具化封装
│           ├── parser/            # MinerU 高精版面分析与多模态提炼
│           │   ├── code_flowchart_service.py # 代码块与 Mermaid 流程图状态机保护
│           │   ├── mineru_adapter.py         # MinerU 云端 API 适配器与 OCR 版面分析
│           │   ├── table_service.py          # 复杂表格抽取、格式清洗与跨页拼接
│           │   └── vlm_service.py            # VLM 多模态大模型配图语义理解
│           ├── pipeline/          # 11 节点入库流水线编排引擎
│           │   ├── chunker.py     # Markdown 两阶段切分与 Breadcrumb 标题链继承
│           │   ├── dedup_service.py          # 标题语义向量防重校验
│           │   ├── state_machine.py          # 流水线状态机、日志事件广播与断点快照
│           │   ├── steps.py       # 11 节点具体执行逻辑
│           │   └── update_service.py         # 先增后删版本平滑演进服务
│           └── retrieval/         # 在线 8 阶段多漏斗检索引擎
│               ├── embedding_service.py      # BGE-M3 1024 维稠密特征提取
│               ├── hyde_service.py           # HyDE 假想技术解答生成与熔断器
│               ├── mmr_filter.py             # MMR 最大边际相关性贪心去重 (lambda=0.7)
│               ├── multi_retriever.py        # 稠密(HNSW)+倒排(TSVector)+稀疏内积三路并行召回
│               ├── preprocessor.py           # 多轮历史指代消除改写与特征生成
│               ├── rerank_service.py         # BGE-Reranker-v2 交叉注意力深度精排
│               ├── rrf_fusion.py             # 场景化加权倒数排名动态融合
│               └── vector_utils.py           # 稀疏向量归一化处理工具
│
└── frontend/                      # Vue 3 现代化工控交互前端工程 (Vite + Tailwind CSS + Pinia)
    ├── package.json               # 前端工程与依赖描述
    ├── vite.config.ts             # Vite 构建与开发反代配置
    ├── tailwind.config.js         # Tailwind CSS 主题配置
    └── src/
        ├── App.vue                # 布局主框架 (常驻侧边栏与会话切换)
        ├── api/                   # 前后端通信客户端 (含 原生 Fetch SSE 长连接解析)
        ├── components/
        │   ├── documents/         # 11 节点流转看板、切片树弹窗与人机审核工作台
        │   └── retrieval/         # 思考流看板、流式气泡渲染与 PDF 原文高亮溯源弹窗
        ├── stores/                # Pinia 响应式状态管理 (文档/会话/生成状态机)
        ├── types/                 # TypeScript 强类型接口契约
        └── views/                 # 核心功能页面 (文档列表 / 提交入库 / 智能问答)
```

---

## 一、环境与前置依赖要求


| 组件类别        | 推荐规格 / 版本                                                      | 说明                |
| :----------- | :-------------------------------------------------------------- | :----------------- |
| **操作系统**    | Linux (Ubuntu 22.04+) / macOS / Windows WSL2                   | 生产与开发环境           |
| **Python**  | Python 3.10 ~ 3.12 (推荐 3.12)                                   | 后端运行环境            |
| **Node.js** | Node.js &gt;= 18.0.0 (推荐 20.x LTS) + npm / pnpm                | 前端运行环境            |
| **数据库**     | PostgreSQL 16 + **pgvector** 扩展                                | 严格单真实实例，禁止 SQLite |
| **对象存储**    | 阿里云 OSS (私有/公共读 Bucket)                                        | 用于原件、版面切图与多模态资产归档 |
| **模型接口**    | MinerU API + SiliconFlow (BAAI/bge-m3, bge-reranker) + LLM/VLM | 多模态解析与向量计算        |


---

## 二、数据库与数据表初始化

在首次启动系统前，需确保 PostgreSQL 16 数据库已就绪并初始化向量扩展与表结构：

```bash
# 在项目根目录下执行（自动创建 vector 扩展、各业务表、HNSW 索引及初始部门树）
python3 -m backend.app.core.init_db
```

执行成功后，终端将输出：

```text
[DB] 已成功初始化默认企业组织架构部门树
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
uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --reload
```

#### 方式 B：Python 模块方式启动

```bash
python3 backend/app/main.py
```

### 3. 验证后端服务状态

- **后端健康检查**：浏览器访问 [http://localhost:8000/health](http://localhost:8000/health)（返回 `{"status": "ok", ...}`）
- **Swagger 交互式文档**：访问 [http://localhost:8000/docs](http://localhost:8000/docs)
- **Redoc 接口文档**：访问 [http://localhost:8000/redoc](http://localhost:8000/redoc)

---

## 四、前端服务启动 (Frontend)

前端采用 Vue 3 + Vite + Tailwind CSS + Pinia 构建，开发服务器内置代理规则自动转发 `/api` 与 `/static` 到后端 8000 端口。

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

在浏览器中打开：[http://localhost:5173](http://localhost:5173)

### 4. 前端打包与构建预览（可选）

```bash
# 类型检查与打包
npm run build

# 本地静态预览生产构建产物
npm run preview
```

---

## 五、全链路快速测试与验证

为确保前后端、数据库与大模型服务连接正常，可在项目根目录下运行全量自动化测试套件：

```bash
# 运行全量 41 项单元与端到端集成测试（覆盖解析、切片、微重试、断点续跑与检索问答）
PYTHONPATH=. pytest backend/tests/ -v
```

若全部通过，将呈现：

```text
41 passed, 2 warnings in ...
```

---

## 七、常见问题排查 (FAQ)

1. **前端上传或问答接口 500 / 报错连接失败**
  - 检查后端 8000 端口是否已启动，访问 `http://localhost:8000/health` 查看是否正常。
  - 检查 `frontend/vite.config.ts` 中的代理配置是否指向当前后端的正确端口。
2. **数据库报错 `type "vector" does not exist`**
  - PostgreSQL 实例未安装 `pgvector` 插件。请先在数据库执行 `CREATE EXTENSION vector;`。
3. **流水线执行中断如何恢复**
  - 本系统内置高可用断点续跑引擎与 Tenacity 3 次指数退避微重试机制。
  - 若遇到网络瞬断失败，在前端流转看板中直接点击【**重试当前步骤 (Step X)**】按钮即可从最近断点（如 Step 9 切片）秒级复原续跑，无需重复消耗 MinerU 与 VLM Token。

