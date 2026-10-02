"""src/rag_kb/graph/state.py —— 定义入库流水线的状态契约 IngestState 及默认状态工厂。

用 TypedDict 声明图上流转的全部字段（标识、元数据、阶段产物、中间结果、控制状态），
并给出 INGEST_DEFAULT_STATE 与 get_default_state()，供各节点、build.py 与 API 层构造初始状态。
"""

import copy
import uuid
from typing import Any, Dict, List, Optional, TypedDict
from rag_kb.core.constants import IngestStatus, STEP_NAMES

class IngestState(TypedDict):
    """入库流水线 LangGraph 全局流转状态契约"""
    # 基础文档标识与元数据
    doc_id: str
    file_name: str
    file_bytes: Optional[bytes]
    temp_file_path: Optional[str]
    source_url: Optional[str]
    raw_oss_url: Optional[str]
    file_hash: Optional[str]
    department: str
    category: str
    tags: List[str]

    # 控制状态与整体进度
    overall_status: str
    current_step_index: int
    current_step_name: str
    steps: List[Dict[str, Any]]
    skip_review: bool

    # 阶段 2 & 3: MinerU 与 Markdown 加载产物
    parsed_dir: Optional[str]
    parse_res: Optional[Dict[str, Any]]
    content_list: List[Dict[str, Any]]
    md_content: Optional[str]

    # 阶段 4 & 5: 配图上传与多模态提炼产物
    image_urls: Dict[str, str]

    # 阶段 6 & 7: 表格、代码块、流程图与人机审核挂起
    processed_tables: List[Any]
    processed_code_blocks: List[Any]
    processed_mermaid_flowcharts: List[Any]
    pending_reviews: List[Dict[str, Any]]
    review_results: Optional[Any]

    # 阶段 8: 描述回填增强 Markdown
    enhanced_md_content: Optional[str]
    enhanced_md_path: Optional[str]
    enhanced_md_oss_url: Optional[str]

    # 阶段 9 & 10: 两阶段切分与向量索引产物
    raw_chunks: List[Any]
    dense_embeddings: List[List[float]]
    sparse_vectors: List[Dict[str, float]]
    tsv_strings: List[str]

    # 异常中断与诊断
    error: Optional[str]
    failed_step: Optional[int]

def get_default_state(
    file_name: str,
    doc_id: Optional[str] = None,
    file_bytes: Optional[bytes] = None,
    temp_file_path: Optional[str] = None,
    source_url: Optional[str] = None,
    department: str = "技术部",
    category: str = "技术规范",
    tags: Optional[List[str]] = None,
    skip_review: bool = False
) -> IngestState:
    """构造初始入库流水线状态字典"""
    actual_doc_id = doc_id or str(uuid.uuid4())
    steps_initial = [
        {
            "step_index": i + 1,
            "name": name,
            "status": "waiting",
            "duration_ms": 0,
            "error": None
        }
        for i, name in enumerate(STEP_NAMES)
    ]

    state: IngestState = {
        "doc_id": actual_doc_id,
        "file_name": file_name,
        "file_bytes": file_bytes,
        "temp_file_path": temp_file_path,
        "source_url": source_url,
        "raw_oss_url": None,
        "file_hash": None,
        "department": department,
        "category": category,
        "tags": list(tags) if tags else [],

        "overall_status": IngestStatus.PENDING.value,
        "current_step_index": 1,
        "current_step_name": STEP_NAMES[0],
        "steps": steps_initial,
        "skip_review": skip_review,

        "parsed_dir": None,
        "parse_res": None,
        "content_list": [],
        "md_content": None,

        "image_urls": {},
        "processed_tables": [],
        "processed_code_blocks": [],
        "processed_mermaid_flowcharts": [],
        "pending_reviews": [],
        "review_results": None,

        "enhanced_md_content": None,
        "enhanced_md_path": None,
        "enhanced_md_oss_url": None,

        "raw_chunks": [],
        "dense_embeddings": [],
        "sparse_vectors": [],
        "tsv_strings": [],

        "error": None,
        "failed_step": None
    }
    return state
