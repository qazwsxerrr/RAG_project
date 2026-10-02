"""src/rag_kb/nodes/__init__.py —— rag_kb.nodes 子包的包标识文件。

它只负责把 `nodes/` 标记为 Python 包，使 base / ingest / loader / splitter / store 等节点模块可被导入。
本身不定义节点、不导出任何符号，节点实例由 `rag_kb.graph.nodes` 精确到具体模块导入并注册。
"""
