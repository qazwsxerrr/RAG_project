"""src/rag_kb/api/system.py —— 大模型渠道状态与全链路健康监控 API。

提供：
1. GET /api/v1/system/llm/channels?range=90m|24h|7d|30d —— 全局模型与基建渠道监控大盘真实数据；
2. GET /api/v1/system/llm/channels/{node_name}/audits —— 单模型历史审计日志抽屉明细；
3. POST /api/v1/system/llm/channels/{node_name}/reset —— 管理员手动重置故障熔断状态机并记录持久化审计。
"""

import time
import uuid
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List
from fastapi import APIRouter, HTTPException, Depends, Query
from sqlalchemy import select, desc, or_

from rag_kb.api.deps import UserContext, require_permission
from rag_kb.core.config import settings, derive_provider_from_url
from rag_kb.core.llm import llm_gateway
from rag_kb.core.resilience.circuit_breaker import CallMetric
from rag_kb.core.logging.context import get_request_id
from rag_kb.core.model_credentials import get_cached_lease_info
from rag_kb.db.session import AsyncSessionLocal
from rag_kb.db.models import AuditLog

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/system", tags=["系统监控"])

_RANGE_DELTAS = {
    "90m": timedelta(minutes=90),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

_VENDOR_TITLES = {
    "KEDAYA": "KEDAYA (GROK 旗舰主节点)",
    "SILICONFLOW": "SILICONFLOW (高可用容灾备选池)",
    "DEEPSEEK": "DEEPSEEK (官方高可用候选池)",
    "DASHSCOPE": "DASHSCOPE (阿里云百炼服务池)",
    "ZHIPU": "ZHIPU (智谱清言大模型池)",
    "OPENAI": "OPENAI (官方模型候选池)",
    "VOLCES": "VOLCES (火山引擎服务池)",
    "INFRA": "EMBEDDING & RERANK (混合检索算力支持)",
}


def _ensure_utc(dt: datetime) -> datetime:
    """将数据库返回的 datetime 归一化为带 UTC 时区对象，消除 naive/aware 比较报错"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_local_time(dt: datetime, fmt: str = "%H:%M:%S") -> str:
    """将数据库 UTC 时间转为本地时区字符串供前端展示"""
    return _ensure_utc(dt).astimezone().strftime(fmt)


async def hydrate_llm_health_tracker_from_db() -> None:
    """应用启动时从 PostgreSQL sys_audit_logs 冷启动校准熔断器状态 (单次执行，数据库异常严格 Fail-Fast)"""
    tracker = llm_gateway.health_tracker
    cfg = llm_gateway.config
    all_nodes = [cfg.primary] + [f for f in cfg.fallbacks if f.name != cfg.primary.name]

    try:
        async with AsyncSessionLocal() as session:
            for node in all_nodes:
                stmt_reset = select(AuditLog).where(
                    AuditLog.action == "LLM_CIRCUIT_RESET",
                    AuditLog.resource_id == node.name
                ).order_by(desc(AuditLog.created_at)).limit(1)
                reset_log = (await session.execute(stmt_reset)).scalar_one_or_none()
                last_reset_ts = _ensure_utc(reset_log.created_at).timestamp() if (reset_log and reset_log.created_at) else 0.0

                stmt_calls = select(AuditLog).where(
                    AuditLog.action == "LLM_INFERENCE",
                    AuditLog.resource_id == node.name
                ).order_by(desc(AuditLog.created_at)).limit(tracker.window_size)
                logs = (await session.execute(stmt_calls)).scalars().all()

                metrics: List[CallMetric] = []
                for log in reversed(logs):
                    ts = _ensure_utc(log.created_at).timestamp() if log.created_at else time.time()
                    details = log.details or {}
                    metrics.append(CallMetric(
                        timestamp=ts,
                        success=(log.status == "SUCCESS"),
                        duration_ms=log.duration_ms or 0,
                        ttft_ms=details.get("ttft_ms"),
                        error_category=details.get("error_category") or log.error_msg
                    ))

                tracker.hydrate_once(node.name, metrics, last_reset_ts)
        logger.info("[SystemMonitoring] 已成功从数据库完成 LLM 熔断状态机冷启动校准。")
    except Exception as e:
        logger.error(f"[SystemMonitoring] 冷启动校准熔断器失败: {e}")
        raise RuntimeError(f"【启动监控校准失败】无法从 PostgreSQL sys_audit_logs 读取历史状态: {e} (Fail-Fast)") from e


def _aggregate_logs_metrics(logs: List[AuditLog]) -> Dict[str, Any]:
    """统一聚合节点调用流水的 SLA、均值延迟、P95 延迟、TTFT 与最近 18 柱状态条"""
    calls_count = len(logs)
    success_count = sum(1 for l in logs if l.status == "SUCCESS")
    durations = [l.duration_ms for l in logs if l.duration_ms is not None]
    ttfts = [l.details.get("ttft_ms") for l in logs if l.details and l.details.get("ttft_ms") is not None]

    if calls_count == 0:
        sla_percent = 100.0
        avg_lat = 0
        p95_lat = 0
        avg_ttft = 0
    else:
        sla_percent = round((success_count / calls_count) * 100.0, 1)
        avg_lat = int(sum(durations) / len(durations)) if durations else 0
        sorted_durations = sorted(durations)
        p95_idx = int(len(sorted_durations) * 0.95)
        p95_lat = int(sorted_durations[min(p95_idx, len(sorted_durations) - 1)]) if sorted_durations else 0
        avg_ttft = int(sum(ttfts) / len(ttfts)) if ttfts else 0

    recent_bars = []
    for l in logs[-18:]:
        ts = _ensure_utc(l.created_at).timestamp() if l.created_at else time.time()
        details = l.details or {}
        recent_bars.append({
            "status": "SUCCESS" if l.status == "SUCCESS" else "FAILED",
            "duration_ms": l.duration_ms or 0,
            "ttft_ms": details.get("ttft_ms"),
            "error_category": details.get("error_category") or l.error_msg,
            "timestamp": ts,
            "time_str": _format_local_time(l.created_at) if l.created_at else ""
        })

    return {
        "calls_count": calls_count,
        "success_count": success_count,
        "ttfts": ttfts,
        "sla_percent": sla_percent,
        "avg_lat": avg_lat,
        "p95_lat": p95_lat,
        "avg_ttft": avg_ttft,
        "recent_bars": recent_bars,
    }


@router.get("/llm/channels")
async def get_llm_channels_status(
    range: str = Query("90m", pattern="^(90m|24h|7d|30d)$"),
    user: UserContext = Depends(require_permission("kb:retrieve"))
):
    """获取所有模型渠道与检索基建组件的真实监控大盘数据 (基于 UTC 时间窗与 SSOT 审计表)"""
    tracker = llm_gateway.health_tracker
    cfg = llm_gateway.config
    all_nodes = [cfg.primary] + [f for f in cfg.fallbacks if f.name != cfg.primary.name if f.is_active]

    now_utc = datetime.now(timezone.utc)
    since_dt = now_utc - _RANGE_DELTAS.get(range, timedelta(minutes=90))

    infra_defs = [
        ("embedding-dense-bge", settings.EMBEDDING_MODEL, settings.EMBEDDING_BASE_URL, "model:embedding", "embeddings"),
        ("reranker-cross-encoder", settings.RERANK_MODEL, settings.RERANK_BASE_URL, "model:rerank", "rerank"),
    ]
    infra_names = [d[0] for d in infra_defs]
    all_node_names = [n.name for n in all_nodes]

    try:
        async with AsyncSessionLocal() as session:
            reset_stmt = select(AuditLog.resource_id, AuditLog.created_at).where(
                AuditLog.action == "LLM_CIRCUIT_RESET"
            ).order_by(desc(AuditLog.created_at))
            reset_rows = (await session.execute(reset_stmt)).all()
            latest_reset_by_node: Dict[str, datetime] = {}
            for r_id, c_at in reset_rows:
                if r_id and r_id not in latest_reset_by_node and c_at:
                    latest_reset_by_node[r_id] = _ensure_utc(c_at)

            stmt_llm = select(AuditLog).where(
                AuditLog.action == "LLM_INFERENCE",
                AuditLog.resource_id.in_(all_node_names),
                AuditLog.created_at >= since_dt
            ).order_by(AuditLog.created_at.asc())
            llm_logs = (await session.execute(stmt_llm)).scalars().all()

            stmt_infra = select(AuditLog).where(
                AuditLog.action.in_(["EMBEDDING_INFERENCE", "RERANK_INFERENCE"]),
                AuditLog.resource_id.in_(infra_names),
                AuditLog.created_at >= since_dt
            ).order_by(AuditLog.created_at.asc())
            infra_logs = (await session.execute(stmt_infra)).scalars().all()
    except Exception as e:
        logger.error(f"[SystemMonitoring] 读取 sys_audit_logs 失败: {e}")
        raise HTTPException(status_code=500, detail=f"【数据库异常】读取审计日志聚合指标失败: {e} (Fail-Fast)")

    logs_by_node: Dict[str, List[AuditLog]] = {name: [] for name in all_node_names + infra_names}
    for log in list(llm_logs) + list(infra_logs):
        r_id = log.resource_id
        if r_id in logs_by_node:
            reset_dt = latest_reset_by_node.get(r_id)
            if reset_dt is None or (log.created_at and _ensure_utc(log.created_at) >= reset_dt):
                logs_by_node[r_id].append(log)

    channel_list = []
    total_cluster_calls = 0
    total_cluster_success = 0
    all_cluster_ttfts: List[int] = []

    for node in all_nodes:
        agg = _aggregate_logs_metrics(logs_by_node.get(node.name, []))
        total_cluster_calls += agg["calls_count"]
        total_cluster_success += agg["success_count"]
        all_cluster_ttfts.extend(agg["ttfts"])

        node_provider = node.provider or derive_provider_from_url(node.base_url)
        lease_info = get_cached_lease_info(node_provider, "model:llm")

        channel_list.append({
            "name": node.name,
            "model": node.model,
            "provider": node_provider,
            "base_url": node.base_url,
            "masked_key": lease_info["masked_display"],
            "lease_expires_at": lease_info["expires_at"],
            "backend": node.backend,
            "role": "PRIMARY" if node.name == cfg.primary.name else "FALLBACK",
            "weight": node.weight,
            "state": tracker.get_node_state(node.name),
            "is_available": tracker.is_node_available(node.name),
            "sla_percent": agg["sla_percent"],
            "avg_latency_ms": agg["avg_lat"],
            "p95_latency_ms": agg["p95_lat"],
            "ttft_ms": agg["avg_ttft"],
            "total_calls": agg["calls_count"],
            "recent_bars": agg["recent_bars"],
        })

    infra_channels = []
    for comp_name, comp_model, comp_url, comp_scope, comp_backend in infra_defs:
        comp_logs = logs_by_node.get(comp_name, [])
        agg = _aggregate_logs_metrics(comp_logs)
        total_cluster_calls += agg["calls_count"]
        total_cluster_success += agg["success_count"]

        comp_provider = derive_provider_from_url(comp_url)
        comp_lease = get_cached_lease_info(comp_provider, comp_scope)

        # 根据真实最近调用流水动态反映基建组件健康态，杜绝全部失败时仍绿灯显示 CLOSED
        recent_tail = comp_logs[-3:]
        if len(recent_tail) >= 3 and all(l.status != "SUCCESS" for l in recent_tail):
            infra_state = "OPEN"
            infra_avail = False
        elif agg["calls_count"] > 0 and agg["sla_percent"] < 95.0:
            infra_state = "HALF_OPEN"
            infra_avail = True
        else:
            infra_state = "CLOSED"
            infra_avail = True

        infra_channels.append({
            "name": comp_name,
            "model": comp_model,
            "provider": comp_provider,
            "base_url": comp_url,
            "masked_key": comp_lease["masked_display"],
            "lease_expires_at": comp_lease["expires_at"],
            "backend": comp_backend,
            "role": "INFRA",
            "weight": 100,
            "state": infra_state,
            "is_available": infra_avail,
            "sla_percent": agg["sla_percent"],
            "avg_latency_ms": agg["avg_lat"],
            "p95_latency_ms": agg["p95_lat"],
            "ttft_ms": 0,
            "total_calls": agg["calls_count"],
            "recent_bars": agg["recent_bars"],
        })

    providers_found: List[str] = []
    for c in channel_list:
        p = c["provider"].upper()
        if p not in providers_found:
            providers_found.append(p)

    vendor_groups = [
        {
            "vendor_id": p,
            "vendor_name": _VENDOR_TITLES.get(p, f"{p} (模型服务池)"),
            "channels": [c for c in channel_list if c["provider"].upper() == p],
        }
        for p in providers_found
    ]
    vendor_groups.append({
        "vendor_id": "INFRA",
        "vendor_name": _VENDOR_TITLES["INFRA"],
        "channels": infra_channels,
    })

    overall_sla = round((total_cluster_success / total_cluster_calls * 100.0), 1) if total_cluster_calls > 0 else 100.0
    overall_ttft = int(sum(all_cluster_ttfts) / len(all_cluster_ttfts)) if all_cluster_ttfts else 0

    return {
        "status": "ok",
        "range": range,
        "updated_at": datetime.now().strftime("%Y/%m/%d %H:%M:%S"),
        "overall_sla": overall_sla,
        "overall_avg_ttft": overall_ttft,
        "total_channels": len(channel_list) + len(infra_channels),
        "groups": [g for g in vendor_groups if g["channels"]],
    }


@router.get("/llm/channels/{node_name}/audits")
async def get_channel_audit_logs(
    node_name: str,
    limit: int = 30,
    user: UserContext = Depends(require_permission("kb:retrieve"))
):
    """查询指定模型或基建节点的近 30 次真实审计调用流水"""
    try:
        async with AsyncSessionLocal() as session:
            stmt = select(AuditLog).where(
                AuditLog.action.in_(["LLM_INFERENCE", "LLM_FAILOVER", "EMBEDDING_INFERENCE", "RERANK_INFERENCE", "LLM_CIRCUIT_RESET"]),
                or_(
                    AuditLog.resource_id == node_name,
                    AuditLog.resource_id.like(f"%{node_name}%")
                )
            ).order_by(desc(AuditLog.created_at)).limit(limit)

            logs = (await session.execute(stmt)).scalars().all()

            return {
                "node_name": node_name,
                "count": len(logs),
                "items": [
                    {
                        "id": str(log.id),
                        "request_id": log.request_id,
                        "action": log.action,
                        "resource_id": log.resource_id,
                        "duration_ms": log.duration_ms,
                        "status": log.status,
                        "error_msg": log.error_msg,
                        "details": log.details,
                        "created_at": _format_local_time(log.created_at, "%Y-%m-%d %H:%M:%S") if log.created_at else ""
                    }
                    for log in logs
                ]
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"【数据库异常】读取审计流水失败: {str(e)} (Fail-Fast)")


@router.post("/llm/channels/{node_name}/reset")
async def reset_channel_circuit_breaker(
    node_name: str,
    user: UserContext = Depends(require_permission("system:admin"))
):
    """管理员手动重置指定模型节点的熔断状态至 CLOSED 健康 (持久化审计 + 跨 Worker 一致性)"""
    tracker = llm_gateway.health_tracker
    now_ts = time.time()
    tracker.reset_node(node_name, reset_timestamp=now_ts)

    try:
        async with AsyncSessionLocal() as session:
            log_entry = AuditLog(
                id=uuid.uuid4(),
                request_id=get_request_id(),
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                action="LLM_CIRCUIT_RESET",
                resource_id=node_name,
                duration_ms=0,
                status="SUCCESS",
                details={
                    "operator_username": user.username,
                    "operator_roles": user.roles,
                    "reset_timestamp": now_ts
                }
            )
            session.add(log_entry)
            await session.commit()
    except Exception as e:
        logger.error(f"持久化 LLM_CIRCUIT_RESET 审计日志失败: {e}")
        raise HTTPException(status_code=500, detail=f"【数据库异常】持久化熔断重置日志失败: {e} (Fail-Fast)")

    return {
        "node_name": node_name,
        "state": tracker.get_node_state(node_name),
        "message": f"节点 [{node_name}] 的熔断状态与指标统计已重置为 CLOSED，基线已持久化同步。"
    }
