"""src/rag_kb/api/__init__.py —— rag_kb.api 包的标识文件。

只把 api 目录标记为常规 Python 包，不做任何再导出，调用方须写全路径（如 from rag_kb.api.main import app）。
刻意保持为空，避免 import 本包时触发 FastAPI 应用构建等副作用。
"""
