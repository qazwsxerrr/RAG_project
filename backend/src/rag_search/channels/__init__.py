"""src/rag_search/channels/__init__.py —— channels 包的标识文件，收纳四路并行召回通道。

不做任何导入或聚合导出，调用方按子模块直接导入 dense / sparse / bm25 / hyde；它只用文档约定各通道需暴露 NAME 与 search 的语义接口。
隐含约定是各通道 NAME 必须互不相同，否则融合阶段按通道名累加时会把两路误当一路。
"""
