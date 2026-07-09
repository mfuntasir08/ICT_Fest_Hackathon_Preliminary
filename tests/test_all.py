import sys
import os
import time
import concurrent.futures
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from app.main import app
from app.database import Base, engine
from app import cache
from app.services import stats, ratelimit, reference, notifications
from app.auth import decode_token

client = TestClient(app)
_user_counter = 0

def setup_fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    # Clear in-memory state
    from app import auth as app_auth
    app_auth._revoked_tokens.clear()
    app_auth._used_refresh_tokens.clear()
    cache._report_cache.clear()
    cache._availability_cache.clear()
    stats._stats.clear()
    stats.init_all_stats()
    ratelimit._buckets.clear()
    reference._counter["value"] = 1000

def get_unique_user():
    global _user_counter
    _user_counter += 1
    return f"testuser_{_user_counter}"

def register_and_login(org_name=None, username=None, password="password123"):
    if org_name is None:
        org_name = f"org_{get_unique_user()}"
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
    resp = client.post("/rooms", json={
        "name": name,
        "capacity": capacity,
        "hourly_rate_cents": rate,
    }, headers={"Authorization": f"Bearer {token}"})
    return resp.json()["id"]

# =====================================================================
# Auth, Token & Registration Tests (A1 - A5)
# =====================================================================

def test_a1_token_expiry():
    setup_fresh_db()
    token = register_and_login()
    payload = decode_token(token)
    duration = payload["exp"] - payload["iat"]
    assert duration == 900, f"Expected 900 seconds, got {duration}"

def test_a2_token_revocation_jti():
    setup_fresh_db()
    org = f"org_{get_unique_user()}"
    user1 = get_unique_user()
    user2 = f"{user1}_other"
    
    # Register two users in same org
    client.post("/auth/register", json={"org_name": org, "username": user1, "password": "pw"})
    client.post("/auth/register", json={"org_name": org, "username": user2, "password": "pw"})
    
    # Login user1 (twice for two active tokens)
    login1a = client.post("/auth/login", json={"org_name": org, "username": user1, "password": "pw"}).json()
    login1b = client.post("/auth/login", json={"org_name": org, "username": user1, "password": "pw"}).json()
    
    # Login user2
    login2 = client.post("/auth/login", json={"org_name": org, "username": user2, "password": "pw"}).json()
    
    token1a = login1a["access_token"]
    token1b = login1b["access_token"]
    token2 = login2["access_token"]
    
    # Logout session 1a (revokes token1a)
    logout_resp = client.post("/auth/logout", headers={"Authorization": f"Bearer {token1a}"})
    assert logout_resp.status_code == 200
    
    # Revoked token1a -> should fail 401
    resp = client.get("/rooms", headers={"Authorization": f"Bearer {token1a}"})
    assert resp.status_code == 401
    
    # Token1b (same user, different JTI) -> should succeed
    resp = client.get("/rooms", headers={"Authorization": f"Bearer {token1b}"})
    assert resp.status_code == 200
    
    # Token2 (different user) -> should succeed
    resp = client.get("/rooms", headers={"Authorization": f"Bearer {token2}"})
    assert resp.status_code == 200

def test_a3_duplicate_username():
    setup_fresh_db()
    org1 = f"org_{get_unique_user()}"
    org2 = f"org_{get_unique_user()}"
    username = get_unique_user()
    
    resp = client.post("/auth/register", json={"org_name": org1, "username": username, "password": "pw"})
    assert resp.status_code == 201
    
    resp = client.post("/auth/register", json={"org_name": org1, "username": username, "password": "pw"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Username already taken in this organization"
    
    resp = client.post("/auth/register", json={"org_name": org2, "username": username, "password": "pw"})
    assert resp.status_code == 201

def test_a4_refresh_tokens_single_use():
    setup_fresh_db()
    org = f"org_{get_unique_user()}"
    username = get_unique_user()
    
    client.post("/auth/register", json={"org_name": org, "username": username, "password": "pw"})
    login = client.post("/auth/login", json={"org_name": org, "username": username, "password": "pw"}).json()
    refresh_token = login["refresh_token"]
    
    refresh1 = client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh1.status_code == 200
    
    refresh2 = client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh2.status_code == 401

def test_a5_concurrent_registration_bug():
    setup_fresh_db()
    
    def register_org(org_name, username):
        resp = client.post("/auth/register", json={
            "org_name": org_name,
            "username": username,
            "password": "pw"
        })
        return resp.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(register_org, "concurrent_org", f"user_{i}") for i in range(5)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]
    assert all(r == 201 for r in results)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(register_org, "concurrent_org", "duplicate_user") for _ in range(5)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]
    
    assert results.count(201) == 1
    assert results.count(409) == 4

# =====================================================================
# Booking Logic, Pricing & Cancellation Tests (B1 - B8)
# =====================================================================

def test_b1_b2_booking_window_and_duration():
    setup_fresh_db()
    token = register_and_login()
    room_id = create_room(token)
    
    now = datetime.utcnow()
    
    # B1: Past start_time -> should fail 400
    past_start = now - timedelta(minutes=10)
    past_end = now + timedelta(hours=1)
    resp = client.post("/bookings", json={"room_id": room_id, "start_time": past_start.isoformat(), "end_time": past_end.isoformat()}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 400
    
    # B2: end_time <= start_time -> should fail 400
    start = now + timedelta(hours=1)
    resp = client.post("/bookings", json={"room_id": room_id, "start_time": start.isoformat(), "end_time": start.isoformat()}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 400
    
    # B2: Duration less than 1 hour -> should fail 400
    end_half_hour = start + timedelta(minutes=30)
    resp = client.post("/bookings", json={"room_id": room_id, "start_time": start.isoformat(), "end_time": end_half_hour.isoformat()}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 400
    
    # B2: Duration greater than 8 hours -> should fail 400
    end_nine_hours = start + timedelta(hours=9)
    resp = client.post("/bookings", json={"room_id": room_id, "start_time": start.isoformat(), "end_time": end_nine_hours.isoformat()}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 400

def test_b3_back_to_back_bookings():
    setup_fresh_db()
    token = register_and_login()
    room_id = create_room(token)
    
    now = datetime.utcnow()
    start1 = now + timedelta(days=1, hours=1)
    end1 = start1 + timedelta(hours=2)
    start2 = end1
    end2 = start2 + timedelta(hours=2)
    
    resp1 = client.post("/bookings", json={"room_id": room_id, "start_time": start1.isoformat(), "end_time": end1.isoformat()}, headers={"Authorization": f"Bearer {token}"})
    assert resp1.status_code == 201
    
    resp2 = client.post("/bookings", json={"room_id": room_id, "start_time": start2.isoformat(), "end_time": end2.isoformat()}, headers={"Authorization": f"Bearer {token}"})
    assert resp2.status_code == 201

def test_b4_b5_b6_cancellation_refunds():
    setup_fresh_db()
    token = register_and_login()
    room_id = create_room(token, rate=1001)  # Odd rate to test rounding
    
    now = datetime.utcnow()
    
    # Booking 1: >48h notice -> 100% refund
    start1 = now + timedelta(days=3)
    end1 = start1 + timedelta(hours=2)
    b1 = client.post("/bookings", json={"room_id": room_id, "start_time": start1.isoformat(), "end_time": end1.isoformat()}, headers={"Authorization": f"Bearer {token}"}).json()
    
    # Booking 2: 24h <= notice <= 48h -> 50% refund (e.g. 30h)
    start2 = now + timedelta(hours=30)
    end2 = start2 + timedelta(hours=2)
    b2 = client.post("/bookings", json={"room_id": room_id, "start_time": start2.isoformat(), "end_time": end2.isoformat()}, headers={"Authorization": f"Bearer {token}"}).json()
    
    # Booking 3: <24h notice -> 0% refund (e.g. 5h)
    start3 = now + timedelta(hours=5)
    end3 = start3 + timedelta(hours=2)
    b3 = client.post("/bookings", json={"room_id": room_id, "start_time": start3.isoformat(), "end_time": end3.isoformat()}, headers={"Authorization": f"Bearer {token}"}).json()
    
    # Cancel 1
    c1 = client.post(f"/bookings/{b1['id']}/cancel", headers={"Authorization": f"Bearer {token}"}).json()
    assert c1["refund_percent"] == 100
    assert c1["refund_amount_cents"] == 2002
    
    # Cancel 2: 50% of 2002 is 1001
    c2 = client.post(f"/bookings/{b2['id']}/cancel", headers={"Authorization": f"Bearer {token}"}).json()
    assert c2["refund_percent"] == 50
    assert c2["refund_amount_cents"] == 1001
    
    # Cancel 3: 0% of 2002 is 0
    c3 = client.post(f"/bookings/{b3['id']}/cancel", headers={"Authorization": f"Bearer {token}"}).json()
    assert c3["refund_percent"] == 0
    assert c3["refund_amount_cents"] == 0

def test_b7_timezone_conversion():
    from app.timeutils import parse_input_datetime
    # 2026-07-10T02:00:00+05:30 -> UTC equivalent is 2026-07-09T20:30:00
    parsed = parse_input_datetime("2026-07-10T02:00:00+05:30")
    assert parsed == datetime(2026, 7, 9, 20, 30, 0)

def test_b8_booking_detail_start_time():
    setup_fresh_db()
    token = register_and_login()
    room_id = create_room(token)
    
    now = datetime.utcnow()
    start = (now + timedelta(days=1, hours=5)).replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=2)
    
    booking = client.post("/bookings", json={"room_id": room_id, "start_time": start.isoformat(), "end_time": end.isoformat()}, headers={"Authorization": f"Bearer {token}"}).json()
    
    detail = client.get(f"/bookings/{booking['id']}", headers={"Authorization": f"Bearer {token}"}).json()
    assert detail["start_time"] != detail["created_at"]
    # Detail start_time should match input start time (rendered as UTC ISO)
    assert detail["start_time"].startswith(start.replace(minute=0, second=0, microsecond=0).isoformat()[:16])

# =====================================================================
# Concurrency, Pagination, Caching, Stats & Liveness (C1 - C13)
# =====================================================================

def test_c1_c2_c3_pagination():
    setup_fresh_db()
    token = register_and_login()
    room_id = create_room(token)
    
    now = datetime.utcnow()
    # Create 3 bookings (on separate days to avoid quota)
    for i in range(3):
        start = now + timedelta(days=i + 1, hours=5)
        end = start + timedelta(hours=1)
        client.post("/bookings", json={"room_id": room_id, "start_time": start.isoformat(), "end_time": end.isoformat()}, headers={"Authorization": f"Bearer {token}"})
        
    # C1: Sorted ascending by start_time
    resp = client.get("/bookings", headers={"Authorization": f"Bearer {token}"}).json()
    items = resp["items"]
    assert len(items) == 3
    assert items[0]["start_time"] < items[1]["start_time"] < items[2]["start_time"]
    
    # C2: Offset calculation
    resp_p1 = client.get("/bookings?page=1&limit=2", headers={"Authorization": f"Bearer {token}"}).json()
    assert len(resp_p1["items"]) == 2
    assert resp_p1["total"] == 3
    
    resp_p2 = client.get("/bookings?page=2&limit=2", headers={"Authorization": f"Bearer {token}"}).json()
    assert len(resp_p2["items"]) == 1
    
    # C3: Respect limit parameter
    resp_l1 = client.get("/bookings?page=1&limit=1", headers={"Authorization": f"Bearer {token}"}).json()
    assert len(resp_l1["items"]) == 1

def test_c4_no_deadlock_on_notifications():
    setup_fresh_db()
    
    class DummyBooking:
        id = 123
        room_id = 1
        price_cents = 100
        status = "confirmed"
    
    dummy = DummyBooking()
    
    def run_create():
        for _ in range(30):
            notifications.notify_created(dummy)
            
    def run_cancel():
        for _ in range(30):
            notifications.notify_cancelled(dummy)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(run_create) for _ in range(3)] + [executor.submit(run_cancel) for _ in range(3)]
        concurrent.futures.wait(futures)

def test_c5_concurrent_reference_uniqueness():
    setup_fresh_db()
    
    def get_code():
        return reference.next_reference_code()
        
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(lambda _: get_code(), range(50)))
        
    assert len(results) == len(set(results))

def test_c6_concurrent_stats():
    setup_fresh_db()
    
    def run_creates():
        for _ in range(30):
            stats.record_create(1, 100)
            
    def run_cancels():
        for _ in range(20):
            stats.record_cancel(1, 100)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(run_creates) for _ in range(3)] + [executor.submit(run_cancels) for _ in range(3)]
        concurrent.futures.wait(futures)
        
    # Creates = 3 * 30 = 90
    # Cancels = 3 * 20 = 60
    # Net count = 30, Net revenue = 3000
    current = stats.get(1)
    assert current["count"] == 30
    assert current["revenue"] == 3000

def test_c7_concurrent_rate_limiting():
    setup_fresh_db()
    
    def attempt_request():
        try:
            ratelimit.record_and_check(user_id=99)
            return True
        except Exception:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as executor:
        results = list(executor.map(lambda _: attempt_request(), range(30)))
        
    success = results.count(True)
    limited = results.count(False)
    assert success == 20
    assert limited == 10

def test_c8_c9_immediate_updates():
    setup_fresh_db()
    token = register_and_login()
    room_id = create_room(token, rate=1500)
    
    today = datetime.utcnow().date()
    date_str = today.isoformat()
    from_str = date_str
    to_str = (today + timedelta(days=5)).isoformat()
    
    # Empty initial reports
    avail_resp = client.get(f"/rooms/{room_id}/availability?date={date_str}", headers={"Authorization": f"Bearer {token}"})
    report_resp = client.get(f"/admin/usage-report?from={from_str}&to={to_str}", headers={"Authorization": f"Bearer {token}"})
    assert len(avail_resp.json()["busy"]) == 0
    assert report_resp.json()["rooms"][0]["confirmed_bookings"] == 0
    
    # Create booking
    start = datetime.combine(today, datetime.utcnow().time()) + timedelta(hours=5)
    end = start + timedelta(hours=2)
    client.post("/bookings", json={"room_id": room_id, "start_time": start.isoformat(), "end_time": end.isoformat()}, headers={"Authorization": f"Bearer {token}"})
    
    # Availability/report must update immediately (no caching)
    avail_resp = client.get(f"/rooms/{room_id}/availability?date={date_str}", headers={"Authorization": f"Bearer {token}"})
    report_resp = client.get(f"/admin/usage-report?from={from_str}&to={to_str}", headers={"Authorization": f"Bearer {token}"})
    assert len(avail_resp.json()["busy"]) == 1
    assert report_resp.json()["rooms"][0]["confirmed_bookings"] == 1

def test_c10_export_scoping():
    setup_fresh_db()
    
    token_a = register_and_login("org_a", "admin_a")
    token_b = register_and_login("org_b", "admin_b")
    
    room_b = create_room(token_b, "Room B")
    
    # Create booking in Room B (Org B)
    now = datetime.utcnow()
    start = now + timedelta(days=2)
    end = start + timedelta(hours=1)
    client.post("/bookings", json={"room_id": room_b, "start_time": start.isoformat(), "end_time": end.isoformat()}, headers={"Authorization": f"Bearer {token_b}"})
    
    # Admin A tries to export Room B -> should be empty (scoping bypass check)
    export_resp = client.get(f"/admin/export?room_id={room_b}&include_all=true", headers={"Authorization": f"Bearer {token_a}"})
    csv_content = export_resp.text.strip().split("\n")
    assert len(csv_content) == 1  # Only header

def test_c13_no_artificial_delay():
    setup_fresh_db()
    token = register_and_login()
    room_id = create_room(token)
    
    start_time = time.time()
    now = datetime.utcnow()
    start = now + timedelta(days=1)
    end = start + timedelta(hours=1)
    
    resp = client.post("/bookings", json={"room_id": room_id, "start_time": start.isoformat(), "end_time": end.isoformat()}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 201
    
    elapsed = time.time() - start_time
    # Should take way less than 200ms without sleeps
    assert elapsed < 0.2

# =====================================================================
# Additional Multi-Tenancy & Extra Tests
# =====================================================================

def test_booking_visibility():
    setup_fresh_db()
    admin_token = register_and_login("org_v", "admin")
    m1_token = register_and_login("org_v", "m1")
    m2_token = register_and_login("org_v", "m2")
    
    room_id = create_room(admin_token)
    
    now = datetime.utcnow()
    start = now + timedelta(days=1)
    end = start + timedelta(hours=1)
    
    b1 = client.post("/bookings", json={"room_id": room_id, "start_time": start.isoformat(), "end_time": end.isoformat()}, headers={"Authorization": f"Bearer {m1_token}"}).json()
    
    # Member 2 tries to get Member 1's booking -> should fail 404
    get_resp = client.get(f"/bookings/{b1['id']}", headers={"Authorization": f"Bearer {m2_token}"})
    assert get_resp.status_code == 404

def test_stats_persistence():
    setup_fresh_db()
    admin_token = register_and_login("org_p", "admin")
    room_id = create_room(admin_token)
    
    now = datetime.utcnow()
    start = now + timedelta(days=1)
    end = start + timedelta(hours=1)
    
    client.post("/bookings", json={"room_id": room_id, "start_time": start.isoformat(), "end_time": end.isoformat()}, headers={"Authorization": f"Bearer {admin_token}"})
    
    # Restart
    stats._stats.clear()
    stats.init_all_stats()
    
    stats_resp = client.get(f"/rooms/{room_id}/stats", headers={"Authorization": f"Bearer {admin_token}"})
    assert stats_resp.json()["total_confirmed_bookings"] == 1

def test_concurrent_double_booking():
    setup_fresh_db()
    admin_token = register_and_login("org_d", "admin")
    room_id = create_room(admin_token)
    
    now = datetime.utcnow()
    start = now + timedelta(days=1)
    end = start + timedelta(hours=1)
    
    def make_booking():
        resp = client.post("/bookings", json={"room_id": room_id, "start_time": start.isoformat(), "end_time": end.isoformat()}, headers={"Authorization": f"Bearer {admin_token}"})
        return resp.status_code
        
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(make_booking), executor.submit(make_booking)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]
        
    assert results.count(201) == 1
    assert results.count(409) == 1

if __name__ == "__main__":
    test_functions = [
        obj for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    ]
    
    passed = 0
    failed = 0
    print(f"Running {len(test_functions)} consolidated tests...")
    print("=" * 60)
    
    for test in test_functions:
        try:
            print(f"Running {test.__name__}...", end="", flush=True)
            # Reset DB/cache before each test to maintain isolation
            test()
            print(" [PASS]")
            passed += 1
        except Exception as e:
            print(" [FAIL]")
            import traceback
            traceback.print_exc()
            failed += 1
            
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed out of {len(test_functions)}")
    print("=" * 60)
    
    if failed > 0:
        sys.exit(1)
    else:
        sys.exit(0)
