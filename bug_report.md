# CoWork Hackathon -- Bug Report

This document lists all the bugs identified and fixed in the CoWork codebase during the preliminary round of the Agentic AI Hackathon.

---

## 🔴 TEAM A -- Auth, Token & Registration Bugs

### Bug A1: Access Token Expiry Calculation Overflow
- **File:** [app/auth.py](file:///c:/Users/Sowmik/Documents/GitHub/ICT_Fest_Hackathon_Preliminary/app/auth.py), line 50
- **What the bug was:** The `lifetime` duration for access tokens was calculated as `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES * 60)`. Because `ACCESS_TOKEN_EXPIRE_MINUTES = 15`, this resulted in an expiry window of **900 minutes** (15 hours) instead of the 15 minutes (900 seconds) required by the specification.
- **Why it caused incorrect behavior:** Access tokens remained valid for 15 hours, violating Business Rule 8 ("Access tokens expire in exactly 900 seconds").
- **How it was fixed:** Removed the `* 60` multiplier to set `lifetime = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)`.

### Bug A2: Logout Token Revocation Checking `sub` Instead of `jti`
- **File:** [app/auth.py](file:///c:/Users/Sowmik/Documents/GitHub/ICT_Fest_Hackathon_Preliminary/app/auth.py), line 97
- **What the bug was:** When revoking access tokens during logout, the token's `jti` (unique identifier) was added to `_revoked_tokens`. However, the middleware checked if the user's `sub` (user ID) was in `_revoked_tokens`.
- **Why it caused incorrect behavior:** Revoking a token for a user would either fail to block the revoked token or potentially invalidate all tokens for that user if a JTI matched a user ID.
- **How it was fixed:** Changed the check in `get_token_payload` to verify if the token's `jti` is present in the `_revoked_tokens` set.

### Bug A3: Duplicate Username Allowed in Same Org (Returned 200 instead of 409)
- **File:** [app/routers/auth.py](file:///c:/Users/Sowmik/Documents/GitHub/ICT_Fest_Hackathon_Preliminary/app/routers/auth.py), lines 37-43
- **What the bug was:** When registering a new user, if a user with the same username already existed in the same organization, the endpoint returned the existing user details with a status code of `200 OK`.
- **Why it caused incorrect behavior:** It allowed silent duplicate registrations and violated Business Rule 15 ("A duplicate username within the org $\rightarrow$ `409 USERNAME_TAKEN`").
- **How it was fixed:** Replaced the return block with `raise AppError(409, "USERNAME_TAKEN", "Username already taken in this organization")`.

### Bug A4: Refresh Tokens Are Not Single-Use (Re-use Vulnerability)
- **File:** [app/auth.py](file:///c:/Users/Sowmik/Documents/GitHub/ICT_Fest_Hackathon_Preliminary/app/auth.py) & [app/routers/auth.py](file:///c:/Users/Sowmik/Documents/GitHub/ICT_Fest_Hackathon_Preliminary/app/routers/auth.py)
- **What the bug was:** The `/auth/refresh` endpoint did not track whether a refresh token had already been used.
- **Why it caused incorrect behavior:** It allowed a refresh token to be reused multiple times, violating Business Rule 8 ("Refresh tokens are single-use: refreshing returns a new access and refresh token and invalidates the presented refresh token").
- **How it was fixed:** Introduced a thread-safe `_used_refresh_tokens` set in `app/auth.py` and a `check_and_use_refresh_token` helper function to verify token uniqueness and reject reuse attempts with a `401 Unauthorized` error.

---

## 🟡 TEAM B -- Booking Logic, Pricing & Cancellation Bugs

### Bug B1: Start Time Grace Window Violation
- **File:** [app/routers/bookings.py](file:///c:/Users/Sowmik/Documents/GitHub/ICT_Fest_Hackathon_Preliminary/app/routers/bookings.py), line 92
- **What the bug was:** The start time validation checked `if start <= now - timedelta(seconds=300)`.
- **Why it caused incorrect behavior:** This allowed bookings starting up to 5 minutes in the past, violating Business Rule 2 ("start_time must be strictly in the future at request time - no grace window").
- **How it was fixed:** Corrected the check to `if start <= now:`.

### Bug B2: Missing Minimum Booking Duration Validation
- **File:** [app/routers/bookings.py](file:///c:/Users/Sowmik/Documents/GitHub/ICT_Fest_Hackathon_Preliminary/app/routers/bookings.py), lines 95-106
- **What the bug was:** The endpoint only checked if `duration_hours > MAX_DURATION_HOURS`. It did not check for negative durations (i.e. `end_time <= start_time`) or if the duration was less than 1 hour.
- **Why it caused incorrect behavior:** Users could make bookings with negative durations or 0-hour durations, violating Business Rule 2 ("Duration must be a whole number of hours, minimum 1, maximum 8").
- **How it was fixed:** Added a check `if end <= start` and verified `duration_hours < MIN_DURATION_HOURS`, raising an `INVALID_BOOKING_WINDOW` error for violations.

### Bug B3: Booking Overlap Check Rejects Back-to-Back Bookings
- **File:** [app/routers/bookings.py](file:///c:/Users/Sowmik/Documents/GitHub/ICT_Fest_Hackathon_Preliminary/app/routers/bookings.py), line 55
- **What the bug was:** Overlap checking used `<=` and `>=` comparisons: `if b.start_time <= end and start <= b.end_time`.
- **Why it caused incorrect behavior:** It blocked back-to-back bookings (e.g., one ending at 2:00 PM and another starting at 2:00 PM). Business Rule 3 explicitly states: "Back-to-back bookings are allowed."
- **How it was fixed:** Changed the comparison operators to strictly `<`: `if b.start_time < end and start < b.end_time`.

### Bug B4: Cancellation Notice <24h Incorrectly Refunding 50%
- **File:** [app/routers/bookings.py](file:///c:/Users/Sowmik/Documents/GitHub/ICT_Fest_Hackathon_Preliminary/app/routers/bookings.py), line 231
- **What the bug was:** The cancellation notice conditional fell back to `refund_percent = 50` when notice was under 24 hours.
- **Why it caused incorrect behavior:** It incorrectly refunded bookings cancelled within 24 hours of start time, violating Business Rule 6 ("notice < 24 hours $\rightarrow$ 0% refund").
- **How it was fixed:** Changed the fallback refund percentage to `0`.

### Bug B5: Cancellation notice duration uses integer truncation
- **File:** [app/routers/bookings.py](file:///c:/Users/Sowmik/Documents/GitHub/ICT_Fest_Hackathon_Preliminary/app/routers/bookings.py), lines 225-231
- **What the bug was:** The cancellation logic used truncated integer division `int(notice.total_seconds() // 3600)` to calculate the notice period in hours.
- **Why it caused incorrect behavior:** An exact comparison against 48 and 24 hours was not possible due to truncation (e.g. 47 hours and 59 minutes would evaluate incorrectly).
- **How it was fixed:** Replaced the integer calculations with direct `timedelta` comparisons: `if notice > timedelta(hours=48)` and `elif notice >= timedelta(hours=24)`.

### Bug B6: Precision Loss in Refund Rounding via Dollar Conversion
- **File:** [app/services/refunds.py](file:///c:/Users/Sowmik/Documents/GitHub/ICT_Fest_Hackathon_Preliminary/app/services/refunds.py) and [app/routers/bookings.py](file:///c:/Users/Sowmik/Documents/GitHub/ICT_Fest_Hackathon_Preliminary/app/routers/bookings.py)
- **What the bug was:** Cents were converted to dollars (floats), the percentage was applied, and the result was cast back to integer cents using `int(refund_dollars * 100)`, which performs floor truncation.
- **Why it caused incorrect behavior:** Floor truncation caused precision loss (violating Business Rule 6: "Refund amount rounds to the nearest cent, half-cents rounding up"). It also caused a mismatch between the RefundLog and the API response.
- **How it was fixed:** Rewrote calculations to use direct integer math in cents and round half-cents up correctly: `int(booking.price_cents * (percent / 100.0) + 0.5)`.

### Bug B7: Timezone Offset Stripped Instead of Converted
- **File:** [app/timeutils.py](file:///c:/Users/Sowmik/Documents/GitHub/ICT_Fest_Hackathon_Preliminary/app/timeutils.py)
- **What the bug was:** Timezone offsets in ISO datetimes were stripped with `.replace(tzinfo=None)` without converting the actual time to UTC first.
- **Why it caused incorrect behavior:** A time like `10:00:00+05:30` incorrectly stored as `10:00:00` UTC instead of converting to `04:30:00` UTC first, violating Business Rule 1.
- **How it was fixed:** Added conversion to UTC before stripping the tzinfo offset: `dt = dt.astimezone(timezone.utc).replace(tzinfo=None)`.

### Bug B8: Booking Detail Overwrites `start_time` with `created_at`
- **File:** [app/routers/bookings.py](file:///c:/Users/Sowmik/Documents/GitHub/ICT_Fest_Hackathon_Preliminary/app/routers/bookings.py)
- **What the bug was:** The `/bookings/{id}` detail route explicitly overrode `response["start_time"] = iso_utc(booking.created_at)`.
- **Why it caused incorrect behavior:** It corrupted the start time in the detail view response, presenting the creation timestamp as the booking start time.
- **How it was fixed:** Removed the redundant line since `serialize_booking` already correctly formats and includes the start time.

---

## 🟢 TEAM C -- Concurrency, Pagination, Caching & Liveness Bugs

### Bug C1: Pagination Incorrect Sort Direction
- **File:** [app/routers/bookings.py](file:///c:/Users/Sowmik/Documents/GitHub/ICT_Fest_Hackathon_Preliminary/app/routers/bookings.py), line 149
- **What the bug was:** Bookings pagination queried with `.order_by(Booking.start_time.desc(), ...)` which is descending order.
- **Why it caused incorrect behavior:** Violated Business Rule 11 ("sorted ascending by start time").
- **How it was fixed:** Switched to `.order_by(Booking.start_time.asc(), Booking.id.asc())`.

### Bug C2: Pagination Wrong Offset Formula
- **File:** [app/routers/bookings.py](file:///c:/Users/Sowmik/Documents/GitHub/ICT_Fest_Hackathon_Preliminary/app/routers/bookings.py), line 150
- **What the bug was:** Pagination used `.offset(page * limit)`.
- **Why it caused incorrect behavior:** For page 1, this skipped the first `limit` items, causing page 1 to show page 2's results.
- **How it was fixed:** Corrected the formula to `.offset((page - 1) * limit)`.

### Bug C3: Pagination Ignores Limit Parameter
- **File:** [app/routers/bookings.py](file:///c:/Users/Sowmik/Documents/GitHub/ICT_Fest_Hackathon_Preliminary/app/routers/bookings.py), line 151
- **What the bug was:** The database query had a hardcoded limit of `.limit(10)`.
- **Why it caused incorrect behavior:** The user-provided `limit` query parameter was ignored.
- **How it was fixed:** Changed to `.limit(limit)`.

### Bug C4: Lock Inversion Deadlock in Notifications
- **File:** [app/services/notifications.py](file:///c:/Users/Sowmik/Documents/GitHub/ICT_Fest_Hackathon_Preliminary/app/services/notifications.py)
- **What the bug was:** `notify_created` acquired `_email_lock` then `_audit_lock`. `notify_cancelled` acquired `_audit_lock` then `_email_lock`.
- **Why it caused incorrect behavior:** This created an ABBA lock ordering inversion. Under concurrent create/cancel actions, the application could deadlock and hang, violating Business Rule 16.
- **How it was fixed:** Decoupled lock acquisition so locks are acquired and released sequentially without nesting.

### Bug C5: Reference Code Generation Race Condition
- **File:** [app/services/reference.py](file:///c:/Users/Sowmik/Documents/GitHub/ICT_Fest_Hackathon_Preliminary/app/services/reference.py)
- **What the bug was:** The next reference code was read, paused with `time.sleep()`, and then written back without any locking protection.
- **Why it caused incorrect behavior:** Concurrent requests could retrieve and assign duplicate reference codes, violating Business Rule 7.
- **How it was fixed:** Wrapped the read-increment-return transaction block with a `threading.Lock()`.

### Bug C6: incremental Stats Counter Race Condition
- **File:** [app/services/stats.py](file:///c:/Users/Sowmik/Documents/GitHub/ICT_Fest_Hackathon_Preliminary/app/services/stats.py)
- **What the bug was:** Read-sleep-write operations in stats tracking were unprotected by synchronization locks.
- **Why it caused incorrect behavior:** Under concurrent requests, statistic updates could be lost, violating Business Rule 14.
- **How it was fixed:** Wrapped the `record_create`, `record_cancel`, and `get` operations in a `threading.Lock()`.

### Bug C7: Rate Limiter Race Condition
- **File:** [app/services/ratelimit.py](file:///c:/Users/Sowmik/Documents/GitHub/ICT_Fest_Hackathon_Preliminary/app/services/ratelimit.py)
- **What the bug was:** Rolling-window checking and updating was not synchronized.
- **Why it caused incorrect behavior:** Multiple concurrent requests from a single user could bypass rate limits, violating Business Rule 5.
- **How it was fixed:** Protected the rolling window list logic within a `threading.Lock()`.

### Bugs C8 & C9: Stale Data Cache Vulnerability
- **Files:** [app/routers/rooms.py](file:///c:/Users/Sowmik/Documents/GitHub/ICT_Fest_Hackathon_Preliminary/app/routers/rooms.py) and [app/routers/admin.py](file:///c:/Users/Sowmik/Documents/GitHub/ICT_Fest_Hackathon_Preliminary/app/routers/admin.py)
- **What the bug was:** Caching was used on the room availability and usage report endpoints, but data was not invalidated reliably across all lifecycle transitions (e.g. creations did not clear the report cache).
- **Why it caused incorrect behavior:** Stale data was served, violating Business Rules 12 & 13 ("Must reflect the current state immediately").
- **How it was fixed:** Caching was completely removed for these endpoints to ensure every query fetches fresh data directly from the database.

### Bug C10: Multi-Tenant Scoping Bypass in Admin Export
- **File:** [app/services/export.py](file:///c:/Users/Sowmik/Documents/GitHub/ICT_Fest_Hackathon_Preliminary/app/services/export.py)
- **What the bug was:** In the export endpoint, when `include_all=True` and a `room_id` was specified, it queried the database using `fetch_bookings_raw(db, room_id)` which bypassed the caller's organizational scoping.
- **Why it caused incorrect behavior:** An administrator from Organization A could export private booking data from rooms belonging to Organization B, violating Business Rule 9 ("A user... may only ever read or act on data belonging to their own organization").
- **How it was fixed:** Rewrote `generate_export` so it always routes query filtering through `_fetch_scoped(db, org_id, user_id, room_id)` with `org_id` securely enforced.

### Bug C11 & C12: Missing Cache Invalidation
- **File:** [app/routers/bookings.py](file:///c:/Users/Sowmik/Documents/GitHub/ICT_Fest_Hackathon_Preliminary/app/routers/bookings.py)
- **What the bug was:** Booking creation did not invalidate report cache (C11), and cancellation did not invalidate availability cache (C12).
- **Why it caused incorrect behavior:** Caused reports/availability views to remain stale until cache expiration.
- **How it was fixed:** Added cache invalidation calls (note: superseded by Bug C8/C9 fixes where caches were removed entirely).

### Bug C13: Blocking Artificial Sleep Bottlenecks
- **File:** Multiple services and routers
- **What the bug was:** The code contained numerous artificial `time.sleep(0.1)` and `time.sleep(0.12)` pauses simulated to mimic latency.
- **Why it caused incorrect behavior:** Severely degraded server response rates, causing concurrency pools to block and violate Business Rule 16 ("no combination of concurrent valid requests may hang the service").
- **How it was fixed:** Removed all artificial `time.sleep()` calls from the application logic.
