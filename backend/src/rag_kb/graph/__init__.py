"""src/rag_kb/graph/__init__.py —— rag_kb.graph 子包的包标识文件。

它只负责把 `graph/` 标记为 Python 包，使 state / nodes / edges / build 等子模块可被正常导入。
本身不导出任何符号，对外的真正入口是 `rag_kb.graph.build.get_ingest_graph()`。
"""
