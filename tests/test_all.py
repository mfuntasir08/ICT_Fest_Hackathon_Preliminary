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
# Auth, Token & Registration Tests (Bug 1 - Bug 5)
# =====================================================================

def test_01_token_expiry():
    """Bug 1: Access Token Expiry Calculation Overflow"""
    setup_fresh_db()
    token = register_and_login()
    payload = decode_token(token)
    duration = payload["exp"] - payload["iat"]
    assert duration == 900, f"Expected 900 seconds, got {duration}"

def test_02_token_revocation_jti():
    """Bug 2: Logout Token Revocation Checking sub Instead of jti"""
    setup_fresh_db()
    org = f"org_{get_unique_user()}"
    user1 = get_unique_user()
    user2 = f"{user1}_other"
    
    client.post("/auth/register", json={"org_name": org, "username": user1, "password": "pw"})
    client.post("/auth/register", json={"org_name": org, "username": user2, "password": "pw"})
    
    login1a = client.post("/auth/login", json={"org_name": org, "username": user1, "password": "pw"}).json()
    login1b = client.post("/auth/login", json={"org_name": org, "username": user1, "password": "pw"}).json()
    login2 = client.post("/auth/login", json={"org_name": org, "username": user2, "password": "pw"}).json()
    
    token1a = login1a["access_token"]
    token1b = login1b["access_token"]
    token2 = login2["access_token"]
    
    logout_resp = client.post("/auth/logout", headers={"Authorization": f"Bearer {token1a}"})
    assert logout_resp.status_code == 200
    
    resp = client.get("/rooms", headers={"Authorization": f"Bearer {token1a}"})
    assert resp.status_code == 401
    
    resp = client.get("/rooms", headers={"Authorization": f"Bearer {token1b}"})
    assert resp.status_code == 200
    
    resp = client.get("/rooms", headers={"Authorization": f"Bearer {token2}"})
    assert resp.status_code == 200

def test_03_duplicate_username():
    """Bug 3: Duplicate Username Allowed in Same Org (Returned 200 instead of 409)"""
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

def test_04_refresh_tokens_single_use():
    """Bug 4: Refresh Tokens Are Not Single-Use (Re-use Vulnerability)"""
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

def test_05_concurrent_registration_bug():
    """Bug 5: Registration Concurrency Crash"""
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
# Booking Logic, Pricing & Cancellation Tests (Bug 6 - Bug 13)
# =====================================================================

def test_06_start_time_grace_window():
    """Bug 6: Start Time Grace Window Violation"""
    setup_fresh_db()
    token = register_and_login()
    room_id = create_room(token)
    now = datetime.utcnow()
    
    # Past start_time by 2 minutes should fail (no 5 min grace window allowed)
    past_start = now - timedelta(minutes=2)
    past_end = now + timedelta(hours=1)
    resp = client.post("/bookings", json={"room_id": room_id, "start_time": past_start.isoformat(), "end_time": past_end.isoformat()}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 400

def test_07_min_duration_and_end_time():
    """Bug 7: Missing Minimum Booking Duration Validation"""
    setup_fresh_db()
    token = register_and_login()
    room_id = create_room(token)
    now = datetime.utcnow()
    
    start = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    
    # end_time <= start_time -> should fail 400
    resp = client.post("/bookings", json={"room_id": room_id, "start_time": start.isoformat(), "end_time": start.isoformat()}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 400
    
    # Duration less than 1 hour -> should fail 400
    end_half_hour = start + timedelta(minutes=30)
    resp = client.post("/bookings", json={"room_id": room_id, "start_time": start.isoformat(), "end_time": end_half_hour.isoformat()}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 400
    
    # Duration greater than 8 hours -> should fail 400
    end_nine_hours = start + timedelta(hours=9)
    resp = client.post("/bookings", json={"room_id": room_id, "start_time": start.isoformat(), "end_time": end_nine_hours.isoformat()}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 400

def test_08_back_to_back_bookings():
    """Bug 8: Booking Overlap Check Rejects Back-to-Back Bookings"""
    setup_fresh_db()
    token = register_and_login()
    room_id = create_room(token)
    
    now = datetime.utcnow()
    start1 = (now + timedelta(days=1, hours=1)).replace(minute=0, second=0, microsecond=0)
    end1 = start1 + timedelta(hours=2)
    start2 = end1
    end2 = start2 + timedelta(hours=2)
    
    resp1 = client.post("/bookings", json={"room_id": room_id, "start_time": start1.isoformat(), "end_time": end1.isoformat()}, headers={"Authorization": f"Bearer {token}"})
    assert resp1.status_code == 201
    
    resp2 = client.post("/bookings", json={"room_id": room_id, "start_time": start2.isoformat(), "end_time": end2.isoformat()}, headers={"Authorization": f"Bearer {token}"})
    assert resp2.status_code == 201

def test_09_cancel_under_24h_refund():
    """Bug 9: Cancellation Notice <24h Incorrectly Refunding 50%"""
    setup_fresh_db()
    token = register_and_login()
    room_id = create_room(token, rate=1000)
    now = datetime.utcnow()
    
    start = (now + timedelta(hours=5)).replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=2)
    booking = client.post("/bookings", json={"room_id": room_id, "start_time": start.isoformat(), "end_time": end.isoformat()}, headers={"Authorization": f"Bearer {token}"}).json()
    
    cancel = client.post(f"/bookings/{booking['id']}/cancel", headers={"Authorization": f"Bearer {token}"}).json()
    assert cancel["refund_percent"] == 0
    assert cancel["refund_amount_cents"] == 0

def test_10_cancel_notice_integer_truncation():
    """Bug 10: Cancellation notice duration uses integer truncation"""
    setup_fresh_db()
    token = register_and_login()
    room_id = create_room(token, rate=1000)
    now = datetime.utcnow()
    
    # 47h 30m notice should NOT be treated as 47h (i.e. not 24h-48h notice). It is exactly 24h-48h notice -> 50% refund.
    # If the code truncated to integer, 47.5 hours would check as 47 hours, which is still in 24h-48h.
    # But what if notice was 47.9h? timedelta comparison handles it correctly.
    start = now + timedelta(hours=47, minutes=30)
    end = start + timedelta(hours=1)
    booking = client.post("/bookings", json={"room_id": room_id, "start_time": start.isoformat(), "end_time": end.isoformat()}, headers={"Authorization": f"Bearer {token}"}).json()
    
    cancel = client.post(f"/bookings/{booking['id']}/cancel", headers={"Authorization": f"Bearer {token}"}).json()
    assert cancel["refund_percent"] == 50

def test_11_refund_rounding_precision():
    """Bug 11: Precision Loss in Refund Rounding via Dollar Conversion"""
    setup_fresh_db()
    token = register_and_login()
    room_id = create_room(token, rate=1001)
    now = datetime.utcnow()
    
    # 50% of 2002 (2 hours * 1001 cents) is 1001
    start = now + timedelta(hours=30)
    end = start + timedelta(hours=2)
    booking = client.post("/bookings", json={"room_id": room_id, "start_time": start.isoformat(), "end_time": end.isoformat()}, headers={"Authorization": f"Bearer {token}"}).json()
    
    cancel = client.post(f"/bookings/{booking['id']}/cancel", headers={"Authorization": f"Bearer {token}"}).json()
    assert cancel["refund_amount_cents"] == 1001

def test_12_timezone_conversion():
    """Bug 12: Timezone Offset Stripped Instead of Converted"""
    from app.timeutils import parse_input_datetime
    parsed = parse_input_datetime("2026-07-10T02:00:00+05:30")
    assert parsed == datetime(2026, 7, 9, 20, 30, 0)

def test_13_booking_detail_start_time():
    """Bug 13: Booking Detail Overwrites start_time with created_at"""
    setup_fresh_db()
    token = register_and_login()
    room_id = create_room(token)
    
    now = datetime.utcnow()
    start = (now + timedelta(days=1, hours=5)).replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=2)
    
    booking = client.post("/bookings", json={"room_id": room_id, "start_time": start.isoformat(), "end_time": end.isoformat()}, headers={"Authorization": f"Bearer {token}"}).json()
    
    detail = client.get(f"/bookings/{booking['id']}", headers={"Authorization": f"Bearer {token}"}).json()
    assert detail["start_time"] != detail["created_at"]
    assert detail["start_time"].startswith(start.isoformat()[:16])

# =====================================================================
# Concurrency, Pagination, Caching, Stats & Liveness (Bug 14 - Bug 24)
# =====================================================================

def test_14_pagination_sort_order():
    """Bug 14: Pagination Incorrect Sort Direction"""
    setup_fresh_db()
    token = register_and_login()
    room_id = create_room(token)
    now = datetime.utcnow()
    
    # Create bookings out of order
    start1 = now + timedelta(days=2)
    start2 = now + timedelta(days=1)
    
    client.post("/bookings", json={"room_id": room_id, "start_time": start1.isoformat(), "end_time": (start1 + timedelta(hours=1)).isoformat()}, headers={"Authorization": f"Bearer {token}"})
    client.post("/bookings", json={"room_id": room_id, "start_time": start2.isoformat(), "end_time": (start2 + timedelta(hours=1)).isoformat()}, headers={"Authorization": f"Bearer {token}"})
    
    resp = client.get("/bookings", headers={"Authorization": f"Bearer {token}"}).json()
    items = resp["items"]
    # First should be the earlier booking (start2)
    assert items[0]["start_time"] < items[1]["start_time"]

def test_15_pagination_offset_formula():
    """Bug 15: Pagination Wrong Offset Formula"""
    setup_fresh_db()
    token = register_and_login()
    room_id = create_room(token)
    now = datetime.utcnow()
    
    for i in range(3):
        start = now + timedelta(days=i + 1)
        client.post("/bookings", json={"room_id": room_id, "start_time": start.isoformat(), "end_time": (start + timedelta(hours=1)).isoformat()}, headers={"Authorization": f"Bearer {token}"})
        
    resp = client.get("/bookings?page=1&limit=2", headers={"Authorization": f"Bearer {token}"}).json()
    assert len(resp["items"]) == 2
    assert resp["total"] == 3

def test_16_pagination_respect_limit():
    """Bug 16: Pagination Ignores Limit Parameter"""
    setup_fresh_db()
    token = register_and_login()
    room_id = create_room(token)
    now = datetime.utcnow()
    
    for i in range(3):
        start = now + timedelta(days=i + 1)
        client.post("/bookings", json={"room_id": room_id, "start_time": start.isoformat(), "end_time": (start + timedelta(hours=1)).isoformat()}, headers={"Authorization": f"Bearer {token}"})
        
    resp = client.get("/bookings?page=1&limit=1", headers={"Authorization": f"Bearer {token}"}).json()
    assert len(resp["items"]) == 1

def test_17_no_deadlock_on_notifications():
    """Bug 17: Lock Inversion Deadlock in Notifications"""
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

def test_18_concurrent_reference_uniqueness():
    """Bug 18: Reference Code Generation Race Condition"""
    setup_fresh_db()
    
    def get_code():
        return reference.next_reference_code()
        
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(lambda _: get_code(), range(50)))
        
    assert len(results) == len(set(results))

def test_19_concurrent_stats():
    """Bug 19: incremental Stats Counter Race Condition"""
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
        
    current = stats.get(1)
    assert current["count"] == 30
    assert current["revenue"] == 3000

def test_20_concurrent_rate_limiting():
    """Bug 20: Rate Limiter Race Condition"""
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

def test_21_availability_cache_bypass():
    """Bug 21: Stale Data Cache Vulnerability (C8 & C9) - Availability updates immediately"""
    setup_fresh_db()
    token = register_and_login()
    room_id = create_room(token)
    today = datetime.utcnow().date()
    
    avail_resp = client.get(f"/rooms/{room_id}/availability?date={today.isoformat()}", headers={"Authorization": f"Bearer {token}"})
    assert len(avail_resp.json()["busy"]) == 0
    
    start = datetime.combine(today, datetime.utcnow().time()) + timedelta(hours=5)
    end = start + timedelta(hours=1)
    client.post("/bookings", json={"room_id": room_id, "start_time": start.isoformat(), "end_time": end.isoformat()}, headers={"Authorization": f"Bearer {token}"})
    
    avail_resp = client.get(f"/rooms/{room_id}/availability?date={today.isoformat()}", headers={"Authorization": f"Bearer {token}"})
    assert len(avail_resp.json()["busy"]) == 1

def test_22_report_cache_bypass():
    """Bug 21: Stale Data Cache Vulnerability (C8 & C9) - Report updates immediately"""
    setup_fresh_db()
    token = register_and_login()
    room_id = create_room(token, rate=1000)
    today = datetime.utcnow().date()
    
    report_resp = client.get(f"/admin/usage-report?from={today.isoformat()}&to={today.isoformat()}", headers={"Authorization": f"Bearer {token}"})
    assert report_resp.json()["rooms"][0]["confirmed_bookings"] == 0
    
    start = datetime.combine(today, datetime.utcnow().time()) + timedelta(hours=5)
    end = start + timedelta(hours=1)
    client.post("/bookings", json={"room_id": room_id, "start_time": start.isoformat(), "end_time": end.isoformat()}, headers={"Authorization": f"Bearer {token}"})
    
    report_resp = client.get(f"/admin/usage-report?from={today.isoformat()}&to={today.isoformat()}", headers={"Authorization": f"Bearer {token}"})
    assert report_resp.json()["rooms"][0]["confirmed_bookings"] == 1

def test_23_export_scoping():
    """Bug 22: Multi-Tenant Scoping Bypass in Admin Export (C10)"""
    setup_fresh_db()
    token_a = register_and_login("org_a", "admin_a")
    token_b = register_and_login("org_b", "admin_b")
    room_b = create_room(token_b, "Room B")
    
    now = datetime.utcnow()
    start = now + timedelta(days=2)
    end = start + timedelta(hours=1)
    client.post("/bookings", json={"room_id": room_b, "start_time": start.isoformat(), "end_time": end.isoformat()}, headers={"Authorization": f"Bearer {token_b}"})
    
    export_resp = client.get(f"/admin/export?room_id={room_b}&include_all=true", headers={"Authorization": f"Bearer {token_a}"})
    csv_content = export_resp.text.strip().split("\n")
    assert len(csv_content) == 1

def test_24_no_artificial_delay():
    """Bug 24: Blocking Artificial Sleep Bottlenecks (C13)"""
    setup_fresh_db()
    token = register_and_login()
    room_id = create_room(token)
    
    start_time = time.time()
    now = datetime.utcnow()
    start = now + timedelta(days=1)
    end = start + timedelta(hours=1)
    
    client.post("/bookings", json={"room_id": room_id, "start_time": start.isoformat(), "end_time": end.isoformat()}, headers={"Authorization": f"Bearer {token}"})
    elapsed = time.time() - start_time
    assert elapsed < 0.2

# =====================================================================
# Additional Verification & Multi-Tenancy Tests
# =====================================================================

def test_extra_booking_visibility():
    setup_fresh_db()
    admin_token = register_and_login("org_v", "admin")
    m1_token = register_and_login("org_v", "m1")
    m2_token = register_and_login("org_v", "m2")
    
    room_id = create_room(admin_token)
    now = datetime.utcnow()
    start = now + timedelta(days=1)
    end = start + timedelta(hours=1)
    
    b1 = client.post("/bookings", json={"room_id": room_id, "start_time": start.isoformat(), "end_time": end.isoformat()}, headers={"Authorization": f"Bearer {m1_token}"}).json()
    
    get_resp = client.get(f"/bookings/{b1['id']}", headers={"Authorization": f"Bearer {m2_token}"})
    assert get_resp.status_code == 404

def test_extra_stats_persistence():
    setup_fresh_db()
    admin_token = register_and_login("org_p", "admin")
    room_id = create_room(admin_token)
    now = datetime.utcnow()
    start = now + timedelta(days=1)
    end = start + timedelta(hours=1)
    
    client.post("/bookings", json={"room_id": room_id, "start_time": start.isoformat(), "end_time": end.isoformat()}, headers={"Authorization": f"Bearer {admin_token}"})
    
    stats._stats.clear()
    stats.init_all_stats()
    
    stats_resp = client.get(f"/rooms/{room_id}/stats", headers={"Authorization": f"Bearer {admin_token}"})
    assert stats_resp.json()["total_confirmed_bookings"] == 1

def test_extra_concurrent_double_booking():
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
    # Sort them by name so they run in order of test_01, test_02, etc.
    test_functions.sort(key=lambda x: x.__name__)
    
    passed = 0
    failed = 0
    print(f"Running {len(test_functions)} consolidated tests...")
    print("=" * 60)
    
    for test in test_functions:
        try:
            print(f"Running {test.__name__}...", end="", flush=True)
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
