"""FastAPI application: runway departure wake-separation validation."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .database import get_db, init_db
from .repository import load_batch, save_batch
from .schemas import (
    BatchResponse,
    HealthOut,
    InfeasibleOut,
    MatrixOut,
    RunwayReportOut,
    RunwayTrialOut,
    TrialResponse,
    ValidateResponse,
    WakeRequest,
    default_matrix_nested,
)
from .separation import ClosureWindow, InfeasibleInfo
from .service import nested_matrix, run_trial, run_validate
from .config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="跑道离场尾流间隔校验 API",
    description=(
        "提交按跑道分组的航班计划（可附带跑道临时关闭窗口、滚动容量约束与"
        "不可移动 locked 航班），校验相邻离场航班的尾流间隔违规、落入关闭窗口的"
        "航班与滚动容量超限，并支持在不改动原计划的前提下试排（向后级联延误，"
        "自动满足容量并避开关闭窗口；锁定航班保持计划时刻，无法满足时在跑道级"
        "报告首个不可行航班、冲突约束与所需最早时刻）。"
    ),
    version="1.3.0",
    lifespan=lifespan,
)


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    """Pydantic 之外可能抛出的领域校验错误统一为 400。"""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/", tags=["meta"])
def root() -> dict:
    return {
        "service": "runway-wake-separation",
        "docs": "/docs",
        "health": "/health",
        "default_matrix": "/matrix/default",
        "endpoints": {
            "validate": "POST /api/v1/validate",
            "trial": "POST /api/v1/trial",
            "batch": "GET /api/v1/batches/{batch_id}",
        },
    }


@app.get("/health", response_model=HealthOut, tags=["meta"])
def health() -> HealthOut:
    return HealthOut(status="ok")


@app.get("/matrix/default", response_model=MatrixOut, tags=["meta"])
def default_matrix() -> MatrixOut:
    return MatrixOut(matrix=default_matrix_nested())


def _window_out(window: ClosureWindow) -> dict:
    return {"start": window.start, "end": window.end}


def _capacity_out(capacity) -> dict:
    return {
        "window_seconds": capacity.window_seconds,
        "max_departures": capacity.max_departures,
    }


def _locked_used_by_runway(req: WakeRequest) -> dict[str, bool]:
    """Whether any flight of the runway carried the ``locked`` key at all —
    this (not the value) decides if locked fields join the response shape."""
    return {
        rw.runway: any(f.locked is not None for f in rw.flights)
        for rw in req.runways
    }


def _infeasible_out(info: InfeasibleInfo) -> InfeasibleOut:
    return InfeasibleOut(
        flight_no=info.flight_no,
        wake=info.wake,
        position=info.position,
        scheduled_off=info.scheduled_off,
        earliest_required_off=info.earliest_required_off,
        constraints=list(info.constraints),
        triggered_by=info.triggered_by,
        closure_window=(
            _window_out(info.closure_window) if info.closure_window else None
        ),
        capacity_anchor=info.capacity_anchor,
        capacity_window=(
            {
                "start": info.capacity_window_start,
                "end": info.capacity_window_end,
            }
            if info.capacity_window_start is not None
            else None
        ),
    )


@app.post("/api/v1/validate", response_model=ValidateResponse, tags=["wake"])
def validate(
    req: WakeRequest, db: sqlite3.Connection = Depends(get_db)
) -> ValidateResponse:
    """按（计划时刻, 航班号）稳定排序，列出各跑道相邻航班的间隔违规、
    落入关闭窗口的航班及其命中窗口，以及滚动容量约束的超限航班、
    触发窗口与最早合规时刻。"""
    (
        payloads,
        matrix,
        windows,
        capacities,
        violations,
        conflicts,
        capacity_violations,
    ) = run_validate(req)
    batch_id = save_batch(
        db,
        mode="validate",
        matrix=matrix,
        runway_payloads=payloads,
        closures_by_runway=windows,
        capacities_by_runway=capacities,
        violations_by_runway=violations,
        closure_conflicts_by_runway=conflicts,
        capacity_violations_by_runway=capacity_violations,
    )

    locked_used = _locked_used_by_runway(req)
    runway_reports: list[RunwayReportOut] = []
    for runway, flights in payloads.items():
        vlist = violations[runway]
        clist = conflicts[runway]
        capacity = capacities.get(runway)
        cvlist = capacity_violations[runway]
        runway_reports.append(
            RunwayReportOut(
                runway=runway,
                order=[f.flight_no for f in flights],
                total=len(flights),
                closures=[_window_out(w) for w in windows[runway]],
                violation_count=len(vlist),
                violations=[
                    {
                        "leader": v.leader_no,
                        "follower": v.follower_no,
                        "leader_wake": v.leader_wake,
                        "follower_wake": v.follower_wake,
                        "leader_scheduled_off": v.leader_time,
                        "follower_scheduled_off": v.follower_time,
                        "required_seconds": v.required_seconds,
                        "actual_seconds": v.actual_seconds,
                        "shortfall_seconds": v.shortfall_seconds,
                        "leader_position": v.leader_position,
                        "follower_position": v.follower_position,
                    }
                    for v in vlist
                ],
                closure_conflict_count=len(clist),
                closure_conflicts=[
                    {
                        "flight_no": c.flight_no,
                        "wake": c.wake,
                        "scheduled_off": c.scheduled_off,
                        "position": c.position,
                        "window_index": c.window_index,
                        "window_start": c.window_start,
                        "window_end": c.window_end,
                    }
                    for c in clist
                ],
                has_capacity=capacity is not None,
                capacity=_capacity_out(capacity) if capacity is not None else None,
                capacity_violation_count=len(cvlist),
                capacity_violations=[
                    {
                        "flight_no": cv.flight_no,
                        "wake": cv.wake,
                        "scheduled_off": cv.scheduled_off,
                        "position": cv.position,
                        "window_seconds": cv.window_seconds,
                        "max_departures": cv.max_departures,
                        "departures_in_window": cv.departures_in_window,
                        "anchor_flight_no": cv.anchor_flight_no,
                        "anchor_position": cv.anchor_position,
                        "window_start": cv.anchor_off,
                        "window_end": cv.window_end,
                        "earliest_compliant_off": cv.earliest_compliant_off,
                    }
                    for cv in cvlist
                ],
                has_locked=locked_used[runway],
                locked_flights=[f.flight_no for f in flights if f.locked],
            )
        )

    return ValidateResponse(
        batch_id=batch_id,
        matrix=nested_matrix(matrix),
        total_runways=len(payloads),
        total_flights=sum(len(f) for f in payloads.values()),
        total_violations=sum(len(v) for v in violations.values()),
        total_closure_conflicts=sum(len(c) for c in conflicts.values()),
        has_capacity=bool(capacities),
        total_capacity_violations=sum(
            len(cv) for cv in capacity_violations.values()
        ),
        runways=runway_reports,
    )


@app.post("/api/v1/trial", response_model=TrialResponse, tags=["wake"])
def trial(
    req: WakeRequest, db: sqlite3.Connection = Depends(get_db)
) -> TrialResponse:
    """保持原排序逐项推迟到最早安全时刻（满足尾流间隔、滚动容量约束且
    避开关闭窗口）；延误向后级联，原计划不变。locked=true 的航班不可移动：
    若约束要求将其推迟，则该跑道在首个不可行航班处停止，跑道级返回
    infeasible（冲突约束与所需最早时刻），items 只含其前的可行前缀。"""
    payloads, matrix, windows, capacities, trials = run_trial(req)
    batch_id = save_batch(
        db,
        mode="trial",
        matrix=matrix,
        runway_payloads=payloads,
        closures_by_runway=windows,
        capacities_by_runway=capacities,
        trial_by_runway=trials,
    )

    locked_used = _locked_used_by_runway(req)
    runway_trials: list[RunwayTrialOut] = []
    total_delayed = 0
    total_delay = 0
    for runway, result in trials.items():
        items = result.items
        delayed = [i for i in items if i.delay_seconds > 0]
        rw_delay = sum(i.delay_seconds for i in items)
        total_delayed += len(delayed)
        total_delay += rw_delay
        capacity = capacities.get(runway)
        has_locked = locked_used[runway]
        runway_trials.append(
            RunwayTrialOut(
                runway=runway,
                order=[i.flight_no for i in items],
                total=len(items),
                closures=[_window_out(w) for w in windows[runway]],
                delayed_count=len(delayed),
                total_delay_seconds=rw_delay,
                max_delay_seconds=max((i.delay_seconds for i in items), default=0),
                has_capacity=capacity is not None,
                capacity=_capacity_out(capacity) if capacity is not None else None,
                has_locked=has_locked,
                locked_flights=[f.flight_no for f in payloads[runway] if f.locked],
                infeasible=(
                    _infeasible_out(result.infeasible)
                    if result.infeasible is not None
                    else None
                ),
                items=[
                    {
                        "position": i.position,
                        "flight_no": i.flight_no,
                        "wake": i.wake,
                        "scheduled_off": i.original_time,
                        "adjusted_off": i.adjusted_time,
                        "delay_seconds": i.delay_seconds,
                        "triggered_by": i.triggered_by,
                        "closure_hit": i.closure_hit,
                        "closure_window": (
                            _window_out(i.closure_window) if i.closure_window else None
                        ),
                        "capacity_hit": i.capacity_hit,
                        "capacity_window_seconds": i.capacity_window_seconds,
                        "capacity_max_departures": i.capacity_max_departures,
                        "capacity_anchor": i.capacity_anchor,
                        "capacity_window": (
                            {
                                "start": i.capacity_window_start,
                                "end": i.capacity_window_end,
                            }
                            if i.capacity_window_start is not None
                            else None
                        ),
                        "has_capacity": capacity is not None,
                        "has_locked": has_locked,
                        "locked": i.locked,
                    }
                    for i in items
                ],
            )
        )

    return TrialResponse(
        batch_id=batch_id,
        matrix=nested_matrix(matrix),
        total_runways=len(trials),
        total_flights=sum(len(r.items) for r in trials.values()),
        total_delayed=total_delayed,
        total_delay_seconds=total_delay,
        runways=runway_trials,
    )


@app.get(
    "/api/v1/batches/{batch_id}",
    response_model=BatchResponse,
    tags=["wake"],
)
def get_batch(
    batch_id: str, db: sqlite3.Connection = Depends(get_db)
) -> BatchResponse:
    """读取已保存的批次（validate 或 trial 均可）。"""
    data = load_batch(db, batch_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"批次不存在: {batch_id}")
    return BatchResponse(**data)
