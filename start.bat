@echo off
chcp 65001 >nul
echo ==========================================================
echo  🚀 企业级多模态 RAG 系统 (LangGraph 2.0+ Windows 启动脚本)
echo ==========================================================

set SCRIPT_DIR=%~dp0
set PYTHONPATH=%SCRIPT_DIR%backend\src;%PYTHONPATH%

echo [1/2] 启动后端 FastAPI (LangGraph 引擎, 端口 8000)...
start "RAG Backend" cmd /k "cd /d %SCRIPT_DIR% && python -m uvicorn rag_kb.api.main:app --host 0.0.0.0 --port 8000 --reload"

timeout /t 3 /nobreak >nul

echo [2/2] 启动前端 Vite (端口 5173)...
start "RAG Frontend" cmd /k "cd /d %SCRIPT_DIR%frontend && npm run dev"

echo.
echo 系统已启动！
echo 前端访问地址: http://localhost:5173
echo 后端接口文档: http://localhost:8000/docs
pause
