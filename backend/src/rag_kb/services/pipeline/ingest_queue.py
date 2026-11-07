"""src/rag_kb/services/pipeline/ingest_queue.py —— 基于 Redis Streams 的文档异步削峰消费队列。

严格对齐 new-command.md (§2 容错降级策略、§5 海量数据清洗) 与 对话.txt (第 23~25 行)：
1. “先存后返 202”削峰解耦：
   - API 层将原文件直传 OSS、写入 rag_documents(overall_status='queued', is_active=False) 后，
     仅把轻量元数据（不含 file_bytes）通过 XADD 推入真实 Redis Streams 立即响应成功提示；
2. 受控并发匀速消费（防高并发 OOM 卡死）：
   - 后台 Worker 循环基于 XREADGROUP + asyncio.Semaphore(INGEST_WORKER_CONCURRENCY) 严格控制并发执行的流水线数；
   - 支持服务重启后从 PEL (Pending Entries List) 自动认领未完成任务继续执行；
3. 瞬态重试与失败引导用户重试：
   - 对非确定性异常（排除 [FORMAT_INVALID] 格式准入拦截）支持可配置自动重试；
   - 若最终失败，将错误原因与失败节点持久化至 rag_documents (overall_status='failed', pipeline_steps)，
     并广播 pipeline_failed 事件，前端明确提示错误原因并引导用户点击「重试」。
"""

import os
import uuid
import json
import socket
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List, Set
from langgraph.types import Command
from sqlalchemy import select

from rag_kb.core.config import settings
from rag_kb.core.constants import IngestStatus
from rag_kb.core.redis_client import get_redis_client
from rag_kb.db.session import AsyncSessionLocal
from rag_kb.db.models import Document
from rag_kb.graph.state import get_default_state
from rag_kb.graph.build import get_ingest_graph
from rag_kb.graph.edges import can_safely_jump_to_step
from rag_kb.api.sse import pipeline_manager

logger = logging.getLogger(__name__)


def cleanup_doc_temp_files(doc_id: str, explicit_temp_path: Optional[str] = None) -> int:
    """安全回收上传阶段落盘在 data/parsed/tmp 下的临时源文件（不影响解析目录与 OSS 原件）"""
    removed = 0
    tmp_root = (Path(settings.DATA_PARSED_DIR) / "tmp").resolve()
    candidates: List[Path] = []

    if explicit_temp_path:
        try:
            p = Path(explicit_temp_path).resolve()
            if p.parent == tmp_root and p.is_file():
                candidates.append(p)
        except OSError:
            pass

    if doc_id and tmp_root.is_dir():
        for matched in tmp_root.glob(f"{doc_id}_*"):
            if matched.is_file():
                candidates.append(matched)

    seen: Set[Path] = set()
    for target in candidates:
        if target in seen:
            continue
        seen.add(target)
        try:
            if target.exists():
                target.unlink()
                removed += 1
        except OSError as e:
            logger.warning(f"[IngestQueue] 清理临时上传文件失败 ({target}): {e}")
    return removed


class RedisIngestQueueService:
    """企业级 Redis Streams 文档异步入库消费中心"""

    def __init__(self):
        self.stream_key = settings.INGEST_STREAM_KEY
        self.group_name = settings.INGEST_CONSUMER_GROUP
        self.consumer_name = f"worker-{socket.gethostname()}-{os.getpid()}"
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None
        self._active_tasks: set[asyncio.Task] = set()
        self._active_doc_ids: set[str] = set()
        self._semaphore: Optional[asyncio.Semaphore] = None

    def _get_semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(max(1, int(settings.INGEST_WORKER_CONCURRENCY)))
        return self._semaphore

    async def ensure_consumer_group(self) -> None:
        """幂等创建 Redis Streams 消费者组（MKSTREAM）"""
        redis_cli = get_redis_client()
        try:
            await redis_cli.xgroup_create(
                name=self.stream_key,
                groupname=self.group_name,
                id="0",
                mkstream=True,
            )
            logger.info(f"[IngestQueue] 初始化消费者组成功: stream={self.stream_key}, group={self.group_name}")
        except Exception as e:
            if "BUSYGROUP" in str(e):
                return
            raise

    async def enqueue_ingest_task(
        self,
        doc_id: str,
        file_name: str,
        raw_oss_url: str,
        file_hash: str,
        department: str,
        category: str,
        tags: List[str],
        skip_review: bool = False,
        temp_file_path: Optional[str] = None,
        attempt: int = 0,
        from_step: int = 1,
    ) -> str:
        """向真实 Redis Streams 投递轻量文档解析任务（严禁携带大体积二进制）"""
        await self.ensure_consumer_group()
        redis_cli = get_redis_client()
        eff_step = max(1, min(11, int(from_step or 1)))

        payload = {
            "doc_id": str(doc_id),
            "file_name": str(file_name),
            "raw_oss_url": str(raw_oss_url),
            "file_hash": str(file_hash),
            "department": str(department),
            "category": str(category),
            "tags_json": json.dumps(tags or [], ensure_ascii=False),
            "skip_review": "1" if skip_review else "0",
            "temp_file_path": str(temp_file_path or ""),
            "attempt": str(int(attempt)),
            "from_step": str(eff_step),
        }
        msg_id = await redis_cli.xadd(
            self.stream_key,
            payload,
            maxlen=50000,
            approximate=True,
        )
        logger.info(
            f"[IngestQueue] 文档已入队排队削峰: doc_id={doc_id}, msg_id={msg_id}, attempt={attempt}, from_step={eff_step}"
        )
        return str(msg_id)

    async def start_worker(self) -> None:
        """在应用生命周期内启动后台异步消费协程"""
        if self._running and self._worker_task and not self._worker_task.done():
            return
        await self.ensure_consumer_group()
        self._semaphore = asyncio.Semaphore(max(1, int(settings.INGEST_WORKER_CONCURRENCY)))
        self._running = True
        self._worker_task = asyncio.create_task(self._consume_loop(), name="redis-ingest-stream-worker")
        logger.info(
            f"[IngestQueue] 后台异步削峰消费器已启动 (concurrency={settings.INGEST_WORKER_CONCURRENCY}, consumer={self.consumer_name})"
        )

    async def stop_worker(self) -> None:
        """优雅关闭后台消费协程并等待在途任务收尾"""
        self._running = False
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        self._worker_task = None

        # 等待在途活跃任务收尾（设置 10s 优雅超时，超时强制取消）
        if self._active_tasks:
            pending = list(self._active_tasks)
            done, not_done = await asyncio.wait(pending, timeout=10.0)
            if not_done:
                logger.warning(f"[IngestQueue] 停机超时，强制取消 {len(not_done)} 个未完成在途任务，留待下次启动 PEL 恢复")
                for t in not_done:
                    t.cancel()
                await asyncio.gather(*not_done, return_exceptions=True)
            self._active_tasks.clear()

    async def process_one_pending_for_test(self, block_ms: int = 500) -> bool:
        """单步拉取并执行一条队列消息（供集成测试确定性验证真实队列链路）"""
        await self.ensure_consumer_group()
        redis_cli = get_redis_client()
        entries = await redis_cli.xreadgroup(
            groupname=self.group_name,
            consumername=self.consumer_name,
            streams={self.stream_key: ">"},
            count=1,
            block=block_ms,
        )
        if not entries:
            return False
        for _, messages in entries:
            for msg_id, fields in messages:
                await self._execute_stream_entry(msg_id, fields)
        return True

    async def _consume_loop(self) -> None:
        """主消费循环：先恢复 PEL 遗留未确认消息与 DB 孤儿 queued 任务，再持续监听新投递消息"""
        redis_cli = get_redis_client()

        # 1. 启动时秒级接管（min_idle_ms=0 立即认领 PEL 中所有前任遗留未确认消息）与 DB 遗留任务
        try:
            await self._recover_pending_entries(min_idle_ms=0)
            await self._recover_db_orphan_queued_docs()
        except Exception as rec_err:
            logger.warning(f"[IngestQueue] 启动恢复巡检提示: {rec_err}")

        last_pel_check = asyncio.get_event_loop().time()

        # 2. 持续受控并发消费新消息
        while self._running:
            # 周期性（每 30s）巡检 PEL，认领长耗时挂死或故障遗留的任务
            now = asyncio.get_event_loop().time()
            if now - last_pel_check >= 30.0:
                last_pel_check = now
                try:
                    await self._recover_pending_entries()
                except Exception as pel_err:
                    logger.warning(f"[IngestQueue] 周期巡检 PEL 异常: {pel_err}")
            try:
                sem = self._get_semaphore()
                await sem.acquire()
                try:
                    entries = await redis_cli.xreadgroup(
                        groupname=self.group_name,
                        consumername=self.consumer_name,
                        streams={self.stream_key: ">"},
                        count=1,
                        block=2000,
                    )
                except Exception:
                    sem.release()
                    raise

                if not entries:
                    sem.release()
                    continue

                dispatched = False
                for _, messages in entries:
                    for msg_id, fields in messages:
                        dispatched = True
                        task = asyncio.create_task(self._run_with_released_slot(sem, msg_id, fields))
                        self._active_tasks.add(task)
                        task.add_done_callback(self._active_tasks.discard)

                if not dispatched:
                    sem.release()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[IngestQueue] 消费循环异常: {e}", exc_info=True)
                await asyncio.sleep(2.0)

    async def _recover_pending_entries(self, min_idle_ms: Optional[int] = None) -> int:
        """使用 XAUTOCLAIM 循环分页认领全部空闲超时的未 XACK 任务（严格接线 settings.INGEST_PEL_IDLE_MS）"""
        redis_cli = get_redis_client()
        idle_threshold = max(0, int(settings.INGEST_PEL_IDLE_MS if min_idle_ms is None else min_idle_ms))
        cursor = "0-0"
        recovered_count = 0

        while True:
            res = await redis_cli.xautoclaim(
                name=self.stream_key,
                groupname=self.group_name,
                consumername=self.consumer_name,
                min_idle_time=idle_threshold,
                start_id=cursor,
                count=50,
            )
            if not isinstance(res, (list, tuple)) or len(res) < 2:
                break
            next_cursor = str(res[0])
            claimed_msgs = res[1] or []
            if not claimed_msgs:
                break

            for msg_id, fields in claimed_msgs:
                if not fields:
                    await redis_cli.xack(self.stream_key, self.group_name, msg_id)
                    continue
                recovered_count += 1
                claimed_did = str(fields.get("doc_id") or "")
                if claimed_did:
                    self._active_doc_ids.add(claimed_did)
                sem = self._get_semaphore()
                await sem.acquire()
                task = asyncio.create_task(self._run_with_released_slot(sem, msg_id, fields))
                self._active_tasks.add(task)
                task.add_done_callback(self._active_tasks.discard)

            if next_cursor in ("0-0", "0", cursor):
                break
            cursor = next_cursor

        if recovered_count > 0:
            logger.info(f"[IngestQueue] 启动 PEL 崩溃恢复已认领 {recovered_count} 条未确认消息 (idle>={idle_threshold}ms)")
        return recovered_count

    async def _recover_db_orphan_queued_docs(self) -> int:
        """
        扫描 PostgreSQL rag_documents 中处于 queued / running 状态的遗留记录，
        联合比对当前进程在途 _active_doc_ids、消费者组 PEL 未确认条目及 Redis Stream 存量消息，
        仅将确无队列与在途条目的孤儿文档安全补投递回队列，确保零丢单且零重复。
        """
        await self.ensure_consumer_group()
        redis_cli = get_redis_client()

        async with AsyncSessionLocal() as session:
            stmt = select(Document).where(Document.overall_status.in_([IngestStatus.QUEUED.value, "running"]))
            res = await session.execute(stmt)
            queued_docs = res.scalars().all()

        if not queued_docs:
            return 0

        # 收集当前在途活跃任务、PEL 待确认条目与 Redis Stream 中的 doc_id，彻底杜绝重复入队
        existing_stream_doc_ids: Set[str] = set(self._active_doc_ids)
        try:
            pel_entries = await redis_cli.xpending_range(
                self.stream_key, self.group_name, min="-", max="+", count=5000
            )
            for pe in pel_entries or []:
                pel_msg_id = pe.get("message_id") if isinstance(pe, dict) else None
                if pel_msg_id:
                    msg_rows = await redis_cli.xrange(self.stream_key, min=pel_msg_id, max=pel_msg_id, count=1)
                    for _, p_fields in msg_rows:
                        p_did = p_fields.get("doc_id")
                        if p_did:
                            existing_stream_doc_ids.add(str(p_did))

            recent_entries = await redis_cli.xrevrange(self.stream_key, count=5000)
            for _, fields in recent_entries:
                did = fields.get("doc_id")
                if did:
                    existing_stream_doc_ids.add(str(did))
        except Exception as e:
            logger.warning(f"[IngestQueue] 读取 Stream 存量快照异常: {e}")

        requeued = 0
        for doc in queued_docs:
            doc_id_str = str(doc.id)
            if doc_id_str in existing_stream_doc_ids:
                continue
            persisted_skip_review = bool(
                doc.pipeline_steps
                and isinstance(doc.pipeline_steps, list)
                and isinstance(doc.pipeline_steps[0], dict)
                and doc.pipeline_steps[0].get("skip_review", False)
            )
            await self.enqueue_ingest_task(
                doc_id=doc_id_str,
                file_name=doc.title,
                raw_oss_url=doc.raw_oss_url,
                file_hash=doc.file_hash,
                department=doc.department_path or "/public",
                category=doc.category or "技术规范",
                tags=doc.tags or [],
                skip_review=persisted_skip_review,
                temp_file_path=None,
                attempt=0,
                from_step=max(1, int(doc.current_step_index or 1)),
            )
            requeued += 1

        if requeued > 0:
            logger.info(f"[IngestQueue] 已从 PostgreSQL 恢复补投递 {requeued} 条孤儿 queued 文档任务")
        return requeued

    async def _run_with_released_slot(self, sem: asyncio.Semaphore, msg_id: str, fields: Dict[str, str]) -> None:
        did = str(fields.get("doc_id") or "")
        if did:
            self._active_doc_ids.add(did)
        try:
            await self._execute_stream_entry(msg_id, fields)
        finally:
            if did:
                self._active_doc_ids.discard(did)
            sem.release()

    async def _sync_db_pipeline_state(
        self,
        doc_id: str,
        overall_status: str,
        step_index: int,
        steps: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """将队列执行状态与错误现场持久化同步至 PostgreSQL rag_documents 表"""
        try:
            doc_uuid = uuid.UUID(str(doc_id))
        except ValueError:
            return

        async with AsyncSessionLocal() as session:
            doc = await session.get(Document, doc_uuid)
            if not doc:
                return
            # 若节点 11 StoreNode 已完成蓝绿切换将其置为 active，不再回退覆盖其状态
            if doc.overall_status == "active" and overall_status in ("completed", "running"):
                if steps is not None:
                    doc.pipeline_steps = steps
                    doc.current_step_index = 11
                    await session.commit()
                return

            doc.overall_status = overall_status
            doc.current_step_index = step_index
            if steps is not None:
                doc.pipeline_steps = steps
            await session.commit()

    async def _finalize_post_graph_state(self, doc_id: str, fallback_state: Dict[str, Any]) -> None:
        """统一收口图执行/唤醒结束后的数据库终态持久化、SSE 广播与成功临时文件回收"""
        final_mem_state = pipeline_manager.get_state(doc_id) or fallback_state
        final_status = final_mem_state.get("overall_status", "")
        err_msg = final_mem_state.get("error")

        if final_status == IngestStatus.FAILED.value or err_msg:
            failed_step = final_mem_state.get("failed_step") or final_mem_state.get("current_step_index", 1)
            await self._sync_db_pipeline_state(
                doc_id=doc_id,
                overall_status=IngestStatus.FAILED.value,
                step_index=failed_step,
                steps=final_mem_state.get("steps"),
            )
            await pipeline_manager.broadcast(
                doc_id,
                "pipeline_failed",
                {
                    "doc_id": doc_id,
                    "step": failed_step,
                    "error": err_msg or "流水线节点执行失败，请检查文档或点击重试",
                },
            )
        elif final_status == IngestStatus.REVIEW.value:
            await self._sync_db_pipeline_state(
                doc_id=doc_id,
                overall_status=IngestStatus.REVIEW.value,
                step_index=7,
                steps=final_mem_state.get("steps"),
            )
        elif final_status == IngestStatus.DUPLICATE_WARNING.value:
            await self._sync_db_pipeline_state(
                doc_id=doc_id,
                overall_status=IngestStatus.DUPLICATE_WARNING.value,
                step_index=11,
                steps=final_mem_state.get("steps"),
            )
        else:
            await self._sync_db_pipeline_state(
                doc_id=doc_id,
                overall_status="active",
                step_index=11,
                steps=final_mem_state.get("steps"),
            )
            cleanup_doc_temp_files(doc_id, final_mem_state.get("temp_file_path"))
            final_mem_state["temp_file_path"] = None

    async def resume_after_review(self, doc_id: str, resume_payload: List[Dict[str, Any]]) -> None:
        """受队列并发信号量保护唤醒第 7 步人工审核挂起图，并完整同步 DB 终态"""
        sem = self._get_semaphore()
        await sem.acquire()
        try:
            state = pipeline_manager.get_state(doc_id) or {}
            state["overall_status"] = "running"
            state["error"] = None
            state["failed_step"] = None
            await self._sync_db_pipeline_state(doc_id, "running", 8, state.get("steps"))
            await pipeline_manager.broadcast(
                doc_id,
                "pipeline_resumed",
                {
                    "doc_id": doc_id,
                    "from_step": 8,
                    "message": "人工审核已确认，流水线继续执行下一步",
                },
            )

            config = {"configurable": {"thread_id": doc_id}}
            graph = get_ingest_graph()
            latest_graph_state: Optional[Dict[str, Any]] = None
            async for mode, chunk in graph.astream(
                Command(resume=resume_payload),
                config=config,
                stream_mode=["custom", "values"],
            ):
                if mode == "custom" and isinstance(chunk, dict):
                    evt_name = chunk.get("event", "message")
                    await pipeline_manager.broadcast(doc_id, evt_name, chunk)
                elif mode == "values" and isinstance(chunk, dict):
                    latest_graph_state = chunk

            if latest_graph_state:
                for preserve_key in ("doc_id", "file_name", "department", "category", "tags", "raw_oss_url", "file_hash"):
                    if not latest_graph_state.get(preserve_key) and state.get(preserve_key):
                        latest_graph_state[preserve_key] = state[preserve_key]
                pipeline_manager.update_state(doc_id, latest_graph_state)
            await self._finalize_post_graph_state(doc_id, latest_graph_state or state)
        except Exception as err:
            logger.error(f"[IngestQueue] 人工审核唤醒后执行失败 [doc_id={doc_id}]: {err}", exc_info=True)
            curr_state = pipeline_manager.get_state(doc_id) or {}
            step_idx = curr_state.get("failed_step") or curr_state.get("current_step_index", 8)
            await self._sync_db_pipeline_state(
                doc_id=doc_id,
                overall_status=IngestStatus.FAILED.value,
                step_index=step_idx,
                steps=curr_state.get("steps"),
            )
            await pipeline_manager.broadcast(
                doc_id,
                "pipeline_failed",
                {
                    "doc_id": doc_id,
                    "step": step_idx,
                    "error": str(err),
                },
            )
        finally:
            sem.release()

    async def resume_after_duplicate_decision(self, doc_id: str, state: Dict[str, Any]) -> None:
        """受队列并发信号量保护恢复第 11 步防重冲突决策后的入库操作，并完整同步 DB 终态"""
        sem = self._get_semaphore()
        await sem.acquire()
        try:
            eff_from = 11 if can_safely_jump_to_step(state, 11) else 1
            state["resume_from_step"] = eff_from
            state["overall_status"] = "running"
            state["error"] = None
            state["failed_step"] = None
            await self._sync_db_pipeline_state(doc_id, "running", eff_from, state.get("steps"))
            await pipeline_manager.broadcast(
                doc_id,
                "pipeline_resumed",
                {
                    "doc_id": doc_id,
                    "from_step": eff_from,
                    "message": "已确认防重策略，正在完成最终入库...",
                },
            )

            config = {"configurable": {"thread_id": doc_id}}
            graph = get_ingest_graph()
            latest_graph_state: Optional[Dict[str, Any]] = None
            async for mode, chunk in graph.astream(
                state,
                config=config,
                stream_mode=["custom", "values"],
            ):
                if mode == "custom" and isinstance(chunk, dict):
                    evt_name = chunk.get("event", "message")
                    await pipeline_manager.broadcast(doc_id, evt_name, chunk)
                elif mode == "values" and isinstance(chunk, dict):
                    latest_graph_state = chunk

            state["resume_from_step"] = None
            if latest_graph_state:
                latest_graph_state["resume_from_step"] = None
                pipeline_manager.update_state(doc_id, latest_graph_state)
            await self._finalize_post_graph_state(doc_id, latest_graph_state or state)
        except Exception as err:
            logger.error(f"[IngestQueue] 防重决策恢复执行失败 [doc_id={doc_id}]: {err}", exc_info=True)
            state["resume_from_step"] = None
            curr_state = pipeline_manager.get_state(doc_id) or state
            step_idx = curr_state.get("failed_step") or curr_state.get("current_step_index", 11)
            await self._sync_db_pipeline_state(
                doc_id=doc_id,
                overall_status=IngestStatus.FAILED.value,
                step_index=step_idx,
                steps=curr_state.get("steps"),
            )
            await pipeline_manager.broadcast(
                doc_id,
                "pipeline_failed",
                {
                    "doc_id": doc_id,
                    "step": step_idx,
                    "error": str(err),
                },
            )
        finally:
            sem.release()

    def schedule_background_resume(self, coro) -> asyncio.Task:
        """统一纳管后台唤醒协程生命周期，避免悬空 Task 被 GC 回收"""
        task = asyncio.create_task(coro)
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)
        return task

    async def _execute_stream_entry(self, msg_id: str, fields: Dict[str, str]) -> None:
        """真正执行单篇文档的 11 节点 LangGraph 解析流水线"""
        redis_cli = get_redis_client()
        doc_id = fields.get("doc_id", "")
        file_name = fields.get("file_name", "document.pdf")
        raw_oss_url = fields.get("raw_oss_url", "")
        file_hash = fields.get("file_hash", "")
        department = fields.get("department", "/public")
        category = fields.get("category", "技术规范")
        temp_file_path = fields.get("temp_file_path") or None
        skip_review = fields.get("skip_review") == "1"
        attempt = int(fields.get("attempt", "0") or 0)
        requested_from_step = max(1, min(11, int(fields.get("from_step", "1") or 1)))

        try:
            tags = json.loads(fields.get("tags_json", "[]"))
            if not isinstance(tags, list):
                tags = []
        except Exception:
            tags = []

        if not doc_id:
            await redis_cli.xack(self.stream_key, self.group_name, msg_id)
            return

        # 若文档在排队期间已被用户物理删除，清理残余临时文件并直接 XACK 跳过，避免浪费算力
        try:
            doc_uuid = uuid.UUID(doc_id)
            async with AsyncSessionLocal() as session:
                db_doc = await session.get(Document, doc_uuid)
                if db_doc is None:
                    logger.info(f"[IngestQueue] 文档已在排队期间被删除，跳过消费: doc_id={doc_id}")
                    cleanup_doc_temp_files(doc_id, temp_file_path)
                    await redis_cli.xack(self.stream_key, self.group_name, msg_id)
                    return
        except ValueError:
            await redis_cli.xack(self.stream_key, self.group_name, msg_id)
            return

        state = pipeline_manager.get_state(doc_id)
        if not state:
            state = get_default_state(
                doc_id=doc_id,
                file_name=file_name,
                temp_file_path=temp_file_path,
                department=department,
                category=category,
                tags=tags,
                skip_review=skip_review,
            )
            state["raw_oss_url"] = raw_oss_url
            state["file_hash"] = file_hash
            pipeline_manager.register_pipeline(doc_id, state)
        else:
            state["raw_oss_url"] = raw_oss_url or state.get("raw_oss_url")
            state["file_hash"] = file_hash or state.get("file_hash")
            state["error"] = None
            state["failed_step"] = None

        effective_start_step = (
            requested_from_step
            if (requested_from_step > 1 and can_safely_jump_to_step(state, requested_from_step))
            else 1
        )
        state["resume_from_step"] = effective_start_step if effective_start_step > 1 else None
        state["current_step_index"] = effective_start_step
        state["overall_status"] = "running"
        await self._sync_db_pipeline_state(doc_id, "running", effective_start_step, state.get("steps"))

        config = {"configurable": {"thread_id": doc_id}}
        graph = get_ingest_graph()

        try:
            latest_graph_state: Optional[Dict[str, Any]] = None
            async for mode, chunk in graph.astream(
                state,
                config=config,
                stream_mode=["custom", "values"],
            ):
                if mode == "custom" and isinstance(chunk, dict):
                    evt_name = chunk.get("event", "message")
                    await pipeline_manager.broadcast(doc_id, evt_name, chunk)
                elif mode == "values" and isinstance(chunk, dict):
                    latest_graph_state = chunk

            state["resume_from_step"] = None
            if latest_graph_state:
                latest_graph_state["resume_from_step"] = None
                pipeline_manager.update_state(doc_id, latest_graph_state)

            # 检查图结束后的最新态（BaseNode 捕获节点异常时会把 state["overall_status"] 置为 "failed" 并短路至 END）
            final_mem_state = latest_graph_state or pipeline_manager.get_state(doc_id) or state
            final_status = final_mem_state.get("overall_status", "")
            err_msg = final_mem_state.get("error")

            if final_status == IngestStatus.FAILED.value or err_msg:
                failed_step = final_mem_state.get("failed_step") or final_mem_state.get("current_step_index", 1)
                is_format_rejected = "[FORMAT_INVALID]" in str(err_msg or "")

                # 若非确定性格式错误且尚有自动重试配额，重新投递队列重试一次（保留失败步骤上下文）
                if (not is_format_rejected) and attempt < settings.INGEST_MAX_AUTO_RETRIES:
                    logger.warning(
                        f"[IngestQueue] 文档解析瞬态失败，执行队列自动重试 ({attempt + 1}/{settings.INGEST_MAX_AUTO_RETRIES}): doc_id={doc_id}, err={err_msg}"
                    )
                    # 重试前清空历史步骤报错残留并重置状态为 waiting
                    steps = final_mem_state.get("steps", [])
                    for idx in range(max(0, failed_step - 1), len(steps)):
                        steps[idx]["status"] = "waiting"
                        steps[idx]["error"] = None
                        steps[idx]["duration_ms"] = 0
                    final_mem_state["error"] = None
                    final_mem_state["failed_step"] = None
                    final_mem_state["overall_status"] = "queued"
                    await self._sync_db_pipeline_state(doc_id, "queued", failed_step, steps)

                    await redis_cli.xack(self.stream_key, self.group_name, msg_id)
                    await self.enqueue_ingest_task(
                        doc_id=doc_id,
                        file_name=file_name,
                        raw_oss_url=raw_oss_url,
                        file_hash=file_hash,
                        department=department,
                        category=category,
                        tags=tags,
                        skip_review=skip_review,
                        temp_file_path=temp_file_path,
                        attempt=attempt + 1,
                        from_step=failed_step,
                    )
                    return

            await self._finalize_post_graph_state(doc_id, final_mem_state)
            await redis_cli.xack(self.stream_key, self.group_name, msg_id)

        except Exception as err:
            state["resume_from_step"] = None
            logger.error(f"[IngestQueue] 流水线消费顶层异常 [doc_id={doc_id}]: {err}", exc_info=True)
            is_format_rejected = "[FORMAT_INVALID]" in str(err)
            curr_state = pipeline_manager.get_state(doc_id) or state
            step_idx = curr_state.get("failed_step") or curr_state.get("current_step_index", 1)

            if (not is_format_rejected) and attempt < settings.INGEST_MAX_AUTO_RETRIES:
                steps = curr_state.get("steps", [])
                for idx in range(max(0, step_idx - 1), len(steps)):
                    steps[idx]["status"] = "waiting"
                    steps[idx]["error"] = None
                    steps[idx]["duration_ms"] = 0
                curr_state["error"] = None
                curr_state["failed_step"] = None
                curr_state["overall_status"] = "queued"
                await self._sync_db_pipeline_state(doc_id, "queued", step_idx, steps)
                await redis_cli.xack(self.stream_key, self.group_name, msg_id)
                await self.enqueue_ingest_task(
                    doc_id=doc_id,
                    file_name=file_name,
                    raw_oss_url=raw_oss_url,
                    file_hash=file_hash,
                    department=department,
                    category=category,
                    tags=tags,
                    skip_review=skip_review,
                    temp_file_path=temp_file_path,
                    attempt=attempt + 1,
                    from_step=step_idx,
                )
                return

            await self._sync_db_pipeline_state(
                doc_id=doc_id,
                overall_status=IngestStatus.FAILED.value,
                step_index=step_idx,
                steps=curr_state.get("steps"),
            )
            await pipeline_manager.broadcast(
                doc_id,
                "pipeline_failed",
                {
                    "doc_id": doc_id,
                    "step": step_idx,
                    "error": str(err),
                },
            )
            await redis_cli.xack(self.stream_key, self.group_name, msg_id)


ingest_queue_service = RedisIngestQueueService()
