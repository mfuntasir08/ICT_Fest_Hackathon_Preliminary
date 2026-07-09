"""Live per-room booking statistics.

Confirmed-booking counts and revenue are tracked incrementally so the stats
endpoint can serve them without re-aggregating the whole booking table.
"""
import threading

_stats: dict[int, dict] = {}
_lock = threading.Lock()


def init_all_stats() -> None:
    from ..database import SessionLocal
    from ..models import Booking
    from sqlalchemy import func
    
    db = SessionLocal()
    try:
        rows = (
            db.query(
                Booking.room_id,
                func.count(Booking.id),
                func.sum(Booking.price_cents)
            )
            .filter(Booking.status == "confirmed")
            .group_by(Booking.room_id)
            .all()
        )
        with _lock:
            for room_id, count, revenue in rows:
                _stats[room_id] = {"count": count or 0, "revenue": revenue or 0}
    except Exception:
        pass
    finally:
        db.close()


def record_create(room_id: int, price_cents: int) -> None:
    with _lock:
        if room_id not in _stats:
            _stats[room_id] = {"count": 0, "revenue": 0}
        current = _stats[room_id]
        _stats[room_id] = {"count": current["count"] + 1, "revenue": current["revenue"] + price_cents}


def record_cancel(room_id: int, price_cents: int) -> None:
    with _lock:
        if room_id not in _stats:
            _stats[room_id] = {"count": 0, "revenue": 0}
        current = _stats[room_id]
        _stats[room_id] = {"count": max(0, current["count"] - 1), "revenue": current["revenue"] - price_cents}


def get(room_id: int) -> dict:
    with _lock:
        if room_id not in _stats:
            _stats[room_id] = {"count": 0, "revenue": 0}
        return _stats[room_id].copy()
