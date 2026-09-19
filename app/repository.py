"""Persistence helpers for batches, flights, violations and trial items."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from .separation import (
    Capacity,
    CapacityViolation,
    ClosureConflict,
    ClosureWindow,
    Flight,
    TrialResult,
    Violation,
)
from .timeutils import ensure_utc


def new_batch_id() -> str:
    return uuid4().hex


def _iso(dt: datetime) -> str:
    return ensure_utc(dt).isoformat()


def _dt(stored: str) -> datetime:
    """Parse a stored ISO string back to a datetime so the batch endpoint
    serialises it exactly like the POST responses (UTC, ``Z`` suffix)."""
    return datetime.fromisoformat(stored)


def save_batch(
    conn: sqlite3.Connection,
    *,
    mode: str,
    matrix: dict[tuple[str, str], int],
    runway_payloads: dict[str, list[Flight]],
    closures_by_runway: dict[str, list[ClosureWindow]] | None = None,
    capacities_by_runway: dict[str, Capacity] | None = None,
    violations_by_runway: dict[str, list[Violation]] | None = None,
    closure_conflicts_by_runway: dict[str, list[ClosureConflict]] | None = None,
    capacity_violations_by_runway: dict[str, list[CapacityViolation]] | None = None,
    trial_by_runway: dict[str, TrialResult] | None = None,
) -> str:
    batch_id = new_batch_id()
    created_at = datetime.now(timezone.utc).isoformat()
    matrix_json = json.dumps(
        {f"{leader}->{follower}": seconds for (leader, follower), seconds in matrix.items()},
        sort_keys=True,
    )
    conn.execute(
        "INSERT INTO batches (batch_id, mode, created_at, matrix_json) VALUES (?, ?, ?, ?)",
        (batch_id, mode, created_at, matrix_json),
    )

    for runway, flights in runway_payloads.items():
        for position, flight in enumerate(flights, start=1):
            conn.execute(
                "INSERT INTO flights (batch_id, runway, position, flight_no, scheduled_off, wake,"
                " locked) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    runway,
                    position,
                    flight.flight_no,
                    _iso(flight.scheduled_off),
                    flight.wake,
                    # None -> NULL: "locked key absent" stays distinguishable
                    # from an explicit false on batch replay.
                    None if flight.locked is None else int(flight.locked),
                ),
            )

    for runway, windows in (closures_by_runway or {}).items():
        for index, window in enumerate(windows, start=1):
            conn.execute(
                "INSERT INTO closures (batch_id, runway, window_index, start_utc, end_utc)"
                " VALUES (?, ?, ?, ?, ?)",
                (batch_id, runway, index, _iso(window.start), _iso(window.end)),
            )

    for runway, capacity in (capacities_by_runway or {}).items():
        conn.execute(
            "INSERT INTO capacities (batch_id, runway, window_seconds, max_departures)"
            " VALUES (?, ?, ?, ?)",
            (
                batch_id,
                runway,
                capacity.window_seconds,
                capacity.max_departures,
            ),
        )

    for runway, violations in (violations_by_runway or {}).items():
        for v in violations:
            conn.execute(
                "INSERT INTO violations (batch_id, runway, leader_position, follower_position,"
                " leader_no, follower_no, leader_wake, follower_wake, leader_time, follower_time,"
                " required_seconds, actual_seconds, shortfall_seconds)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    runway,
                    v.leader_position,
                    v.follower_position,
                    v.leader_no,
                    v.follower_no,
                    v.leader_wake,
                    v.follower_wake,
                    _iso(v.leader_time),
                    _iso(v.follower_time),
                    v.required_seconds,
                    v.actual_seconds,
                    v.shortfall_seconds,
                ),
            )

    for runway, conflicts in (closure_conflicts_by_runway or {}).items():
        for c in conflicts:
            conn.execute(
                "INSERT INTO closure_conflicts (batch_id, runway, position, flight_no, wake,"
                " scheduled_off, window_index, window_start, window_end)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    runway,
                    c.position,
                    c.flight_no,
                    c.wake,
                    _iso(c.scheduled_off),
                    c.window_index,
                    _iso(c.window_start),
                    _iso(c.window_end),
                ),
            )

    for runway, cap_violations in (capacity_violations_by_runway or {}).items():
        for cv in cap_violations:
            conn.execute(
                "INSERT INTO capacity_violations (batch_id, runway, position, flight_no, wake,"
                " scheduled_off, window_seconds, max_departures, departures_in_window,"
                " anchor_flight_no, anchor_position, window_start, window_end,"
                " earliest_compliant_off)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    runway,
                    cv.position,
                    cv.flight_no,
                    cv.wake,
                    _iso(cv.scheduled_off),
                    cv.window_seconds,
                    cv.max_departures,
                    cv.departures_in_window,
                    cv.anchor_flight_no,
                    cv.anchor_position,
                    _iso(cv.anchor_off),
                    _iso(cv.window_end),
                    _iso(cv.earliest_compliant_off),
                ),
            )

    for runway, result in (trial_by_runway or {}).items():
        for item in result.items:
            conn.execute(
                "INSERT INTO trial_items (batch_id, runway, position, flight_no, wake,"
                " scheduled_off, adjusted_off, delay_seconds, triggered_by,"
                " closure_hit, closure_window_start, closure_window_end,"
                " capacity_hit, capacity_window_seconds, capacity_max_departures,"
                " capacity_anchor, capacity_window_start, capacity_window_end, locked)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    runway,
                    item.position,
                    item.flight_no,
                    item.wake,
                    _iso(item.original_time),
                    _iso(item.adjusted_time),
                    item.delay_seconds,
                    item.triggered_by,
                    int(item.closure_hit),
                    _iso(item.closure_window.start) if item.closure_window else None,
                    _iso(item.closure_window.end) if item.closure_window else None,
                    int(item.capacity_hit),
                    item.capacity_window_seconds,
                    item.capacity_max_departures,
                    item.capacity_anchor,
                    _iso(item.capacity_window_start)
                    if item.capacity_window_start
                    else None,
                    _iso(item.capacity_window_end) if item.capacity_window_end else None,
                    int(item.locked),
                ),
            )
        infeasible = result.infeasible
        if infeasible is not None:
            conn.execute(
                "INSERT INTO trial_infeasible (batch_id, runway, position, flight_no, wake,"
                " scheduled_off, earliest_required_off, constraints_json, triggered_by,"
                " closure_window_start, closure_window_end, capacity_anchor,"
                " capacity_window_start, capacity_window_end)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    runway,
                    infeasible.position,
                    infeasible.flight_no,
                    infeasible.wake,
                    _iso(infeasible.scheduled_off),
                    _iso(infeasible.earliest_required_off),
                    json.dumps(list(infeasible.constraints)),
                    infeasible.triggered_by,
                    _iso(infeasible.closure_window.start)
                    if infeasible.closure_window
                    else None,
                    _iso(infeasible.closure_window.end)
                    if infeasible.closure_window
                    else None,
                    infeasible.capacity_anchor,
                    _iso(infeasible.capacity_window_start)
                    if infeasible.capacity_window_start
                    else None,
                    _iso(infeasible.capacity_window_end)
                    if infeasible.capacity_window_end
                    else None,
                ),
            )

    return batch_id


def load_batch(conn: sqlite3.Connection, batch_id: str) -> dict | None:
    """Reconstruct a stored batch in the same shape as the POST responses."""
    row = conn.execute(
        "SELECT batch_id, mode, created_at, matrix_json FROM batches WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    if row is None:
        return None

    flat = {
        tuple(pair.split("->", 1)): seconds
        for pair, seconds in json.loads(row["matrix_json"]).items()
    }
    matrix: dict[str, dict[str, int]] = {}
    for (leader, follower), seconds in flat.items():
        matrix.setdefault(leader, {})[follower] = seconds

    flight_rows = conn.execute(
        "SELECT runway, position, flight_no, locked FROM flights"
        " WHERE batch_id = ? ORDER BY runway, position",
        (batch_id,),
    ).fetchall()
    order_by_runway: dict[str, list[str]] = {}
    locked_used_by_runway: dict[str, bool] = {}
    locked_flights_by_runway: dict[str, list[str]] = {}
    for fr in flight_rows:
        order_by_runway.setdefault(fr["runway"], []).append(fr["flight_no"])
        if fr["locked"] is not None:
            locked_used_by_runway[fr["runway"]] = True
            if fr["locked"]:
                locked_flights_by_runway.setdefault(fr["runway"], []).append(
                    fr["flight_no"]
                )

    closure_rows = conn.execute(
        "SELECT runway, window_index, start_utc, end_utc FROM closures"
        " WHERE batch_id = ? ORDER BY runway, window_index",
        (batch_id,),
    ).fetchall()
    closures_by_runway: dict[str, list[dict]] = {}
    for cr in closure_rows:
        closures_by_runway.setdefault(cr["runway"], []).append(
            {"start": _dt(cr["start_utc"]), "end": _dt(cr["end_utc"])}
        )

    capacity_rows = conn.execute(
        "SELECT runway, window_seconds, max_departures FROM capacities"
        " WHERE batch_id = ?",
        (batch_id,),
    ).fetchall()
    capacities_by_runway: dict[str, dict] = {}
    for cr in capacity_rows:
        capacities_by_runway[cr["runway"]] = {
            "window_seconds": cr["window_seconds"],
            "max_departures": cr["max_departures"],
        }
    batch_has_capacity = bool(capacities_by_runway)

    if row["mode"] == "validate":
        vrows = conn.execute(
            "SELECT * FROM violations WHERE batch_id = ? ORDER BY runway, leader_position",
            (batch_id,),
        ).fetchall()
        runways = []
        violations_by_runway: dict[str, list[dict]] = {}
        for vr in vrows:
            violations_by_runway.setdefault(vr["runway"], []).append(
                {
                    "leader": vr["leader_no"],
                    "follower": vr["follower_no"],
                    "leader_wake": vr["leader_wake"],
                    "follower_wake": vr["follower_wake"],
                    "leader_scheduled_off": _dt(vr["leader_time"]),
                    "follower_scheduled_off": _dt(vr["follower_time"]),
                    "required_seconds": vr["required_seconds"],
                    "actual_seconds": vr["actual_seconds"],
                    "shortfall_seconds": vr["shortfall_seconds"],
                    "leader_position": vr["leader_position"],
                    "follower_position": vr["follower_position"],
                }
            )
        crows = conn.execute(
            "SELECT * FROM closure_conflicts WHERE batch_id = ? ORDER BY runway, position",
            (batch_id,),
        ).fetchall()
        conflicts_by_runway: dict[str, list[dict]] = {}
        for cr in crows:
            conflicts_by_runway.setdefault(cr["runway"], []).append(
                {
                    "flight_no": cr["flight_no"],
                    "wake": cr["wake"],
                    "scheduled_off": _dt(cr["scheduled_off"]),
                    "position": cr["position"],
                    "window_index": cr["window_index"],
                    "window_start": _dt(cr["window_start"]),
                    "window_end": _dt(cr["window_end"]),
                }
            )
        cap_vrows = conn.execute(
            "SELECT * FROM capacity_violations WHERE batch_id = ?"
            " ORDER BY runway, position",
            (batch_id,),
        ).fetchall()
        cap_violations_by_runway: dict[str, list[dict]] = {}
        for cr in cap_vrows:
            cap_violations_by_runway.setdefault(cr["runway"], []).append(
                {
                    "flight_no": cr["flight_no"],
                    "wake": cr["wake"],
                    "scheduled_off": _dt(cr["scheduled_off"]),
                    "position": cr["position"],
                    "window_seconds": cr["window_seconds"],
                    "max_departures": cr["max_departures"],
                    "departures_in_window": cr["departures_in_window"],
                    "anchor_flight_no": cr["anchor_flight_no"],
                    "anchor_position": cr["anchor_position"],
                    "window_start": _dt(cr["window_start"]),
                    "window_end": _dt(cr["window_end"]),
                    "earliest_compliant_off": _dt(cr["earliest_compliant_off"]),
                }
            )
        for runway in order_by_runway:
            violations = violations_by_runway.get(runway, [])
            conflicts = conflicts_by_runway.get(runway, [])
            report = {
                "runway": runway,
                "order": order_by_runway[runway],
                "total": len(order_by_runway[runway]),
                "closures": closures_by_runway.get(runway, []),
                "violation_count": len(violations),
                "violations": violations,
                "closure_conflict_count": len(conflicts),
                "closure_conflicts": conflicts,
            }
            if locked_used_by_runway.get(runway):
                report["locked_flights"] = locked_flights_by_runway.get(runway, [])
            if runway in capacities_by_runway:
                cap_violations = cap_violations_by_runway.get(runway, [])
                report["capacity"] = capacities_by_runway[runway]
                report["capacity_violation_count"] = len(cap_violations)
                report["capacity_violations"] = cap_violations
            runways.append(report)
        result = {
            "batch_id": row["batch_id"],
            "mode": "validate",
            "created_at": row["created_at"],
            "matrix": matrix,
            "total_runways": len(runways),
            "total_flights": sum(len(o) for o in order_by_runway.values()),
            "total_violations": sum(len(v) for v in violations_by_runway.values()),
            "total_closure_conflicts": sum(
                len(c) for c in conflicts_by_runway.values()
            ),
            "runways": runways,
        }
        if batch_has_capacity:
            result["total_capacity_violations"] = sum(
                len(v) for v in cap_violations_by_runway.values()
            )
        return result

    # trial
    trows = conn.execute(
        "SELECT * FROM trial_items WHERE batch_id = ? ORDER BY runway, position",
        (batch_id,),
    ).fetchall()
    items_by_runway: dict[str, list[dict]] = {}
    for tr in trows:
        closure_window = None
        if tr["closure_window_start"] is not None:
            closure_window = {
                "start": _dt(tr["closure_window_start"]),
                "end": _dt(tr["closure_window_end"]),
            }
        item = {
            "position": tr["position"],
            "flight_no": tr["flight_no"],
            "wake": tr["wake"],
            "scheduled_off": _dt(tr["scheduled_off"]),
            "adjusted_off": _dt(tr["adjusted_off"]),
            "delay_seconds": tr["delay_seconds"],
            "triggered_by": tr["triggered_by"],
            "closure_hit": bool(tr["closure_hit"]),
            "closure_window": closure_window,
        }
        runway_has_locked = locked_used_by_runway.get(tr["runway"], False)
        if runway_has_locked:
            item["locked"] = bool(tr["locked"])
        runway_has_capacity = tr["runway"] in capacities_by_runway
        if runway_has_capacity:
            capacity_window = None
            if tr["capacity_window_start"] is not None:
                capacity_window = {
                    "start": _dt(tr["capacity_window_start"]),
                    "end": _dt(tr["capacity_window_end"]),
                }
            item["capacity_hit"] = bool(tr["capacity_hit"])
            item["capacity_window_seconds"] = tr["capacity_window_seconds"]
            item["capacity_max_departures"] = tr["capacity_max_departures"]
            item["capacity_anchor"] = tr["capacity_anchor"]
            item["capacity_window"] = capacity_window
        items_by_runway.setdefault(tr["runway"], []).append(item)

    irows = conn.execute(
        "SELECT * FROM trial_infeasible WHERE batch_id = ? ORDER BY runway",
        (batch_id,),
    ).fetchall()
    infeasible_by_runway: dict[str, dict] = {}
    for ir in irows:
        constraints = json.loads(ir["constraints_json"])
        report_infeasible = {
            "flight_no": ir["flight_no"],
            "wake": ir["wake"],
            "position": ir["position"],
            "scheduled_off": _dt(ir["scheduled_off"]),
            "earliest_required_off": _dt(ir["earliest_required_off"]),
            "constraints": constraints,
        }
        # per-constraint detail keys mirror the POST serializer exactly
        if "wake" in constraints:
            report_infeasible["triggered_by"] = ir["triggered_by"]
        if "closure" in constraints:
            report_infeasible["closure_window"] = {
                "start": _dt(ir["closure_window_start"]),
                "end": _dt(ir["closure_window_end"]),
            }
        if "capacity" in constraints:
            report_infeasible["capacity_anchor"] = ir["capacity_anchor"]
            report_infeasible["capacity_window"] = {
                "start": _dt(ir["capacity_window_start"]),
                "end": _dt(ir["capacity_window_end"]),
            }
        infeasible_by_runway[ir["runway"]] = report_infeasible

    runways = []
    total_delayed = 0
    total_delay = 0
    total_items = 0
    for runway in order_by_runway:
        items = items_by_runway.get(runway, [])
        delayed = [i for i in items if i["delay_seconds"] > 0]
        rw_delay = sum(i["delay_seconds"] for i in items)
        max_delay = max((i["delay_seconds"] for i in items), default=0)
        total_delayed += len(delayed)
        total_delay += rw_delay
        total_items += len(items)
        runway_report = {
            "runway": runway,
            "order": [i["flight_no"] for i in items],
            "total": len(items),
            "closures": closures_by_runway.get(runway, []),
            "delayed_count": len(delayed),
            "total_delay_seconds": rw_delay,
            "max_delay_seconds": max_delay,
            "items": items,
        }
        if locked_used_by_runway.get(runway):
            runway_report["locked_flights"] = locked_flights_by_runway.get(runway, [])
            runway_report["infeasible"] = infeasible_by_runway.get(runway)
        if runway in capacities_by_runway:
            runway_report["capacity"] = capacities_by_runway[runway]
        runways.append(runway_report)
    return {
        "batch_id": row["batch_id"],
        "mode": "trial",
        "created_at": row["created_at"],
        "matrix": matrix,
        "total_runways": len(runways),
        "total_flights": total_items,
        "total_delayed": total_delayed,
        "total_delay_seconds": total_delay,
        "runways": runways,
    }
