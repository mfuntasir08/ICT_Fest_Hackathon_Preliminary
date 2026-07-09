"""Tests for Team C Phase 1 bug fixes: C1, C2, C3, C11, C12.

Run with: python tests/test_team_c_phase1.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from app.main import app
from app.database import Base, engine
from app import cache
from app.services import stats, ratelimit, reference

client = TestClient(app)

_user_counter = 0


def setup_fresh_db():
    """Drop and recreate all tables + reset in-memory state."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    # Reset in-memory caches/state
    cache._report_cache.clear()
    cache._availability_cache.clear()
    stats._stats.clear()
    ratelimit._buckets.clear()
    reference._counter["value"] = 1000


def get_unique_user():
    """Generate a unique username to avoid conflicts between tests."""
    global _user_counter
    _user_counter += 1
    return f"testuser_{_user_counter}"


def register_and_login(org_name=None, username=None, password="pass123"):
    """Register a user and return the access token."""
    if org_name is None:
        org_name = f"testorg_{get_unique_user()}"
    if username is None:
        username = get_unique_user()
    client.post("/auth/register", json={
        "org_name": org_name,
        "username": username,
        "password": password,
    })
    resp = client.post("/auth/login", json={
        "org_name": org_name,
        "username": username,
        "password": password,
    })
    return resp.json()["access_token"]


def create_room(token, name="Room A", capacity=10, rate=1000):
    """Create a room and return its id."""
    resp = client.post("/rooms", json={
        "name": name,
        "capacity": capacity,
        "hourly_rate_cents": rate,
    }, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 201, f"Room creation failed: {resp.text}"
    return resp.json()["id"]


def create_booking(token, room_id, start_time, end_time):
    """Create a booking and return the response."""
    resp = client.post("/bookings", json={
        "room_id": room_id,
        "start_time": start_time,
        "end_time": end_time,
    }, headers={"Authorization": f"Bearer {token}"})
    return resp


def test_c1_pagination_sort_ascending():
    """C1: GET /bookings should sort by start_time ASC, then id ASC."""
    print("\n--- Testing C1: Pagination Sort Order ---")
    setup_fresh_db()
    token = register_and_login()
    room_id = create_room(token)

    now = datetime.utcnow()
    # Create 3 bookings at different times (all >24h out to avoid quota issues)
    times = [
        (now + timedelta(hours=48), now + timedelta(hours=49)),
        (now + timedelta(hours=72), now + timedelta(hours=73)),
        (now + timedelta(hours=30), now + timedelta(hours=31)),
    ]

    for start, end in times:
        resp = create_booking(token, room_id, start.isoformat(), end.isoformat())
        assert resp.status_code == 201, f"Booking failed: {resp.text}"

    resp = client.get("/bookings", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 3, f"Expected 3 items, got {len(items)}"

    # Verify ascending order by start_time
    start_times = [item["start_time"] for item in items]
    assert start_times == sorted(start_times), \
        f"FAIL: Bookings not sorted ascending! Got: {start_times}"

    print("[PASS] C1: Pagination sorts by start_time ASC")


def test_c2_pagination_offset():
    """C2: Page 1 should return the FIRST items, not skip them."""
    print("\n--- Testing C2: Pagination Offset ---")
    setup_fresh_db()
    token = register_and_login()
    room_id = create_room(token)

    now = datetime.utcnow()
    # Create 3 bookings (spread far apart to avoid quota in 24h window)
    for i in range(3):
        start = now + timedelta(days=i + 1, hours=5)
        end = start + timedelta(hours=1)
        resp = create_booking(token, room_id, start.isoformat(), end.isoformat())
        assert resp.status_code == 201, f"Booking {i} failed: {resp.text}"

    # Page 1 with limit 2 should return 2 items
    resp = client.get("/bookings?page=1&limit=2",
                      headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 2, \
        f"FAIL: Page 1 should have 2 items, got {len(data['items'])}. Data: {data}"
    assert data["total"] == 3

    # Page 2 with limit 2 should return 1 item
    resp = client.get("/bookings?page=2&limit=2",
                      headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1, \
        f"FAIL: Page 2 should have 1 item, got {len(data['items'])}. Data: {data}"

    print("[PASS] C2: Pagination offset formula correct (page 1 starts at 0)")


def test_c3_pagination_limit():
    """C3: GET /bookings should respect the limit parameter, not hardcode 10."""
    print("\n--- Testing C3: Pagination Limit Parameter ---")
    setup_fresh_db()
    # Use separate users for different bookings to avoid quota (max 3 in 24h)
    org_name = f"limitorg_{get_unique_user()}"
    admin_user = get_unique_user()
    token = register_and_login(org_name=org_name, username=admin_user)
    room_id = create_room(token)

    now = datetime.utcnow()
    # Create 3 bookings (within quota limit of 3, spread across days)
    for i in range(3):
        start = now + timedelta(days=i + 1, hours=5)
        end = start + timedelta(hours=1)
        resp = create_booking(token, room_id, start.isoformat(), end.isoformat())
        assert resp.status_code == 201, f"Booking {i} failed: {resp.text}"

    # Request with limit=2 — should return exactly 2
    resp = client.get("/bookings?page=1&limit=2",
                      headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 2, \
        f"FAIL: limit=2 but got {len(data['items'])} items. Data: {data}"
    assert data["limit"] == 2

    # Request with limit=1 — should return exactly 1
    resp = client.get("/bookings?page=1&limit=1",
                      headers={"Authorization": f"Bearer {token}"})
    data = resp.json()
    assert len(data["items"]) == 1, \
        f"FAIL: limit=1 but got {len(data['items'])} items. Data: {data}"
    assert data["limit"] == 1

    # Request with limit=100 — should return all 3
    resp = client.get("/bookings?page=1&limit=100",
                      headers={"Authorization": f"Bearer {token}"})
    data = resp.json()
    assert len(data["items"]) == 3, \
        f"FAIL: limit=100 but got {len(data['items'])} items. Data: {data}"

    print("[PASS] C3: Pagination respects user-provided limit")


def test_c11_create_invalidates_report_cache():
    """C11: Creating a booking should invalidate the usage report cache."""
    print("\n--- Testing C11: Create Invalidates Report Cache ---")
    setup_fresh_db()
    token = register_and_login()
    room_id = create_room(token, rate=2000)

    today = datetime.utcnow().date()
    from_str = today.isoformat()
    to_str = (today + timedelta(days=30)).isoformat()

    # Get initial report (should be empty, gets cached)
    resp = client.get(f"/admin/usage-report?from={from_str}&to={to_str}",
                      headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    initial_report = resp.json()
    assert initial_report["rooms"][0]["confirmed_bookings"] == 0

    # Create a booking
    now = datetime.utcnow()
    start = now + timedelta(days=2)
    end = start + timedelta(hours=2)
    resp = create_booking(token, room_id, start.isoformat(), end.isoformat())
    assert resp.status_code == 201, f"Booking failed: {resp.text}"

    # Get report again — should reflect the new booking (not cached stale)
    resp = client.get(f"/admin/usage-report?from={from_str}&to={to_str}",
                      headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    updated_report = resp.json()
    room_data = updated_report["rooms"][0]
    assert room_data["confirmed_bookings"] == 1, \
        f"FAIL: Report still shows 0 bookings after create. Got: {room_data}"
    assert room_data["revenue_cents"] == 4000  # 2000 * 2 hours

    print("[PASS] C11: Booking creation invalidates report cache")


def test_c12_cancel_invalidates_availability_cache():
    """C12: Cancelling a booking should invalidate the availability cache."""
    print("\n--- Testing C12: Cancel Invalidates Availability Cache ---")
    setup_fresh_db()
    token = register_and_login()
    room_id = create_room(token)

    now = datetime.utcnow()
    start = now + timedelta(days=5)  # Far enough for >48h notice
    end = start + timedelta(hours=2)
    date_str = start.date().isoformat()

    # Create booking
    resp = create_booking(token, room_id, start.isoformat(), end.isoformat())
    assert resp.status_code == 201, f"Booking failed: {resp.text}"
    booking_id = resp.json()["id"]

    # Check availability — should show 1 busy slot (gets cached)
    resp = client.get(f"/rooms/{room_id}/availability?date={date_str}",
                      headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert len(resp.json()["busy"]) == 1, "Should have 1 busy slot"

    # Cancel the booking
    resp = client.post(f"/bookings/{booking_id}/cancel",
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200

    # Check availability again — should show 0 busy slots (cache invalidated)
    resp = client.get(f"/rooms/{room_id}/availability?date={date_str}",
                      headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    busy = resp.json()["busy"]
    assert len(busy) == 0, \
        f"FAIL: Availability still shows busy after cancel. Got: {busy}"

    print("[PASS] C12: Cancellation invalidates availability cache")


def test_pagination_sequential_pages():
    """Bonus: Verify sequential pages don't skip or repeat items (Rule 11)."""
    print("\n--- Testing Bonus: Sequential Pages No Skip/Repeat ---")
    setup_fresh_db()
    token = register_and_login()
    room_id = create_room(token)

    now = datetime.utcnow()
    # Create 3 bookings (within quota, spread across days)
    for i in range(3):
        start = now + timedelta(days=i + 1, hours=5)
        end = start + timedelta(hours=1)
        resp = create_booking(token, room_id, start.isoformat(), end.isoformat())
        assert resp.status_code == 201, f"Booking {i} failed: {resp.text}"

    # Collect all IDs across pages (limit=2)
    all_ids = []
    for page in [1, 2]:
        resp = client.get(f"/bookings?page={page}&limit=2",
                          headers={"Authorization": f"Bearer {token}"})
        items = resp.json()["items"]
        for item in items:
            all_ids.append(item["id"])

    assert len(all_ids) == 3, f"Expected 3 items across 2 pages, got {len(all_ids)}"
    assert len(set(all_ids)) == 3, f"Duplicate IDs found: {all_ids}"

    print("[PASS] Bonus: No skip/repeat across sequential pages")


if __name__ == "__main__":
    print("=" * 60)
    print("TEAM C PHASE 1 -- Bug Fix Tests")
    print("=" * 60)

    tests = [
        test_c1_pagination_sort_ascending,
        test_c2_pagination_offset,
        test_c3_pagination_limit,
        test_c11_create_invalidates_report_cache,
        test_c12_cancel_invalidates_availability_cache,
        test_pagination_sequential_pages,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  [FAIL] {test.__name__}: {e}")
            failed += 1

    print("\n" + "=" * 60)
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    print("=" * 60)
