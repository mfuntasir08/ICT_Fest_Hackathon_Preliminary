"""Tests for Team A bugs: A1, A2, A3, A4.

Run with: python tests/test_team_a.py
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from app.main import app
from app.database import Base, engine
from app.auth import decode_token
from app.routers import auth

client = TestClient(app)

_user_counter = 0

def setup_fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    # Clear token revocations and used refresh tokens
    from app import auth as app_auth
    app_auth._revoked_tokens.clear()
    app_auth._used_refresh_tokens.clear()

def get_unique_org_and_user():
    global _user_counter
    _user_counter += 1
    return f"org_{_user_counter}", f"user_{_user_counter}"

def test_a1_token_expiry():
    print("\n--- Testing A1: Access Token Expiry (Exactly 15 mins / 900s) ---")
    setup_fresh_db()
    org, username = get_unique_org_and_user()
    
    # Register & Login
    client.post("/auth/register", json={"org_name": org, "username": username, "password": "pw"})
    resp = client.post("/auth/login", json={"org_name": org, "username": username, "password": "pw"})
    assert resp.status_code == 200
    
    access_token = resp.json()["access_token"]
    payload = decode_token(access_token)
    
    duration = payload["exp"] - payload["iat"]
    print(f"Token duration: {duration} seconds")
    assert duration == 900, f"Expected 900 seconds, got {duration}"
    print("[PASS] A1: Token expiry is exactly 900 seconds")

def test_a2_token_revocation_jti():
    print("\n--- Testing A2: Token Revocation by JTI (not SUB) ---")
    setup_fresh_db()
    org, user1 = get_unique_org_and_user()
    user2 = f"{user1}_other"
    
    # Register two users in the same org
    client.post("/auth/register", json={"org_name": org, "username": user1, "password": "pw"})
    client.post("/auth/register", json={"org_name": org, "username": user2, "password": "pw"})
    
    # Login user1 (twice to get two active tokens)
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
    
    # Try using revoked token1a -> should fail 401
    resp = client.get("/rooms", headers={"Authorization": f"Bearer {token1a}"})
    assert resp.status_code == 401
    
    # Try using token1b (same user, different JTI) -> should succeed
    resp = client.get("/rooms", headers={"Authorization": f"Bearer {token1b}"})
    assert resp.status_code == 200
    
    # Try using token2 (different user) -> should succeed
    resp = client.get("/rooms", headers={"Authorization": f"Bearer {token2}"})
    assert resp.status_code == 200
    
    print("[PASS] A2: Revocation checks JTI, other tokens/users unaffected")

def test_a3_duplicate_username():
    print("\n--- Testing A3: Duplicate Username Registration (409) ---")
    setup_fresh_db()
    org1, user1 = get_unique_org_and_user()
    org2, _ = get_unique_org_and_user()
    
    # Register user1 in org1
    resp = client.post("/auth/register", json={"org_name": org1, "username": user1, "password": "pw"})
    assert resp.status_code == 201
    
    # Register duplicate user1 in org1 -> should fail 409
    resp = client.post("/auth/register", json={"org_name": org1, "username": user1, "password": "pw"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Username already taken in this organization"
    
    # Register user1 in org2 (different org) -> should succeed 201
    resp = client.post("/auth/register", json={"org_name": org2, "username": user1, "password": "pw"})
    assert resp.status_code == 201
    
    print("[PASS] A3: Duplicate username in same org rejected with 409, allowed across different orgs")

def test_a4_refresh_tokens_single_use():
    print("\n--- Testing A4: Single-Use Refresh Tokens (Reuse -> 401) ---")
    setup_fresh_db()
    org, username = get_unique_org_and_user()
    
    client.post("/auth/register", json={"org_name": org, "username": username, "password": "pw"})
    login = client.post("/auth/login", json={"org_name": org, "username": username, "password": "pw"}).json()
    refresh_token = login["refresh_token"]
    
    # Refresh once -> should succeed
    refresh1 = client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh1.status_code == 200
    
    # Refresh again with same old refresh token -> should fail 401
    refresh2 = client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh2.status_code == 401
    
    print("[PASS] A4: Refresh token is single-use and reuse yields 401")

if __name__ == "__main__":
    print("=" * 60)
    print("TEAM A -- BUG FIX TESTS")
    print("=" * 60)
    
    tests = [
        test_a1_token_expiry,
        test_a2_token_revocation_jti,
        test_a3_duplicate_username,
        test_a4_refresh_tokens_single_use,
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
