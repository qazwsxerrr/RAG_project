#!/usr/bin/env bash
# ==============================================================================
# 企业级多模态 RAG (LangGraph 架构版) 一键启动脚本
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="${SCRIPT_DIR}/backend/src:${PYTHONPATH}"
export NO_PROXY="127.0.0.1,localhost,aliyuncs.com,siliconflow.cn,kedaya.ai,${NO_PROXY}"
export no_proxy="${NO_PROXY}"

echo "=========================================================="
echo " 🚀 正在启动 企业级多模态 RAG 系统 (LangGraph 2.0+)"
echo " 工作目录: ${SCRIPT_DIR}"
echo "=========================================================="

MODE="${1:-all}"

start_backend() {
    echo ">> [1/2] 正在启动后端 FastAPI (LangGraph 引擎, 端口 8000)..."
    cd "${SCRIPT_DIR}"
    python3 -m uvicorn rag_kb.api.main:app --host 0.0.0.0 --port 8000 --reload
}

start_frontend() {
    echo ">> [2/2] 正在启动前端 Vite 开发服务器 (端口 5173)..."
    cd "${SCRIPT_DIR}/frontend"
    npm run dev -- --host 0.0.0.0
}

case "${MODE}" in
    backend)
        start_backend
        ;;
    frontend)
        start_frontend
        ;;
    all|*)
        echo ">> 启动后端与前端并发进程..."
        (start_backend) &
        BACKEND_PID=$!
        
        # 等待后端端口就绪
        sleep 2
        (start_frontend) &
        FRONTEND_PID=$!

        trap "kill ${BACKEND_PID} ${FRONTEND_PID} 2>/dev/null || true; exit" SIGINT SIGTERM
        wait
        ;;
esac
