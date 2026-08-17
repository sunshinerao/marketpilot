from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from marketpilot.domain.attribution import AttributionTask
from marketpilot.services.stream_attribution_schemas import (
    AttributionReviewInput,
    AttributionReviewListOutput,
    AttributionReviewOutput,
    AttributionSignalInput,
    AttributionTaskListOutput,
    CounterfactualReplayOutput,
    DeliveryAuditListOutput,
)
from marketpilot.services.stream_attribution_service import (
    AttributionWorkflowError,
    StreamAttributionService,
)
from marketpilot.services.stream_attribution_store import StreamCursorError


def create_stream_attribution_router(service: StreamAttributionService) -> APIRouter:
    """Build the Phase 4 router without importing or mutating the application root."""

    router = APIRouter(prefix="/v1", tags=["alert-stream", "attribution"])

    @router.get("/alerts/stream/deliveries", response_model=DeliveryAuditListOutput)
    def delivery_audit(stream_event_id: str | None = None) -> DeliveryAuditListOutput:
        return DeliveryAuditListOutput(
            deliveries=service.store.deliveries(stream_event_id=stream_event_id)
        )

    @router.get("/alerts/stream")
    def alert_stream(
        request: Request,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        connection_id: str | None = Header(default=None, alias="X-Connection-ID"),
        heartbeat_seconds: float = Query(default=15.0, ge=1.0, le=60.0),
        max_frames: int | None = Query(default=None, ge=1, le=100, include_in_schema=False),
    ) -> StreamingResponse:
        try:
            service.validate_cursor(last_event_id)
        except StreamCursorError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        resolved_connection_id = connection_id or str(uuid4())
        return StreamingResponse(
            service.stream_frames(
                last_event_id=last_event_id,
                connection_id=resolved_connection_id,
                heartbeat_seconds=heartbeat_seconds,
                max_frames=max_frames,
                disconnected=request.is_disconnected,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "X-Connection-ID": resolved_connection_id,
            },
        )

    @router.post(
        "/attribution/signals",
        response_model=AttributionTask,
        status_code=status.HTTP_201_CREATED,
    )
    def create_task(request: AttributionSignalInput) -> AttributionTask:
        return service.create_attribution_task(request.signal)

    @router.get("/attribution/tasks", response_model=AttributionTaskListOutput)
    def tasks() -> AttributionTaskListOutput:
        return AttributionTaskListOutput(tasks=service.tasks())

    @router.get("/attribution/tasks/{task_id}", response_model=AttributionTask)
    def task(task_id: str) -> AttributionTask:
        try:
            return service.task(task_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="attribution task not found",
            ) from exc

    @router.get(
        "/attribution/tasks/{task_id}/reviews",
        response_model=AttributionReviewListOutput,
    )
    def reviews(task_id: str) -> AttributionReviewListOutput:
        try:
            return AttributionReviewListOutput(reviews=service.reviews(task_id))
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="attribution task not found",
            ) from exc

    @router.post(
        "/attribution/tasks/{task_id}/reviews",
        response_model=AttributionReviewOutput,
    )
    def review(task_id: str, request: AttributionReviewInput) -> AttributionReviewOutput:
        try:
            recorded, updated = service.review(
                task_id=task_id,
                status=request.status,
                reviewer=request.reviewer,
                reviewed_at=request.reviewed_at,
                note=request.note,
                retain_as_reusable_sample=request.retain_as_reusable_sample,
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="attribution task not found",
            ) from exc
        except AttributionWorkflowError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        return AttributionReviewOutput(review=recorded, task=updated)

    @router.get(
        "/attribution/tasks/{task_id}/counterfactual-replay",
        response_model=CounterfactualReplayOutput,
    )
    def counterfactual_replay(task_id: str) -> CounterfactualReplayOutput:
        try:
            return CounterfactualReplayOutput.model_validate(service.counterfactual_replay(task_id))
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="attribution task not found",
            ) from exc

    return router
