import asyncio
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from backend.app.schemas.pipeline import (
    PipelineStepStatus,
    PipelineSnapshotResponse,
    ReviewItem
)

STEP_NAMES = [
    "接入",            # 1
    "MinerU 解析",     # 2
    "MD 加载",         # 3
    "图片上传",        # 4
    "图片描述",        # 5
    "表格转文本",      # 6
    "人工审核",        # 7
    "表格回填",        # 8
    "切分",            # 9
    "向量化",          # 10
    "入库"             # 11
]

class PipelineInstance:
    """单个文档入库流水线运行时实例"""

    def __init__(self, doc_id: str, file_name: str, department: str, category: str, tags: List[str]):
        self.doc_id = doc_id
        self.file_name = file_name
        self.department = department
        self.category = category
        self.tags = tags
        
        self.overall_status = "running" # running | pending_review | completed | failed
        self.current_step_index = 1
        self.current_step_name = STEP_NAMES[0]
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = datetime.now(timezone.utc)
        self.start_time = time.time()
        
        # 初始化 11 个标准节点
        self.steps: List[PipelineStepStatus] = [
            PipelineStepStatus(
                step_index=i + 1,
                name=name,
                status="waiting",
                duration_ms=0
            )
            for i, name in enumerate(STEP_NAMES)
        ]
        
        # 待审核项与审核结果
        self.pending_reviews: List[ReviewItem] = []
        self.review_future: Optional[asyncio.Future] = None
        
        # 失败状态与根因保护
        self.failed_step: Optional[int] = None
        self.error_message: Optional[str] = None
        self.root_cause_error: Optional[str] = None
        
        # SSE 事件分发队列与历史日志（支持断线重连）
        self.event_subscribers: List[asyncio.Queue] = []
        self.event_history: List[Dict[str, Any]] = []
        self.event_seq: int = 0
        self.context: Dict[str, Any] = {}


    def to_snapshot(self) -> PipelineSnapshotResponse:
        """生成静态快照（支持刷新与断线重连）"""
        dedup_res = self.context.get("dedup_res") or {}
        matched_id = dedup_res.get("matched_doc_id")
        return PipelineSnapshotResponse(
            doc_id=self.doc_id,
            file_name=self.file_name,
            overall_status=self.overall_status,
            current_step_index=self.current_step_index,
            current_step_name=self.current_step_name,
            steps=self.steps,
            requires_review=(self.overall_status == "pending_review"),
            pending_reviews=self.pending_reviews,
            is_duplicate_warning=(self.overall_status == "duplicate_warning"),
            matched_doc_id=str(matched_id) if matched_id else None
        )

    async def broadcast_event(self, event_type: str, data: Dict[str, Any]):
        """向所有 SSE 订阅者广播实时事件并计入历史日志"""
        self.event_seq += 1
        event_payload = {
            "id": str(self.event_seq),
            "event": event_type,
            "doc_id": self.doc_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data
        }
        self.event_history.append(event_payload)
        for q in list(self.event_subscribers):
            try:
                await q.put(event_payload)
            except Exception:
                pass

    def update_step_status(self, step_idx: int, status: str, duration_ms: int = 0, error: Optional[str] = None):
        """更新节点状态"""
        if 1 <= step_idx <= 11:
            step = self.steps[step_idx - 1]
            step.status = status
            step.duration_ms = duration_ms
            step.error_message = error
            step.updated_at = datetime.now(timezone.utc)
            self.current_step_index = step_idx
            self.current_step_name = STEP_NAMES[step_idx - 1]
            self.updated_at = datetime.now(timezone.utc)


import json
from pathlib import Path
from backend.app.core.config import settings

class CheckpointManager:
    """流水线物理检查点落盘与反序列化恢复管理器 (高可用容灾核心)"""

    @staticmethod
    def get_checkpoint_path(doc_id: str) -> Path:
        return Path(settings.DATA_PARSED_DIR) / doc_id / "checkpoint.json"

    @classmethod
    def save_checkpoint(
        cls,
        instance: PipelineInstance,
        failed_step: Optional[int] = None,
        error: Optional[str] = None
    ) -> Path:
        """
        将当前流水线状态、11 节点快照与三大物理资产元数据落盘至 checkpoint.json
        """
        doc_dir = Path(settings.DATA_PARSED_DIR) / instance.doc_id
        doc_dir.mkdir(parents=True, exist_ok=True)
        ckpt_file = cls.get_checkpoint_path(instance.doc_id)

        serialized_steps = [s.model_dump(mode="json") for s in instance.steps]

        assets = {
            "raw_oss_url": instance.context.get("raw_oss_url"),
            "sha256": instance.context.get("sha256"),
            "enhanced_md_path": instance.context.get("enhanced_md_path"),
            "enhanced_md_oss_url": instance.context.get("enhanced_md_oss_url"),
            "full_md_oss_url": instance.context.get("full_md_oss_url"),
        }

        # 固化 Checkpoint 2: 人工审核成果快照 (保护最值钱的人脑智慧资产)
        if instance.pending_reviews:
            review_snap_file = doc_dir / "review_snapshot.json"
            try:
                serialized_reviews = [r.model_dump(mode="json") for r in instance.pending_reviews]
                with open(review_snap_file, "w", encoding="utf-8") as f:
                    json.dump(serialized_reviews, f, ensure_ascii=False, indent=2, default=str)
                assets["review_snapshot_path"] = str(review_snap_file)
            except Exception:
                pass

        # 提取上下文可序列化的安全元数据
        context_meta = {}
        for k, v in instance.context.items():
            if k in ("raw_chunks", "dense_embeddings", "sparse_vectors", "tsv_strings"):
                continue
            if isinstance(v, (str, int, float, bool, list, dict)):
                context_meta[k] = v

        data = {
            "doc_id": instance.doc_id,
            "file_name": instance.file_name,
            "department": instance.department,
            "category": instance.category,
            "tags": instance.tags,
            "overall_status": instance.overall_status,
            "current_step_index": instance.current_step_index,
            "current_step_name": instance.current_step_name,
            "failed_step": failed_step if failed_step is not None else instance.failed_step,
            "error_message": error if error is not None else instance.error_message,
            "root_cause_error": getattr(instance, "root_cause_error", None) or error,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "steps": serialized_steps,
            "assets": assets,
            "context_meta": context_meta
        }

        with open(ckpt_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)

        return ckpt_file

    @classmethod
    def restore_instance(cls, doc_id: str) -> Optional[PipelineInstance]:
        """
        从本地 checkpoint.json 反序列化恢复已丢失的在途/失败流水线实例
        """
        ckpt_file = cls.get_checkpoint_path(doc_id)
        if not ckpt_file.exists():
            return None

        try:
            with open(ckpt_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None

        instance = PipelineInstance(
            doc_id=data["doc_id"],
            file_name=data["file_name"],
            department=data["department"],
            category=data["category"],
            tags=data.get("tags", [])
        )
        instance.overall_status = data.get("overall_status", "failed")
        instance.current_step_index = data.get("current_step_index", 1)
        instance.current_step_name = data.get("current_step_name", STEP_NAMES[instance.current_step_index - 1])
        instance.failed_step = data.get("failed_step")
        instance.error_message = data.get("error_message")
        instance.root_cause_error = data.get("root_cause_error") or data.get("error_message")


        # 恢复 11 节点状态列表
        if "steps" in data and isinstance(data["steps"], list):
            instance.steps = [PipelineStepStatus(**s) for s in data["steps"]]

        # 恢复上下文核心物理资产
        if "assets" in data and isinstance(data["assets"], dict):
            for k, v in data["assets"].items():
                if v:
                    instance.context[k] = v

        if "context_meta" in data and isinstance(data["context_meta"], dict):
            instance.context.update(data["context_meta"])

        # 恢复 Checkpoint 2: 人工审核快照
        doc_dir = Path(settings.DATA_PARSED_DIR) / doc_id
        review_snap_file = doc_dir / "review_snapshot.json"
        if review_snap_file.exists():
            try:
                with open(review_snap_file, "r", encoding="utf-8") as f:
                    reviews_data = json.load(f)
                instance.pending_reviews = [ReviewItem(**r) for r in reviews_data]
            except Exception:
                pass

        return instance


class PipelineManager:
    """全局流水线管理器与内存状态注册表 (支持磁盘检查点双重保障)"""

    def __init__(self):
        self._instances: Dict[str, PipelineInstance] = {}

    @property
    def instances(self) -> Dict[str, PipelineInstance]:
        return self._instances

    def create_instance(self, doc_id: str, file_name: str, department: str, category: str, tags: List[str]) -> PipelineInstance:
        instance = PipelineInstance(doc_id, file_name, department, category, tags)
        self._instances[doc_id] = instance
        # 初始固化检查点
        CheckpointManager.save_checkpoint(instance)
        return instance

    def get_instance(self, doc_id: str) -> Optional[PipelineInstance]:
        inst = self._instances.get(doc_id)
        if not inst:
            # 内存不存在（进程重启过），尝试从磁盘 Checkpoint 自动复活
            inst = CheckpointManager.restore_instance(doc_id)
            if inst:
                self._instances[doc_id] = inst
        return inst

    def remove_instance(self, doc_id: str) -> Optional[PipelineInstance]:
        inst = self._instances.pop(doc_id, None)
        if inst:
            inst.overall_status = "cancelled"
            CheckpointManager.save_checkpoint(inst)
        return inst

    def list_all(self) -> List[PipelineInstance]:
        return list(self._instances.values())

pipeline_manager = PipelineManager()
