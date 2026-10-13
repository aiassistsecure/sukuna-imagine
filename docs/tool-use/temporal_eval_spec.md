# Temporal Evaluation Specification: Fixed-Clock Fixtures

Project Imagine, next-gen track (temporal grounding + native tool use).
Status: design only. Imagine v12 is frozen; no training runs, no corpus changes in this assignment.

## 1. Purpose and scope

This spec defines how temporal grounding is evaluated for the Imagine model family.
It covers calendar semantics, rolling windows, boundary conditions, database time
types, time-zone grounding, and event time vs transaction time. Every fixture pins
the clock: the model under test never sees the real wall clock. "Now" reaches the
model only through two declared channels: SQL time functions (`CURRENT_DATE`,
`CURRENT_TIMESTAMP`, `date_trunc`, `INTERVAL`) evaluated under a harness-pinned
session time zone, and the `clock.now` tool.

Temporal grounding is primarily a SQL-generation capability, not a tool-use
capability. Relative-time references ("yesterday", "last month", "trailing 30 days")
must be compiled to `date_trunc`/`INTERVAL` predicates inside the `<<<SQL>>>`
envelope. `clock.now` is reserved for requests where the current instant itself is
the answer (or must be stamped by the host), never for date arithmetic the database
can do. Fixtures T-13 and T-14 enforce this split.

## 2. Normative conventions

### 2.1 Envelopes

| Envelope | Use |
|---|---|
| `<<<SQL>>>` ... `<<<END>>>` | A SQL statement the model proposes. Exactly one statement. |
| `<<<TOOL_CALL>>>` ... `<<<END>>>` | A JSON payload `{"name": ..., "arguments": {...}}` invoking one V1 tool. |
| `<<<CLARIFY>>>` ... `<<<END>>>` | A question back to the user when the request is ambiguous or unanswerable from available schema. |
| `<<<UNANSWERABLE>>>` ... `<<<END>>>` | A refusal when the request cannot be answered from the authorized schema at all. |

### 2.2 Harness contract

Each fixture declares:

- `now_utc`: an ISO-8601 UTC timestamp. The harness freezes the database clock to
  this instant (statement timestamp, `now()`, `CURRENT_TIMESTAMP` all resolve to it).
- `session_zone`: an IANA time-zone name. The harness runs `SET TIME ZONE` to this
  zone before executing candidate SQL, so `CURRENT_DATE` and casts from
  `timestamptz` to local time resolve in this zone.
- `schema`: the authorized tables and columns. The model must not reference any
  identifier outside this schema.

The model under test is evaluated with no access to the real wall clock except via
the `clock.now` tool. Any calendar literal in a candidate answer that cannot be
derived from the user input or from `clock.now` output counts as a hallucinated date.

### 2.3 Time model

- Storage is UTC. `TIMESTAMPTZ` columns hold absolute instants.
- Calendar language ("yesterday", "last month", "today") is interpreted in the
  effective zone: the explicit user zone when the request supplies one, otherwise
  the business-local zone declared by the fixture.
- All range predicates use half-open intervals `[start, end)`: `>= start AND < end`.
  Inclusive upper bounds (`<= end_of_day`) are rejected; they mishandle fractional
  seconds and DST edges.
- `DATE` columns compare with `DATE` literals. `TIMESTAMPTZ` columns compare with
  `timestamptz` bounds derived from `CURRENT_DATE`/`CURRENT_TIMESTAMP` arithmetic
  in the session zone.

## 3. Fixed clock fixtures

| ID | `now_utc` | Session zone | Local wall time | Why this instant |
|---|---|---|---|---|
| N1 | 2026-10-13T14:00:00Z | America/New_York | 2026-10-13 10:00 EDT (Tue) | Ordinary business day; baseline for yesterday/last-month/quarter/rolling fixtures |
| N2 | 2026-01-15T08:00:00Z | America/New_York | 2026-01-15 03:00 EST (Thu) | Winter (EST); early-morning local time |
| N3 | 2024-03-01T05:00:00Z | America/New_York | 2024-03-01 00:00 EST (Fri) | Local midnight immediately after leap day 2024-02-29 |
| N4 | 2026-03-08T07:30:00Z | America/New_York | 2026-03-08 03:30 EDT (Sun) | Spring forward: local 02:00-02:59 never existed this day (23-hour day) |
| N5 | 2026-11-01T05:30:00Z | America/New_York | 2026-11-01 01:30 EDT (Sun) | Fall back: the 01:00 hour occurs twice; this is the first occurrence |
| N6 | 2027-01-01T04:59:59Z | America/New_York | 2026-12-31 23:59:59 EST (Thu) | UTC date (2027-01-01) differs from local date (2026-12-31) |
| N7 | 2026-07-01T03:30:00Z | America/New_York | 2026-06-30 23:30 EDT (Tue) | UTC quarter (Q3) differs from local quarter (Q2) |
| N8 | 2026-10-13T23:30:00Z | Pacific/Auckland | 2026-10-14 12:30 NZDT (Wed) | Explicit user zone; local date (2026-10-14) differs from business zone date (2026-10-13) |

DST facts used above: US daylight saving time begins 2026-03-08 (clocks 02:00 EST
to 03:00 EDT) and ends 2026-11-01 (clocks 02:00 EDT back to 01:00 EST). 2024 is a
leap year. New Zealand observes NZDT (UTC+13) on 2026-10-13.

## 4. Calendar semantics

"Calendar" references resolve to whole calendar periods in the effective zone:

- **yesterday**: `[CURRENT_DATE - 1 day, CURRENT_DATE)`.
- **previous week**: the Monday-to-Sunday week before the current one:
  `[date_trunc('week', CURRENT_DATE) - 1 week, date_trunc('week', CURRENT_DATE))`.
  Weeks start Monday (`date_trunc('week', ...)`).
- **last month**: `[date_trunc('month', CURRENT_DATE) - 1 month, date_trunc('month', CURRENT_DATE))`.
- **quarter to date**: `[date_trunc('quarter', CURRENT_DATE), CURRENT_DATE + 1 day)`.
- **year to date**: `[date_trunc('year', CURRENT_DATE), CURRENT_DATE + 1 day)`.
- **previous year**: `[date_trunc('year', CURRENT_DATE) - 1 year, date_trunc('year', CURRENT_DATE))`.

The model must derive these from `CURRENT_DATE` arithmetic. Writing a literal such
as `DATE '2026-10-12'` for "yesterday" is a date hallucination and fails the fixture,
even when the literal happens to be correct: the literal is not robust to clock
changes and proves nothing about grounding. Literals are permitted only when the
user supplies the calendar reference explicitly ("February 2024", "on September
15"), in which case the model materializes exactly the named period.

## 5. Rolling windows

"Rolling" references resolve to absolute-time windows ending at the current instant:

- **past 24 hours**: `created_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours'`.
- **last 7 days**: `created_at >= CURRENT_TIMESTAMP - INTERVAL '7 days'`.
- **trailing 30 days**: `created_at >= CURRENT_TIMESTAMP - INTERVAL '30 days'`.
- **trailing 12 months**: `created_at >= CURRENT_TIMESTAMP - INTERVAL '12 months'`.

On `timestamptz`, `INTERVAL` subtraction is absolute-time arithmetic: a "past 24
hours" window on spring-forward day (N4) spans exactly 24 absolute hours even though
the local day is 23 hours long. The model must not hand-roll wall-clock math
(subtracting from local hour/minute fields) and must not emit local-time literals
spanning a transition. Rolling windows have no upper bound (or equivalently
`<= CURRENT_TIMESTAMP`); they must not be confused with calendar periods
("trailing 30 days" is not "this month").

## 6. Boundary conditions

- **Midnight**: the instant `00:00:00` belongs to the day that starts then.
  Half-open intervals handle this by construction: a row at exactly
  `2026-10-12 00:00:00-04` is in Oct 12, not Oct 11.
- **Inclusive/exclusive endpoints**: ranges are `[start, end)`. A row at the exact
  end instant is excluded. Fixtures seed rows at `23:59:59` and at the following
  `00:00:00` to verify the split.
- **Leap years**: February of a leap year has 29 days. The model must not assume 28.
  Fixture T-06 seeds a row on 2024-02-29 and requires its inclusion.
- **Month-end**: "last month" for an October clock is September (30 days); the
  bound arithmetic must not hard-code 30/31.
- **Year-end**: N6 separates UTC date from local date; the local calendar wins.
- **DST spring forward (N4)**: local 2026-03-08 02:00 through 02:59:59 does not
  exist. Any candidate SQL containing such a literal fails. Absolute `INTERVAL`
  arithmetic on `timestamptz` is the only accepted form.
- **DST fall back (N5)**: local 01:00 through 01:59:59 occurs twice. A request for
  "orders between 1:00 AM and 2:00 AM on November 1st" is ambiguous and must
  produce `<<<CLARIFY>>>`, never a guessed offset. Fixture T-05.

## 7. Database types: DATE, TIMESTAMP, TIMESTAMPTZ

- **DATE** (`order_date`): compare with `DATE` values. "Orders from October 12"
  with an `order_date DATE` column is `WHERE order_date = DATE '2026-10-12'`.
- **TIMESTAMP** (no zone): compare with `timestamp` bounds. The fixture schema
  declares the assumed zone in that case; the model uses session-zone arithmetic
  and does not attach offsets itself.
- **TIMESTAMPTZ**: compare with `timestamptz` bounds. `CURRENT_DATE`/`CURRENT_TIMESTAMP`
  arithmetic in the pinned session zone produces correct bounds; explicit
  `AT TIME ZONE` conversions are accepted but not required when the harness pins
  the session zone.

When both a `DATE` column and a `TIMESTAMPTZ` column could satisfy the request,
prefer the `DATE` column for whole-day predicates (fixture T-12). When only a
`TIMESTAMPTZ` column exists, use half-open `timestamptz` bounds, never
`::date` casts on the column (casts defeat indexes and misbehave at zone edges).

## 8. Time-zone grounding

Four zones matter; the spec fixes their precedence:

1. **Explicit user zone**: when the request names a zone ("in Auckland time",
   "for our London office"), it wins. Fixture T-09.
2. **Business-local zone**: the fixture's declared business zone (America/New_York
   unless stated). Calendar language resolves here.
3. **DB session zone**: pinned by the harness to the effective zone (1 or 2).
   Candidate SQL is written against this pin: plain `CURRENT_DATE`, no hard-coded
   offsets.
4. **UTC storage**: `timestamptz` values compare correctly against session-zone
   bounds without manual offset math.

Anti-patterns that fail tz fixtures: converting with a hard-coded `-04:00` or
`-05:00` offset (breaks across DST), filtering on the UTC calendar date when the
request is local (N6, N7), and using `clock.now` output as a SQL literal instead
of `CURRENT_DATE` arithmetic.

## 9. Event time vs transaction time (advanced tier)

Tables with temporal provenance carry two clocks:

- **Event time** (`valid_from`, `valid_to`): when the fact was true in the world.
- **Transaction time** (`recorded_at`): when the database learned it.

"As of" questions ("what was the address on September 15") are event-time
questions and must predicate on `valid_from`/`valid_to`, never on `recorded_at`.
A backdated correction (recorded in October, effective in August) must be visible
to an as-of-September query and invisible to an as-of-July query. Fixture T-15.

Lineage questions ("who changed this, and in which transaction") are not SQL
questions at all: they route to the `nedb.provenance` tool. See the tool-use
curriculum taxonomy.

## 10. Golden example

Input: "Show last month's completed orders."

```sql
<<<SQL>>>
SELECT *
FROM orders
WHERE status = 'completed'
  AND placed_at >= date_trunc('month', CURRENT_DATE) - INTERVAL '1 month'
  AND placed_at < date_trunc('month', CURRENT_DATE);
<<<END>>>
```

At clock N1 this resolves to `[2026-09-01 00:00:00-04, 2026-10-01 00:00:00-04)`:
September 1 through September 30 inclusive, October 1 excluded. The lower bound is
inclusive, the upper bound exclusive, both derived from `CURRENT_DATE` with no
invented literals.

## 11. Fixture cases

Each case lists input, clock, session zone, schema, expected output, the seed edge
rows the harness plants, and the anti-patterns that fail.

### T-01: yesterday (calendar)

- Input: "How many orders were placed yesterday?"
- Clock: N1. Session zone: America/New_York.
- Schema: `orders(id integer, placed_at timestamptz, total numeric)`.
- Expected:
```sql
<<<SQL>>>
SELECT COUNT(*)
FROM orders
WHERE placed_at >= CURRENT_DATE - INTERVAL '1 day'
  AND placed_at < CURRENT_DATE;
<<<END>>>
```
- Seed edge rows: `2026-10-11 23:59:59-04` (excluded), `2026-10-12 00:00:00-04`
  (included), `2026-10-12 23:59:59-04` (included), `2026-10-13 00:00:00-04` (excluded).
- Tests: calendar-period accuracy, midnight boundary.
- Anti-pattern: literal `DATE '2026-10-12'`; `placed_at::date = CURRENT_DATE - 1`.

### T-02: last month (golden)

- Input: "Show last month's completed orders."
- Clock: N1. Session zone: America/New_York.
- Schema: `orders(id integer, placed_at timestamptz, status text, total numeric)`.
- Expected: the golden SQL from section 10.
- Seed edge rows: `2026-08-31 23:59:59-04` (excluded), `2026-09-01 00:00:00-04`
  (included), `2026-09-30 23:59:59-04` (included), `2026-10-01 00:00:00-04`
  (excluded); one September row with `status = 'cancelled'` (excluded by filter).
- Tests: calendar-period accuracy, month-end boundary, filter conjunction.
- Anti-pattern: `INTERVAL '30 days'` for a calendar month; `BETWEEN` with an
  inclusive upper bound.

### T-03: quarter to date

- Input: "What is our revenue quarter to date?"
- Clock: N1. Session zone: America/New_York.
- Schema: `orders(id integer, placed_at timestamptz, total numeric)`.
- Expected:
```sql
<<<SQL>>>
SELECT SUM(total)
FROM orders
WHERE placed_at >= date_trunc('quarter', CURRENT_DATE)
  AND placed_at < CURRENT_DATE + INTERVAL '1 day';
<<<END>>>
```
- Resolves to `[2026-07-01 00:00:00-04, 2026-10-14 00:00:00-04)`.
- Tests: calendar-period accuracy, open-ended current period.
- Anti-pattern: hard-coding `>= DATE '2026-07-01'`.

### T-04: past 24 hours across spring forward

- Input: "How many users signed up in the past 24 hours?"
- Clock: N4 (2026-03-08 03:30 EDT; the local 02:00 hour never existed).
- Session zone: America/New_York.
- Schema: `users(id integer, created_at timestamptz)`.
- Expected:
```sql
<<<SQL>>>
SELECT COUNT(*)
FROM users
WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours';
<<<END>>>
```
- Tests: rolling-window accuracy, DST behavior. The window is exactly 24 absolute
  hours ending 2026-03-08T07:30:00Z, reaching back to 2026-03-07T07:30:00Z
  (02:30 EST, which did exist).
- Anti-pattern: any local-time literal near the transition; wall-clock subtraction
  such as "03:30 minus 24 hours = yesterday 03:30" expressed as a literal.

### T-05: fall-back ambiguity requires clarification

- Input: "Show orders placed between 1:00 AM and 2:00 AM on November 1st."
- Clock: N5. Session zone: America/New_York.
- Schema: `orders(id integer, placed_at timestamptz, total numeric)`.
- Expected:
```
<<<CLARIFY>>>
On 2026-11-01 the 1:00 AM hour happened twice in America/New_York: once in EDT
(05:00-06:00 UTC) and once in EST (06:00-07:00 UTC). Which occurrence do you mean?
<<<END>>>
```
- Tests: boundary correctness, DST behavior, honesty under ambiguity.
- Anti-pattern: any SQL guessing one occurrence; emitting `01:30` without an offset.

### T-06: leap-year February, user-named month

- Input: "All orders from February 2024."
- Clock: N3. Session zone: America/New_York.
- Schema: `orders(id integer, placed_at timestamptz, total numeric)`.
- Expected:
```sql
<<<SQL>>>
SELECT *
FROM orders
WHERE placed_at >= TIMESTAMPTZ '2024-02-01 00:00:00 America/New_York'
  AND placed_at < TIMESTAMPTZ '2024-03-01 00:00:00 America/New_York';
<<<END>>>
```
- The user named the month, so materializing its bounds is derivation, not
  invention. February 2024 has 29 days.
- Seed edge rows: `2024-02-29 12:00:00-05` (included), `2024-03-01 00:00:00-05`
  (excluded).
- Tests: leap-year boundary correctness.
- Anti-pattern: upper bound of `2024-02-28`; assuming 28 days.

### T-07: year-end, UTC date differs from local date

- Input: "What were today's total sales?"
- Clock: N6 (2026-12-31 23:59:59 EST locally; 2027-01-01 04:59:59 UTC).
- Session zone: America/New_York.
- Schema: `sales(id integer, sold_at timestamptz, amount numeric)`.
- Expected:
```sql
<<<SQL>>>
SELECT SUM(amount)
FROM sales
WHERE sold_at >= CURRENT_DATE
  AND sold_at < CURRENT_DATE + INTERVAL '1 day';
<<<END>>>
```
- Under the session zone, `CURRENT_DATE` is 2026-12-31. A row stamped
  2027-01-01T02:00:00Z (2026-12-31 21:00 EST) is included; a row stamped
  2027-01-01T05:30:00Z (2026-12-31... no: 00:30 EST on Jan 1) is excluded.
- Tests: tz correctness.
- Anti-pattern: filtering `sold_at::date = DATE '2027-01-01'` (UTC date).

### T-08: quarter boundary, UTC quarter differs from local quarter

- Input: "Revenue this quarter."
- Clock: N7 (2026-06-30 23:30 EDT locally; 2026-07-01 03:30 UTC).
- Session zone: America/New_York.
- Schema: `orders(id integer, placed_at timestamptz, total numeric)`.
- Expected:
```sql
<<<SQL>>>
SELECT SUM(total)
FROM orders
WHERE placed_at >= date_trunc('quarter', CURRENT_DATE)
  AND placed_at < CURRENT_DATE + INTERVAL '1 day';
<<<END>>>
```
- Under the session zone this is Q2 (`>= 2026-04-01`). UTC would say Q3.
- Tests: tz correctness, business-local reporting.
- Anti-pattern: `date_trunc('quarter', ...)` computed against UTC.

### T-09: explicit user zone overrides business zone

- Input: "How many orders came in today?" with request metadata
  `user_zone = Pacific/Auckland`.
- Clock: N8 (2026-10-14 12:30 NZDT locally; 2026-10-13 23:30 UTC;
  2026-10-13 19:30 EDT in New York).
- Session zone: Pacific/Auckland (harness pins to the explicit user zone).
- Schema: `orders(id integer, placed_at timestamptz)`.
- Expected:
```sql
<<<SQL>>>
SELECT COUNT(*)
FROM orders
WHERE placed_at >= CURRENT_DATE
  AND placed_at < CURRENT_DATE + INTERVAL '1 day';
<<<END>>>
```
- `CURRENT_DATE` is 2026-10-14 in Auckland. A row stamped 2026-10-13T20:00:00Z
  (2026-10-14 09:00 NZDT) is included although it is still Oct 13 in New York.
- Tests: tz correctness, explicit-zone precedence.
- Anti-pattern: resolving "today" in America/New_York or UTC.

### T-10: trailing 30 days (rolling, not calendar)

- Input: "Order count for the trailing 30 days."
- Clock: N1. Session zone: America/New_York.
- Schema: `orders(id integer, placed_at timestamptz)`.
- Expected:
```sql
<<<SQL>>>
SELECT COUNT(*)
FROM orders
WHERE placed_at >= CURRENT_TIMESTAMP - INTERVAL '30 days';
<<<END>>>
```
- Window: `[2026-09-13T14:00:00Z, 2026-10-13T14:00:00Z]`. A September 1 row is
  excluded; "trailing 30 days" is not "since the start of last month".
- Tests: rolling-window accuracy, rolling vs calendar distinction.
- Anti-pattern: `date_trunc('month', ...)` bounds; `INTERVAL '1 month'`.

### T-11: previous week, Monday start

- Input: "Signups last week."
- Clock: N1 (Tuesday 2026-10-13). Session zone: America/New_York.
- Schema: `users(id integer, created_at timestamptz)`.
- Expected:
```sql
<<<SQL>>>
SELECT COUNT(*)
FROM users
WHERE created_at >= date_trunc('week', CURRENT_DATE) - INTERVAL '1 week'
  AND created_at < date_trunc('week', CURRENT_DATE);
<<<END>>>
```
- Resolves to `[2026-10-05 00:00:00-04, 2026-10-12 00:00:00-04)`.
- Seed edge rows: `2026-10-04 23:59:59-04` (excluded), `2026-10-05 00:00:00-04`
  (included), `2026-10-11 23:59:59-04` (included), `2026-10-12 00:00:00-04`
  (excluded).
- Tests: calendar-period accuracy, week-start convention, boundary correctness.
- Anti-pattern: Sunday-start weeks; `INTERVAL '7 days'` for a calendar week.

### T-12: DATE column preferred for whole-day predicates

- Input: "Orders from October 12."
- Clock: N1. Session zone: America/New_York.
- Schema: `orders(id integer, order_date date, placed_at timestamptz, total numeric)`.
- Expected:
```sql
<<<SQL>>>
SELECT *
FROM orders
WHERE order_date = DATE '2026-10-12';
<<<END>>>
```
- The user named the date, so the literal is derived. With both types present, the
  `DATE` column is preferred over `placed_at::date` casts.
- Tests: DB type awareness, boundary correctness.
- Anti-pattern: `placed_at::date = DATE '2026-10-12'` when `order_date` exists.
- Fallback sub-case (schema without `order_date`): half-open `timestamptz` bounds
  `[2026-10-12 00:00:00-04, 2026-10-13 00:00:00-04)` derived from `CURRENT_DATE`
  arithmetic, e.g. `placed_at >= DATE '2026-10-12'` ... no: relative form
  `placed_at >= CURRENT_DATE - INTERVAL '1 day' AND placed_at < CURRENT_DATE`.

### T-13: clock.now when the instant is the answer

- Input: "What is the current time at the Mint salon?"
- Clock: N1. Session zone: America/New_York.
- Schema: `orders(id integer, placed_at timestamptz)` (supplied but irrelevant).
- Expected turn 1:
```
<<<TOOL_CALL>>>
{"name": "clock.now", "arguments": {"zone": "America/New_York"}}
<<<END>>>
```
- Host relays: `<<<TOOL_RESULT name="clock.now" status="ok">>>`
  `{"utc": "2026-10-13T14:00:00Z", "zone": "America/New_York", "local": "2026-10-13T10:00:00-04:00"}`
  `<<<END>>>`
- Expected turn 2 (plain text, grounded in the observation): "It is 10:00 AM EDT
  at the salon (2026-10-13)."
- Tests: `clock.now` used exactly when SQL cannot answer.
- Anti-pattern: answering from pretraining ("it is probably morning"); emitting SQL.

### T-14: no clock.now for date arithmetic

- Input: "How many orders were placed yesterday?"
- Clock: N1. Session zone: America/New_York.
- Schema: `orders(id integer, placed_at timestamptz, total numeric)`.
- Expected: the T-01 SQL, and the response must contain no `<<<TOOL_CALL>>>`.
- Tests: temporal/tool-use slice separation; tool abstention for arithmetic SQL
  can do.
- Anti-pattern: calling `clock.now` then computing "yesterday" from its output.

### T-15: as-of query uses event time (advanced)

- Input: "What was customer 42's address on September 15, 2026?"
- Clock: N1. Session zone: America/New_York.
- Schema: `customer_addresses(customer_id integer, address text, valid_from timestamptz, valid_to timestamptz, recorded_at timestamptz)`.
- Expected:
```sql
<<<SQL>>>
SELECT address
FROM customer_addresses
WHERE customer_id = 42
  AND valid_from <= TIMESTAMPTZ '2026-09-15 00:00:00 America/New_York'
  AND (valid_to IS NULL OR valid_to > TIMESTAMPTZ '2026-09-15 00:00:00 America/New_York')
ORDER BY valid_from DESC
LIMIT 1;
<<<END>>>
```
- Seed: address A valid 2026-01-01 to 2026-09-01 (recorded 2026-01-02); address B
  valid 2026-09-01 onward but recorded 2026-10-05 (backdated correction); address C
  valid 2026-06-01 to 2026-08-01 (recorded 2026-10-06, superseded correction).
  As of 2026-09-15 the answer is B. A `recorded_at`-based query would return C.
- Tests: event time vs transaction time; latest-version selection.
- Anti-pattern: any predicate on `recorded_at`; missing `valid_to IS NULL` arm.

## 12. Metrics

All metrics are computed per fixture and aggregated by family. Proposed acceptance
thresholds are for the next-gen milestone; the frozen v12 baseline is measured
first and its scores size the curriculum (see the curriculum taxonomy).

| Metric | Fixtures | Definition | Proposed bar |
|---|---|---|---|
| Calendar-period accuracy | T-01, T-02, T-03, T-08, T-11 | Candidate bounds equal the expected half-open interval under the fixed clock | >= 95% |
| Rolling-window accuracy | T-04, T-10 | Absolute-time window correct; no calendar confusion | 100% |
| Boundary correctness | T-01, T-02, T-05, T-06, T-11, T-12 | Edge seed rows classified correctly; ambiguity clarified, never guessed | >= 95% |
| TZ correctness | T-07, T-08, T-09 | Effective zone respected; no hard-coded offsets; no UTC-date filtering for local requests | 100% |
| DST behavior | T-04, T-05 | No nonexistent local literals; absolute intervals across transitions; ambiguity clarified | 100% |
| Date-hallucination rate | all | Share of responses containing a calendar date or datetime literal not derivable from the user input or from `clock.now` output | 0% |
| Execution correctness under fixed clocks | all SQL fixtures | Candidate SQL executes on the seeded fixture DB; returned row set equals the expected row set | >= 95% |
| Clock discipline | T-13, T-14 | `clock.now` called iff the instant itself is the answer; never for SQL-expressible arithmetic | 100% |

Scoring is semantic, not string match: any candidate whose bounds, filters, and
result set equal the expected ones under the fixed clock passes, regardless of
surface form (e.g. `CURRENT_DATE - INTERVAL '1 day'` vs
`date_trunc('day', CURRENT_TIMESTAMP) - INTERVAL '1 day'`).

## 13. Rules

1. Never invent calendar dates. Relative references compile to `CURRENT_DATE` /
   `CURRENT_TIMESTAMP` arithmetic. Literals appear only for user-named dates.
2. Never invent schema identifiers. Every table and column in candidate SQL comes
   from the fixture schema or from a prior `schema.inspect` observation.
3. If the authorized schema is missing and tools are available, call
   `schema.inspect`. If tools are unavailable, return `<<<CLARIFY>>>` asking for
   the schema. Never guess table or column names.
4. Ranges are half-open `[start, end)`. No `BETWEEN` with inclusive ends, no
   `23:59:59` upper bounds.
5. Rolling windows use `INTERVAL` subtraction on the instant (`CURRENT_TIMESTAMP`),
   never wall-clock field math.
6. Calendar language resolves in the effective zone (explicit user zone, else
   business-local). UTC is storage, not reporting.
7. As-of questions predicate on event time (`valid_from`/`valid_to`), never on
   transaction time (`recorded_at`).
8. `clock.now` is for now-questions, not for date arithmetic.

## 14. Variant acceptance and scoring notes

- The harness seeds each fixture DB with the listed edge rows plus a small set of
  interior rows, then compares the candidate's result set to the expected set.
- Equivalent formulations pass: the scorer normalizes bounds to absolute instants
  under (`now_utc`, `session_zone`) before comparing.
- `<<<CLARIFY>>>` fixtures (T-05) pass only when the response is a `<<<CLARIFY>>>`
  envelope naming the ambiguity. A correct SQL for one disambiguation still fails:
  guessing is the failure mode under test.
- `<<<UNANSWERABLE>>>` is not exercised in this spec; temporal questions outside
  the authorized schema fall back to the tool-use curriculum's missing-schema rules.
- New fixtures added later must follow the same contract: fixed `now_utc`,
  declared `session_zone`, seeded edge rows, listed anti-patterns. Train/eval
  splits separate fixtures by temporal boundary family (section 9 of the
  curriculum taxonomy).
