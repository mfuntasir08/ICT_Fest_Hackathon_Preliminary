# 🚀 CoWork Hackathon — Solution Pipeline

## 📌 Problem Summary

This is a **bug-fix hackathon**. We are given a broken REST API for a multi-tenant coworking space booking system (**CoWork**). We must find and fix all bugs so the API conforms to the business rules (Sections 3–4) and the API contract (Section 5) in the problem statement. **No feature additions or refactors** — just targeted bug fixes.

**Stack:** Python 3.11, FastAPI, SQLAlchemy, SQLite  
**Grading:** Automated black-box — a grader hits the API and checks behavior against the spec.  
**Scoring:** Easy = 3 pts, Medium = 5 pts, Hard = 10 pts. Tie-break favors harder bugs + `bug_report.md`.

---

## 🐛 Complete Bug Inventory

After a thorough audit of every file, here are **all the bugs found**, grouped into three workstreams so three team members can work **simultaneously without merge conflicts**.

---

### 🔴 TEAM A — Auth, Token & Registration Bugs

**Files:** `app/auth.py`, `app/routers/auth.py`, `app/config.py`

#### Bug A1 — Access Token Expiry Way Too Long (Easy)
- **File:** `auth.py` Line 50
- **Bug:** `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES * 60)` → multiplies 15 minutes by 60, giving **900 minutes = 54,000 seconds**. The spec says access tokens expire in exactly **900 seconds** (15 minutes).
- **Fix:** Change to `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)` (remove the `* 60`).

#### Bug A2 — Token Revocation Checks `sub` Instead of `jti` (Medium)
- **File:** `auth.py` Line 97
- **Bug:** `if payload.get("sub") in _revoked_tokens` — this checks the **user ID** against the revoked set, but `revoke_access_token()` on L86 stores the **`jti`** (token ID). After logout, ALL tokens for that user would fail (if `sub` happened to match a `jti`), and the actual revoked token would NOT be blocked.
- **Fix:** Change to `if payload.get("jti") in _revoked_tokens`.

#### Bug A3 — Duplicate Username Returns Success Instead of 409 (Medium)
- **File:** `routers/auth.py` Lines 37-43
- **Bug:** When a user with the same username already exists in the org, the code **returns the existing user's info** with 200 instead of raising `409 USERNAME_TAKEN`.
- **Fix:** Replace the return block with: `raise AppError(409, "USERNAME_TAKEN", "Username already taken in this organization")`.

#### Bug A4 — Refresh Tokens Are Not Single-Use (Medium)
- **File:** `routers/auth.py` Lines 81-93
- **Bug:** The spec says "Refresh tokens are single-use: refreshing returns new tokens and invalidates the presented refresh token (reuse → 401)." The current code never records or checks whether a refresh token `jti` has been used.
- **Fix:** Maintain a `_used_refresh_tokens` set. On refresh, check if the token's `jti` is already used; if so → 401. Otherwise add it to the set, then issue new tokens.

---

### 🟡 TEAM B — Booking Logic, Pricing & Cancellation Bugs

**Files:** `app/routers/bookings.py`, `app/services/refunds.py`, `app/timeutils.py`

#### Bug B1 — Start-Time-in-Future Check Has 5-Minute Grace Window (Easy)
- **File:** `routers/bookings.py` Line 86
- **Bug:** `if start <= now - timedelta(seconds=300)` — this allows bookings up to 5 minutes in the past. Spec says "`start_time` must be **strictly in the future** at request time — no grace window."
- **Fix:** Change to `if start <= now:`.

#### Bug B2 — Missing Minimum Duration Check (Easy)
- **File:** `routers/bookings.py` Lines 89-94
- **Bug:** The code checks `duration_hours > MAX_DURATION_HOURS` but never checks `duration_hours < MIN_DURATION_HOURS` (minimum 1 hour). Also doesn't check `end > start` (which would catch zero or negative durations).
- **Fix:** Add check: `if end <= start: raise AppError(...)` and `if duration_hours < MIN_DURATION_HOURS: raise AppError(...)`.

#### Bug B3 — Overlap Detection Uses `<=` Instead of `<` (Easy)
- **File:** `routers/bookings.py` Line 50
- **Bug:** `if b.start_time <= end and start <= b.end_time` — uses `<=` which **rejects back-to-back bookings**. Spec says overlap iff `existing.start < new.end AND new.start < existing.end`. Back-to-back must be allowed.
- **Fix:** Change to `if b.start_time < end and start < b.end_time`.

#### Bug B4 — Cancellation Refund: <24h Notice Gives 50% Instead of 0% (Easy)
- **File:** `routers/bookings.py` Line 206
- **Bug:** The `else` branch (notice < 24 hours) sets `refund_percent = 50` instead of `0`. Spec: "notice < 24 hours → 0% refund."
- **Fix:** Change to `refund_percent = 0`.

#### Bug B5 — Cancellation Refund: Uses Integer Truncation Instead of Timedelta Comparison (Medium)
- **File:** `routers/bookings.py` Lines 200-201
- **Bug:** `notice_hours = int(notice.total_seconds() // 3600)` then `if notice_hours > 48` — this uses integer truncation. The spec says `> 48h → 100%`, but the code compares integer-truncated hours, not the actual timedelta. Should compare the timedelta directly.
- **Fix:** Compare `notice > timedelta(hours=48)` directly (timedelta comparison, not integer hours).

#### Bug B6 — Refund Rounding via Dollar-Conversion Loses Precision (Medium)
- **File:** `services/refunds.py` Lines 15-17
- **Bug:** Converts cents to dollars, applies percentage, converts back — `int(refund_dollars * 100)`. This uses `int()` (floor truncation) instead of rounding. Spec says "half-cents rounding up." Also the needless dollar conversion introduces floating-point drift.
- **Fix:** Use proper rounding: `amount_cents = round(booking.price_cents * percent / 100.0)` — or use `math.ceil` for half-up. Direct integer math avoids float issues.

> **Important:** The cancel endpoint AND the refund log must agree on the amount. Currently the cancel endpoint computes its own `refund_amount_cents` independently with `round()` while refunds.py uses `int()`. These can diverge.

#### Bug B7 — Timezone-Aware Input Datetimes Not Converted to UTC (Medium)
- **File:** `timeutils.py` Lines 12-13
- **Bug:** `dt = dt.replace(tzinfo=None)` — this **strips** the timezone without converting. A datetime like `2026-07-10T10:00:00+05:30` should become `2026-07-10T04:30:00` UTC, but this code keeps `10:00:00` and just drops the offset.
- **Fix:** Use `dt = dt.astimezone(timezone.utc).replace(tzinfo=None)` to **convert** then strip.

#### Bug B8 — GET /bookings/{id} Overwrites `start_time` with `created_at` (Easy)
- **File:** `routers/bookings.py` Line 166
- **Bug:** `response["start_time"] = iso_utc(booking.created_at)` — this replaces the booking's actual start_time with created_at. This is clearly wrong.
- **Fix:** Remove this line entirely (the serializer already sets `start_time` correctly).

---

### 🟢 TEAM C — Concurrency, Pagination, Caching, Stats & Liveness Bugs

**Files:** `app/routers/bookings.py` (pagination only), `app/services/notifications.py`, `app/services/stats.py`, `app/services/reference.py`, `app/services/ratelimit.py`, `app/cache.py`, `app/routers/rooms.py`, `app/routers/admin.py`, `app/services/export.py`

#### Bug C1 — Pagination: Wrong Sort Order (Easy)
- **File:** `routers/bookings.py` Line 137
- **Bug:** `.order_by(Booking.start_time.desc(), ...)` — uses **descending**. Spec says "ascending by start time (ties by ascending id)."
- **Fix:** Change to `.order_by(Booking.start_time.asc(), Booking.id.asc())`.

#### Bug C2 — Pagination: Wrong Offset Formula (Easy)
- **File:** `routers/bookings.py` Line 138
- **Bug:** `.offset(page * limit)` — for page 1, this skips `limit` items (page 1 shows items starting from index `limit`). Correct formula is `(page - 1) * limit`.
- **Fix:** Change to `.offset((page - 1) * limit)`.

#### Bug C3 — Pagination: Hardcoded `.limit(10)` Ignores User's Limit (Easy)
- **File:** `routers/bookings.py` Line 139
- **Bug:** `.limit(10)` is hardcoded instead of using the `limit` parameter.
- **Fix:** Change to `.limit(limit)`.

#### Bug C4 — Notification Deadlock: Lock Ordering Inversion (Hard)
- **File:** `services/notifications.py` Lines 24-35
- **Bug:** `notify_created` acquires `_email_lock` then `_audit_lock`. `notify_cancelled` acquires `_audit_lock` then `_email_lock`. This is a classic **ABBA deadlock** — if a create and cancel happen concurrently, they can deadlock and **hang the service** (violates Rule 16: Liveness).
- **Fix:** Use consistent lock ordering in both functions (always acquire `_email_lock` first, then `_audit_lock`), or use a single lock.

#### Bug C5 — Reference Code Race Condition (Hard)
- **File:** `services/reference.py` Lines 17-21
- **Bug:** Read-sleep-write pattern without any locking. Two concurrent requests can read the same counter value, sleep, then both increment to the same value — producing **duplicate reference codes** (violates Rule 7: uniqueness under concurrency).
- **Fix:** Add a `threading.Lock()` around the read-increment-return block.

#### Bug C6 — Stats Race Condition (Hard)
- **File:** `services/stats.py` Lines 15-26
- **Bug:** Read-sleep-write pattern without locking in both `record_create` and `record_cancel`. Under concurrent requests, stats can lose updates (Rule 14: "always consistent…including after bursts of concurrent activity").
- **Fix:** Add a `threading.Lock()` around each read-modify-write block.

#### Bug C7 — Rate Limiter Race Condition (Hard)
- **File:** `services/ratelimit.py` Lines 18-26
- **Bug:** Read-sleep-write on the bucket list without locking. Under concurrent requests from the same user, more than 20 requests can slip through the window (Rule 5: "Must hold under concurrent requests").
- **Fix:** Add a `threading.Lock()` around the read-modify-check block.

#### Bug C8 — Availability Cache Serves Stale Data (Medium)
- **File:** `routers/rooms.py` Lines 69-71
- **Bug:** The availability endpoint returns cached data. But Rule 13 says availability "must reflect the current state immediately." The cache is only invalidated on booking creation (not on cancellation), and even then, caching at all prevents "immediate" reflection.
- **Fix:** Remove the cache get/set for availability entirely (always query the DB), OR also invalidate on cancellation in `cancel_booking`.

#### Bug C9 — Usage Report Cache Serves Stale Data (Medium)
- **File:** `routers/admin.py` Lines 25-27
- **Bug:** Rule 12 says the usage report "Must reflect the current state immediately." Caching prevents this. The cache is invalidated on cancel but NOT on booking creation.
- **Fix:** Remove report caching entirely, OR also invalidate on booking creation in `create_booking`.

#### Bug C10 — Export with `include_all=True` and `room_id` Bypasses Org Scoping (Medium)
- **File:** `services/export.py` Lines 48-50
- **Bug:** When `include_all=True` and `room_id` is provided, `fetch_bookings_raw(db, room_id)` is called — this queries **all bookings for that room with no org filter**. An admin could export bookings for a room in another org (violates Rule 9: multi-tenancy).
- **Fix:** In `fetch_bookings_raw`, add an org_id filter, or use `_fetch_scoped` with `user_id=None` instead.

#### Bug C11 — Booking Create Not Invalidating Report Cache (Easy)
- **File:** `routers/bookings.py` Lines 120-122
- **Bug:** After creating a booking, the code invalidates availability cache but **does not** invalidate the report cache. New bookings won't appear in usage reports until the cache is cleared by a cancellation.
- **Fix:** Add `cache.invalidate_report(user.org_id)` after booking creation.

#### Bug C12 — Cancel Not Invalidating Availability Cache (Easy)
- **File:** `routers/bookings.py` Lines 216-218
- **Bug:** After cancelling a booking, the code invalidates the report cache but **does not** invalidate the availability cache. Cancelled bookings still show as "busy."
- **Fix:** Add `cache.invalidate_availability(booking.room_id, booking.start_time.date().isoformat())` after cancellation.

#### Bug C13 — Blocking `time.sleep()` Calls in Sync Endpoints (Hard)
- **File:** Multiple: `bookings.py` Lines 29,34,39, `stats.py` L12, `reference.py` L14, `ratelimit.py` L15, `notifications.py` L16,21
- **Bug:** Numerous `time.sleep()` calls throughout the codebase (0.1–0.12 seconds each) that cumulatively slow down requests. A single booking creation triggers multiple sleeps totaling ~0.5s+. Under load, this could cause timeouts and violates the **Liveness** rule (Rule 16). These are fake "simulation" pauses with no real purpose.
- **Fix:** Remove all `time.sleep()` calls (or reduce to near-zero). They're simulated operations that shouldn't block.

---

## 📊 Bug Summary Table

| Bug ID | File(s) | Difficulty | Team | Description |
|--------|---------|-----------|------|-------------|
| A1 | `auth.py` | Easy | A | Token expiry `* 60` overflow |
| A2 | `auth.py` | Medium | A | Revocation checks `sub` not `jti` |
| A3 | `routers/auth.py` | Medium | A | Duplicate username returns 200 not 409 |
| A4 | `routers/auth.py` | Medium | A | Refresh tokens not single-use |
| B1 | `routers/bookings.py` | Easy | B | 5-minute grace window on start_time |
| B2 | `routers/bookings.py` | Easy | B | Missing min duration & end>start check |
| B3 | `routers/bookings.py` | Easy | B | Overlap uses `<=` not `<` |
| B4 | `routers/bookings.py` | Easy | B | <24h refund → 50% instead of 0% |
| B5 | `routers/bookings.py` | Medium | B | Notice comparison via int truncation |
| B6 | `services/refunds.py` | Medium | B | Refund rounding `int()` not proper round |
| B7 | `timeutils.py` | Medium | B | TZ offset stripped, not converted |
| B8 | `routers/bookings.py` | Easy | B | `start_time` overwritten with `created_at` |
| C1 | `routers/bookings.py` | Easy | C | Pagination sort desc not asc |
| C2 | `routers/bookings.py` | Easy | C | Pagination offset `page*limit` |
| C3 | `routers/bookings.py` | Easy | C | Hardcoded `.limit(10)` |
| C4 | `services/notifications.py` | Hard | C | ABBA deadlock on locks |
| C5 | `services/reference.py` | Hard | C | Reference code race condition |
| C6 | `services/stats.py` | Hard | C | Stats TOCTOU race condition |
| C7 | `services/ratelimit.py` | Hard | C | Rate limiter race condition |
| C8 | `routers/rooms.py` | Medium | C | Availability cache stale data |
| C9 | `routers/admin.py` | Medium | C | Report cache stale data |
| C10 | `services/export.py` | Medium | C | Export bypasses org scope |
| C11 | `routers/bookings.py` | Easy | C | Create doesn't invalidate report cache |
| C12 | `routers/bookings.py` | Easy | C | Cancel doesn't invalidate availability cache |
| C13 | Multiple services | Hard | C | Blocking `time.sleep()` degrades liveness |

---

## 🏗️ Team Work Distribution

### 👤 Team Member A — Auth & Token System
**Priority: High (Medium-point bugs, foundational correctness)**

| Order | Bug | Points | Time Est. |
|-------|-----|--------|-----------|
| 1 | A1 — Token expiry | 3 | 2 min |
| 2 | A2 — Revocation `jti` check | 5 | 3 min |
| 3 | A3 — Duplicate username 409 | 5 | 3 min |
| 4 | A4 — Single-use refresh tokens | 5 | 10 min |
| **Total** | | **18 pts** | **~18 min** |

**Files touched:** `app/auth.py`, `app/routers/auth.py`

---

### 👤 Team Member B — Booking Logic & Business Rules
**Priority: High (Many easy wins + critical business logic)**

| Order | Bug | Points | Time Est. |
|-------|-----|--------|-----------|
| 1 | B4 — 0% refund for <24h | 3 | 1 min |
| 2 | B8 — Remove `start_time` overwrite | 3 | 1 min |
| 3 | B3 — Overlap `<` not `<=` | 3 | 1 min |
| 4 | B1 — Remove grace window | 3 | 2 min |
| 5 | B2 — Min duration + end>start | 3 | 3 min |
| 6 | B7 — Timezone conversion | 5 | 5 min |
| 7 | B5 — Notice timedelta comparison | 5 | 3 min |
| 8 | B6 — Refund rounding | 5 | 10 min |
| **Total** | | **30 pts** | **~26 min** |

**Files touched:** `app/routers/bookings.py` (create/cancel/get logic), `app/timeutils.py`, `app/services/refunds.py`

---

### 👤 Team Member C — Concurrency, Caching & Infrastructure
**Priority: Highest value (Hard bugs = 10 pts each)**

| Order | Bug | Points | Time Est. |
|-------|-----|--------|-----------|
| 1 | C1–C3 — Pagination (3 easy fixes) | 9 | 5 min |
| 2 | C4 — Deadlock fix | 10 | 5 min |
| 3 | C5 — Reference code lock | 10 | 5 min |
| 4 | C6 — Stats lock | 10 | 5 min |
| 5 | C7 — Rate limiter lock | 10 | 5 min |
| 6 | C8 — Availability cache | 5 | 5 min |
| 7 | C9 — Report cache | 5 | 3 min |
| 8 | C10 — Export org scope | 5 | 5 min |
| 9 | C11 — Create → invalidate report | 3 | 2 min |
| 10 | C12 — Cancel → invalidate avail | 3 | 2 min |
| 11 | C13 — Remove `time.sleep()` calls | 10 | 10 min |
| **Total** | | **80 pts** | **~52 min** |

**Files touched:** `app/routers/bookings.py` (pagination lines only), `app/services/notifications.py`, `app/services/reference.py`, `app/services/stats.py`, `app/services/ratelimit.py`, `app/routers/rooms.py`, `app/routers/admin.py`, `app/services/export.py`

---

## ⏱️ Execution Timeline (4-Hour Window)

### Phase 1: Setup (0:00 – 0:15)
- [ ] Fork repo & leave fork network
- [ ] Clone locally; set up Python venv / Docker
- [ ] Verify the app starts: `uvicorn app.main:app --reload`
- [ ] Quick smoke test: register → login → create room → create booking
- [ ] Each team member reads their bug group above

### Phase 2: Easy Bugs Blitz (0:15 – 1:00)
All three members fix their **Easy** bugs first (highest points-per-minute):
- **A:** A1 (token expiry)
- **B:** B1, B2, B3, B4, B8 (5 easy one-liners)
- **C:** C1, C2, C3, C11, C12 (pagination + cache invalidation)

**Checkpoint:** Commit & push. ~30 easy points locked in.

### Phase 3: Medium Bugs (1:00 – 2:00)
- **A:** A2, A3, A4 (token revocation, duplicate username, single-use refresh)
- **B:** B5, B6, B7 (notice comparison, refund rounding, timezone fix)
- **C:** C8, C9, C10 (stale caches, export org scoping)

**Checkpoint:** Commit & push. ~45 medium points locked in.

### Phase 4: Hard Bugs — Concurrency (2:00 – 3:00)
- **C** leads: C4 (deadlock), C5 (reference race), C6 (stats race), C7 (rate limit race), C13 (remove sleeps)
- **A & B** support with testing and reviewing concurrent scenarios
- Test with `curl` scripts running in parallel / Python `concurrent.futures`

**Checkpoint:** Commit & push. ~50 hard points locked in.

### Phase 5: Integration Testing & bug_report.md (3:00 – 3:45)
- [ ] Run full API test suite against all business rules
- [ ] Test concurrent booking creation (double-booking, quota, rate limit)
- [ ] Test concurrent cancellation (double-cancel)
- [ ] Test cross-org isolation (multi-tenancy)
- [ ] Test edge cases: back-to-back bookings, exactly 24h/48h notice, timezone offsets
- [ ] Write `bug_report.md` (tie-breaker!)

### Phase 6: Final Polish (3:45 – 4:00)
- [ ] Final commit & push
- [ ] Make repo public
- [ ] Submit URL via Google Form
- [ ] Verify the grader can reach the repo

---

## 🧪 Testing Checklist

### Auth Tests
```bash
# Register new org → should be admin
# Register same org → should be member
# Register duplicate username → should get 409 USERNAME_TAKEN
# Login → get tokens
# Use access token → works
# Logout → token invalidated
# Use same token after logout → 401
# Refresh → get new tokens
# Reuse old refresh token → 401
# Token expiry → verify 900 seconds
```

### Booking Tests
```bash
# Create booking with past start_time → 400
# Create booking with 0-hour or 9-hour duration → 400
# Create booking with end < start → 400
# Create booking with timezone offset → verify UTC conversion
# Create back-to-back bookings → should succeed
# Create overlapping booking → 409 ROOM_CONFLICT
# Create >3 bookings in 24h window → 409 QUOTA_EXCEEDED
# GET /bookings → verify ascending sort, correct pagination
# GET /bookings/{id} → verify start_time is correct (not created_at)
```

### Cancellation Tests
```bash
# Cancel with >48h notice → 100% refund
# Cancel with 24-48h notice → 50% refund
# Cancel with <24h notice → 0% refund
# Cancel already cancelled → 409 ALREADY_CANCELLED
# Verify refund amount in response matches RefundLog
```

### Concurrency Tests
```bash
# Simultaneous booking creates for same slot → only one succeeds
# Simultaneous cancels of same booking → only one succeeds
# >20 rapid POSTs → rate limited
# Verify reference codes unique under concurrent creation
# Verify stats consistent after concurrent creates/cancels
```

### Multi-Tenancy Tests
```bash
# User in org A cannot see org B's rooms
# User in org A cannot book org B's room (404)
# Admin export cannot leak other org's bookings
# Member cannot see other member's bookings (404)
```

---

## 📝 bug_report.md Template

Each team member should document their bugs in this format for the tie-breaker:

```markdown
## Bug [ID]: [Short Title]
- **File(s):** `path/to/file.py`, line(s) X–Y
- **What the bug was:** Description of the incorrect behavior
- **Why it caused incorrect behavior:** Link to the violated business rule
- **How it was fixed:** Brief code change description
```

---

## 🎯 Points Potential

| Difficulty | Count | Points Each | Subtotal |
|-----------|-------|-------------|----------|
| Easy | 11 | 3 | 33 |
| Medium | 10 | 5 | 50 |
| Hard | 5 | 10 | 50 |
| **TOTAL** | **26** | | **133** |

**Good luck, team! 🔥**
