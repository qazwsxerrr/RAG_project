import asyncio
import json
from typing import AsyncGenerator, Optional
from fastapi import APIRouter, HTTPException, Header, Query
from fastapi.responses import StreamingResponse
from pathlib import Path
from backend.app.core.oss import AliyunOSSService
from backend.app.schemas.pipeline import (
    PipelineSnapshotResponse,
    ReviewSubmitRequest
)
from backend.app.services.pipeline.state_machine import pipeline_manager

router = APIRouter(prefix="/documents", tags=["流水线状态监控与人机协同"])


def _sse_business_payload(event: dict) -> str:
    """SSE 的 event/id 已在协议字段里。data 行只放业务载荷，避免步骤号被包在外壳中。"""
    payload = event.get("data")
    if event.get("event") and isinstance(payload, dict):
        return json.dumps(payload, default=str)
    return json.dumps(event, default=str)

@router.get("/{doc_id}/status", response_model=PipelineSnapshotResponse)
async def get_pipeline_status(doc_id: str):
    """
    获取流水线静态快照（用于页面加载与 Img 04 的“重新连接”按钮恢复机制）
    """
    instance = pipeline_manager.get_instance(doc_id)
    if not instance:
        raise HTTPException(status_code=404, detail=f"未找到文档 ID 为 {doc_id} 的入库流水线")
    return instance.to_snapshot()

@router.get("/{doc_id}/events")
async def stream_pipeline_events(
    doc_id: str,
    last_event_id: Optional[str] = Header(None, alias="Last-Event-ID"),
    from_event_id: Optional[int] = Query(None, description="支持 Query 参数重传历史事件")
):
    """
    SSE 长连接推流接口（实时同步 11 节点流转、审核挂起信号）
    支持标准 Last-Event-ID 机制进行断网重连与历史事件回放
    """
    instance = pipeline_manager.get_instance(doc_id)
    if not instance:
        raise HTTPException(status_code=404, detail=f"未找到文档 ID 为 {doc_id} 的入库流水线")

    event_queue: asyncio.Queue = asyncio.Queue()
    instance.event_subscribers.append(event_queue)

    # 确定断线重连起始 ID
    since_id = 0
    if last_event_id and last_event_id.isdigit():
        since_id = int(last_event_id)
    elif from_event_id is not None:
        since_id = from_event_id

    async def event_generator() -> AsyncGenerator[str, None]:
        # 若为全新连接，先下发当前状态快照
        if since_id == 0:
            init_data = json.dumps(instance.to_snapshot().model_dump(), default=str)
            yield f"id: 0\nevent: initial_state\ndata: {init_data}\n\n"
        else:
            # 回放历史断线事件
            for past_evt in instance.event_history:
                past_id = int(past_evt.get("id", 0))
                if past_id > since_id:
                    p_name = past_evt.get("event", "message")
                    p_data = _sse_business_payload(past_evt)
                    yield f"id: {past_id}\nevent: {p_name}\ndata: {p_data}\n\n"

        # 若流水线已完成或终止，回放完毕后直接安全关闭推流，避免连接长期挂起
        if instance.overall_status in ["completed", "failed"]:
            return

        try:
            while True:
                # 等待流水线执行产生的广播事件
                event = await event_queue.get()
                event_id = event.get("id", "")
                event_name = event.get("event", "message")
                data_str = _sse_business_payload(event)
                yield f"id: {event_id}\nevent: {event_name}\ndata: {data_str}\n\n"

                # 若整体已结束（完成或失败），退出生成器
                if event_name in ["pipeline_completed", "pipeline_failed"]:
                    break
        except asyncio.CancelledError:
            pass
        finally:
            if event_queue in instance.event_subscribers:
                instance.event_subscribers.remove(event_queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )

@router.post("/{doc_id}/review")
async def submit_pipeline_review(doc_id: str, request: ReviewSubmitRequest):
    """
    提交人工审核结果并唤醒流水线（对应 Img 07 的 ReviewDialog 确认并继续）
    """
    instance = pipeline_manager.get_instance(doc_id)
    if not instance:
        raise HTTPException(status_code=404, detail=f"未找到文档 ID 为 {doc_id} 的入库流水线")

    if instance.overall_status != "pending_review":
        raise HTTPException(status_code=400, detail="当前流水线并未处于待审核状态")

    # 更新人工审核的文本描述和备注
    update_map = {item.item_id: item for item in request.items}
    for rev in instance.pending_reviews:
        if rev.item_id in update_map:
            up = update_map[rev.item_id]
            rev.user_description = up.user_description
            rev.remark = up.remark
            rev.status = "modified" if up.user_description != rev.vlm_description else "approved"

    # 释放挂起 Future，唤醒后台继续执行 Step 8（表格回填）
    if instance.review_future and not instance.review_future.done():
        instance.review_future.set_result(True)

    await instance.broadcast_event("review_completed", {
        "doc_id": doc_id,
        "reviewed_items_count": len(request.items)
    })

    return {
        "status": "success",
        "message": "人工审核已确认，流水线已继续执行下一步（表格回填）",
        "doc_id": doc_id
    }


@router.get("/{doc_id}/review-items/{item_id}/asset")
async def get_review_item_asset(doc_id: str, item_id: str):
    """
    人工审核多模态资产 (配图/表格切图) 安全流式反向代理
    完全对齐智能问答查看切片 (/chunks/{chunk_id}/asset) 的实现机制，
    由后端使用凭证从私有 OSS 内存流式拉取并回传浏览器，彻底根除私有桶 403 AccessDenied 与签名过期问题
    """
    raw_oss_url = None

    # 从当前活跃的流水线实例中精准匹配待审核项
    inst = pipeline_manager.get_instance(doc_id)
    if not inst:
        raise HTTPException(status_code=404, detail=f"未找到文档 {doc_id} 的活跃流水线实例")

    for r in inst.pending_reviews:
        if r.item_id == item_id:
            raw_oss_url = getattr(r, "raw_oss_url", None) or r.asset_url
            break

    if not raw_oss_url or not raw_oss_url.startswith("http"):
        raise HTTPException(status_code=404, detail="未找到该待审核图元或资产无关联云端切图")

    oss_service = AliyunOSSService()
    try:
        stream_gen, content_length, _ = oss_service.get_object_stream(raw_oss_url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"拉取 OSS 资产流失败: {str(e)}")

    ext = Path(raw_oss_url.split("?")[0]).suffix.lower()
    if ext in [".jpg", ".jpeg"]:
        media_type = "image/jpeg"
    elif ext == ".png":
        media_type = "image/png"
    elif ext == ".webp":
        media_type = "image/webp"
    else:
        media_type = "application/octet-stream"

    headers = {
        "Cache-Control": "public, max-age=86400",
        "Content-Disposition": "inline"
    }
    if content_length:
        headers["Content-Length"] = str(content_length)

    return StreamingResponse(stream_gen, media_type=media_type, headers=headers)

