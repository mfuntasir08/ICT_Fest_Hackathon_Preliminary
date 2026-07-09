"""Comprehensive test suite for all Team C bug fixes:
C4: Notification Deadlock
C5: Reference Code Race Condition
C6: Stats Race Condition
C7: Rate Limiter Race Condition
C8: Availability Cache Stale Data / Immediate Reflection
C9: Usage Report Cache Stale Data / Immediate Reflection
C10: Export Scoping (Multi-tenancy bypass)
C13: Removal of blocking time.sleep()

Run with: python tests/test_team_c_all.py
"""
import sys
import os
import time
import concurrent.futures
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from app.main import app
from app.database import Base, engine
from app import cache
from app.services import stats, ratelimit, reference, notifications

client = TestClient(app)

_user_counter = 0

def setup_fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    cache._report_cache.clear()
    cache._availability_cache.clear()
    stats._stats.clear()
    ratelimit._buckets.clear()
    reference._counter["value"] = 1000

def get_unique_user():
    global _user_counter
    _user_counter += 1
    return f"user_{_user_counter}"

def register_and_login(org_name, username, password="password123"):
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

def create_room(token, name="Room X", capacity=10, rate=1000):
    resp = client.post("/rooms", json={
        "name": name,
        "capacity": capacity,
        "hourly_rate_cents": rate,
    }, headers={"Authorization": f"Bearer {token}"})
    return resp.json()["id"]

# C13: Time sleep removal verification
def test_c13_no_artificial_delay():
    print("\n--- Testing C13: Absence of time.sleep delays ---")
    setup_fresh_db()
    token = register_and_login("org_c13", "admin_c13")
    room_id = create_room(token)
    
    start_time = time.time()
    now = datetime.utcnow()
    start = now + timedelta(days=1)
    end = start + timedelta(hours=1)
    
    # Create booking should be extremely fast now (well under 50ms) without sleeps
    resp = client.post("/bookings", json={
        "room_id": room_id,
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
    }, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 201
    
    elapsed = time.time() - start_time
    print(f"Booking creation took {elapsed:.4f} seconds")
    # Before, it was sleeping at least 0.12 (pricing) + 0.1 (quota) + 0.12 (reference) + 0.12 (email/audit) = ~0.46s
    assert elapsed < 0.2, f"FAIL: Booking took too long ({elapsed}s), indicating time.sleep() might still be present."
    print("[PASS] C13: Sleeps removed successfully")

# C4: Lock deadlock verification
def test_c4_no_deadlock_on_notifications():
    print("\n--- Testing C4: Notification Deadlock avoidance ---")
    setup_fresh_db()
    token = register_and_login("org_c4", "admin_c4")
    room_id = create_room(token)
    
    # Mocking booking class for notifications
    class DummyBooking:
        id = 123
        room_id = 1
        price_cents = 100
        status = "confirmed"
    
    # Run multiple notifications concurrently to trigger any lock ordering issues
    dummy = DummyBooking()
    
    def run_create():
        for _ in range(50):
            notifications.notify_created(dummy)
            
    def run_cancel():
        for _ in range(50):
            notifications.notify_cancelled(dummy)

    start_time = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(run_create) for _ in range(5)] + [executor.submit(run_cancel) for _ in range(5)]
        concurrent.futures.wait(futures)
        
    print(f"Concurrency notify test finished in {time.time() - start_time:.4f}s")
    print("[PASS] C4: No deadlock occurred")

# C5: Reference code uniqueness
def test_c5_concurrent_reference_uniqueness():
    print("\n--- Testing C5: Concurrent Reference Code Uniqueness ---")
    setup_fresh_db()
    
    codes = []
    def get_code():
        return reference.next_reference_code()
        
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        results = list(executor.map(lambda _: get_code(), range(100)))
        
    assert len(results) == len(set(results)), f"FAIL: Duplicate reference codes generated: {results}"
    print("[PASS] C5: All concurrent reference codes are unique")

# C6: Stats correctness under concurrent changes
def test_c6_concurrent_stats():
    print("\n--- Testing C6: Concurrent Stats Updates ---")
    setup_fresh_db()
    
    # Simulate concurrent creates and cancels
    def run_creates():
        for _ in range(50):
            stats.record_create(1, 100)
            
    def run_cancels():
        for _ in range(30):
            stats.record_cancel(1, 100)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(run_creates) for _ in range(5)] + [executor.submit(run_cancels) for _ in range(5)]
        concurrent.futures.wait(futures)
        
    # Total creates = 5 * 50 = 250
    # Total cancels = 5 * 30 = 150
    # Final count should be 100, final revenue should be 100 * 100 = 10000 cents
    current = stats.get(1)
    assert current["count"] == 100, f"Expected count 100, got {current['count']}"
    assert current["revenue"] == 10000, f"Expected revenue 10000, got {current['revenue']}"
    print("[PASS] C6: Stats incremental counters are correct and thread-safe")

# C7: Rate limiting thread-safety
def test_c7_concurrent_rate_limiting():
    print("\n--- Testing C7: Concurrent Rate Limiter ---")
    setup_fresh_db()
    
    success = 0
    limited = 0
    
    def attempt_request():
        try:
            ratelimit.record_and_check(user_id=42)
            return True
        except Exception:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as executor:
        results = list(executor.map(lambda _: attempt_request(), range(30)))
        
    for r in results:
        if r:
            success += 1
        else:
            limited += 1
            
    # Max requests is 20, so exactly 20 should succeed, and 10 should be rate limited
    assert success == 20, f"Expected exactly 20 successes, got {success}"
    assert limited == 10, f"Expected exactly 10 rate limited, got {limited}"
    print("[PASS] C7: Rate limiter correctly restricts concurrent bursts to max limit")

# C8 & C9: immediate availability/report updates
def test_c8_c9_immediate_updates():
    print("\n--- Testing C8 & C9: Immediate updates (No cache stale data) ---")
    setup_fresh_db()
    token = register_and_login("org_c89", "admin_c89")
    room_id = create_room(token, rate=1500)
    
    today = datetime.utcnow().date()
    date_str = today.isoformat()
    from_str = date_str
    to_str = (today + timedelta(days=5)).isoformat()
    
    # 1. Check availability & report (empty)
    avail_resp = client.get(f"/rooms/{room_id}/availability?date={date_str}", headers={"Authorization": f"Bearer {token}"})
    report_resp = client.get(f"/admin/usage-report?from={from_str}&to={to_str}", headers={"Authorization": f"Bearer {token}"})
    assert len(avail_resp.json()["busy"]) == 0
    assert report_resp.json()["rooms"][0]["confirmed_bookings"] == 0
    
    # 2. Create booking
    start = datetime.combine(today, datetime.utcnow().time()) + timedelta(hours=5)
    end = start + timedelta(hours=2)
    create_resp = client.post("/bookings", json={
        "room_id": room_id,
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
    }, headers={"Authorization": f"Bearer {token}"})
    assert create_resp.status_code == 201
    booking_id = create_resp.json()["id"]
    
    # 3. Check immediately again (should show 1 busy slot and 1 confirmed booking, no stale cache)
    avail_resp = client.get(f"/rooms/{room_id}/availability?date={date_str}", headers={"Authorization": f"Bearer {token}"})
    report_resp = client.get(f"/admin/usage-report?from={from_str}&to={to_str}", headers={"Authorization": f"Bearer {token}"})
    assert len(avail_resp.json()["busy"]) == 1
    assert report_resp.json()["rooms"][0]["confirmed_bookings"] == 1
    assert report_resp.json()["rooms"][0]["revenue_cents"] == 3000
    
    # 4. Cancel booking (notice > 48h to simplify)
    cancel_start = datetime.utcnow() + timedelta(days=5)
    cancel_end = cancel_start + timedelta(hours=2)
    cancel_date_str = cancel_start.date().isoformat()
    
    cancel_booking_resp = client.post("/bookings", json={
        "room_id": room_id,
        "start_time": cancel_start.isoformat(),
        "end_time": cancel_end.isoformat(),
    }, headers={"Authorization": f"Bearer {token}"})
    assert cancel_booking_resp.status_code == 201
    cancel_booking_id = cancel_booking_resp.json()["id"]
    
    # Verify busy slot exists
    avail_resp2 = client.get(f"/rooms/{room_id}/availability?date={cancel_date_str}", headers={"Authorization": f"Bearer {token}"})
    assert len(avail_resp2.json()["busy"]) == 1
    
    # Cancel it
    cancel_resp = client.post(f"/bookings/{cancel_booking_id}/cancel", headers={"Authorization": f"Bearer {token}"})
    assert cancel_resp.status_code == 200
    
    # Verify immediately free
    avail_resp3 = client.get(f"/rooms/{room_id}/availability?date={cancel_date_str}", headers={"Authorization": f"Bearer {token}"})
    assert len(avail_resp3.json()["busy"]) == 0
    
    print("[PASS] C8 & C9: Availability and Usage Reports update immediately without stale cache issues")

# C10: Export scoping multi-tenancy bypass prevention
def test_c10_export_scoping():
    print("\n--- Testing C10: Export scoping multi-tenancy protection ---")
    setup_fresh_db()
    
    # Create two orgs
    token_a = register_and_login("org_a", "admin_a")
    token_b = register_and_login("org_b", "admin_b")
    
    room_a = create_room(token_a, "Room A")
    room_b = create_room(token_b, "Room B")
    
    # Create a booking in room B
    now = datetime.utcnow()
    start = now + timedelta(days=2)
    end = start + timedelta(hours=1)
    resp = client.post("/bookings", json={
        "room_id": room_b,
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
    }, headers={"Authorization": f"Bearer {token_b}"})
    assert resp.status_code == 201
    
    # Admin A attempts to export including all for room B (room of org B)
    # The export should return nothing because room_b belongs to org_b, and admin_a is in org_a
    export_resp = client.get(f"/admin/export?room_id={room_b}&include_all=true", headers={"Authorization": f"Bearer {token_a}"})
    assert export_resp.status_code == 200
    
    csv_content = export_resp.text.strip().split("\n")
    # Should only contain the header row (length 1), no rows from room_b because of org scoping!
    assert len(csv_content) == 1, f"FAIL: Admin A bypassed multi-tenancy and exported room B bookings: {csv_content}"
    
    # Admin B attempts the same export and should succeed
    export_resp_b = client.get(f"/admin/export?room_id={room_b}&include_all=true", headers={"Authorization": f"Bearer {token_b}"})
    assert export_resp_b.status_code == 200
    csv_content_b = export_resp_b.text.strip().split("\n")
    assert len(csv_content_b) == 2, f"FAIL: Admin B could not export their own room bookings: {csv_content_b}"
    
    print("[PASS] C10: Multi-tenant scoping verified in Export endpoint")

if __name__ == "__main__":
    print("=" * 60)
    print("TEAM C -- COMPREHENSIVE BUG FIX TESTS")
    print("=" * 60)

    tests = [
        test_c13_no_artificial_delay,
        test_c4_no_deadlock_on_notifications,
        test_c5_concurrent_reference_uniqueness,
        test_c6_concurrent_stats,
        test_c7_concurrent_rate_limiting,
        test_c8_c9_immediate_updates,
        test_c10_export_scoping,
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
    if failed > 0:
        sys.exit(1)
