"""src/rag_search/edges.py —— 检索图的节点名常量与执行顺序收口处。

集中维护 NODE_* 节点名字符串，以及 CHANNEL_NODES（四路召回的扇出顺序）与 TRACE_ORDER（轨迹对外展示顺序）。
build 按它注册节点与连边、nodes 用它写轨迹、API 层按 TRACE_ORDER 重排并行轨迹，统一收口以免裸字符串拼错或改漏。
"""

from typing import List

NODE_PREPARE = "prepare"
NODE_DENSE = "dense"
NODE_SPARSE = "sparse"
NODE_BM25 = "bm25"
NODE_HYDE = "hyde"
NODE_FUSE = "fuse"
NODE_RERANK = "rerank"
NODE_MMR = "mmr"

# 四路并行召回通道扇出节点列表
CHANNEL_NODES: List[str] = [
    NODE_DENSE,
    NODE_SPARSE,
    NODE_BM25,
    NODE_HYDE
]

# 检索流程思考流轨迹的标准展示顺序
TRACE_ORDER: List[str] = [
    NODE_PREPARE,
    NODE_DENSE,
    NODE_SPARSE,
    NODE_BM25,
    NODE_HYDE,
    NODE_FUSE,
    NODE_RERANK,
    NODE_MMR
]
