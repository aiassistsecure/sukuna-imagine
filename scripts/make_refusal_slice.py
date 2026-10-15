#!/usr/bin/env python3
"""Build a refusal/clarification training slice for Imagine.

v12's corpus was ~zero refusal rows: scripts/make_protocol.py's
clean_sql_block() accepts only bare <<<SQL>>> targets and drops everything
else, so the model never saw a demonstrated refusal. The standing eval
(~/workspace/v12-refusal-regression) measured the result: 0/10 UNANSWERABLE
and 0/5 CLARIFY — every out-of-schema probe came back as invented SQL.

This is the data-side fix. It generates UNANSWERABLE and CLARIFY rows whose
prompts are built with the EXACT build_prompt() from stealth/forge.py
(imported, never copied) and whose targets are real sentinel blocks parsed by
stealth/sentinel.py:

    <<<UNANSWERABLE>>>
    <short reason>
    <<<END>>>

    <<<CLARIFY>>>
    <one precise question>
    <<<END>>>

Usage:
    python scripts/make_refusal_slice.py --out corpus/refusal_train.jsonl
    python scripts/make_refusal_slice.py --schemas shop,clinic --count-per-kind 5 --seed 7 --out /tmp/r.jsonl

Deterministic in `seed`: same args, same bytes.

Row format (mirrors make_protocol.py):
    {"messages": [{"role":"system",...},{"role":"user",...},{"role":"assistant","content":"<<<UNANSWERABLE>>>..."}],
     "meta": {"kind":"refusal","refusal_kind":"UNANSWERABLE","schema_key":"shop","question":...}}

Scaling: the question banks below hold ~12 UNANSWERABLE + ~10 CLARIFY per
schema (132 unique questions across the six catalog schemas). --count-per-kind
cycles the bank when it exceeds bank size, so a few hundred to a few thousand
rows need no new questions — e.g. --count-per-kind 100 gives 6*2*100 = 1200
rows. To go beyond the natural-language variety of the banks, extend the banks
(they are the whole content budget) rather than inventing templates.

What this does NOT do: it sets no corpus percentages. How the refusal slice
mixes with the SQL/identity/protocol slices stays a hypothesis until the
Phase 4 baseline lands — that call is out of scope here.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STEALTH = os.path.join(REPO, "stealth")


def _load_stealth():
    """Load sentinel/schemas/forge as files, bypassing the DB-backed __init__.

    Same approach as ~/workspace/v12-refusal-regression/run_refusal_suite.py:
    forge.py needs the DB-backed gate and psycopg2 at import time, but this
    generator only calls build_prompt(), so both are stubbed.
    """
    pkg = types.ModuleType("stealth")
    pkg.__path__ = [STEALTH]
    sys.modules["stealth"] = pkg

    def load(name, path):
        spec = importlib.util.spec_from_file_location("stealth." + name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["stealth." + name] = mod
        setattr(pkg, name, mod)
        spec.loader.exec_module(mod)
        return mod

    sentinel = load("sentinel", os.path.join(STEALTH, "sentinel.py"))
    schemas = load("schemas", os.path.join(STEALTH, "schemas.py"))
    gate_stub = types.ModuleType("stealth.gate")
    gate_stub.Gate = type("Gate", (), {})
    gate_stub.Level = type("Level", (), {})
    sys.modules["stealth.gate"] = gate_stub
    pkg.gate = gate_stub
    pg = types.ModuleType("psycopg2")
    pg.__path__ = []
    sys.modules["psycopg2"] = pg
    forge = load("forge", os.path.join(STEALTH, "forge.py"))
    return forge, sentinel, schemas


forge, sentinel, schemas = _load_stealth()

# ---------------------------------------------------------------------------
# Question banks. Each entry: (question, target payload).
#
# UNANSWERABLE: the schema genuinely cannot answer it — world knowledge,
# non-database requests, entities/columns that do not exist (never invent
# sensitive attributes like passwords/SSNs), or questions whose premise is
# false for the schema. The reason must be a TRUE statement about the schema.
#
# CLARIFY: genuinely ambiguous — 2+ materially different readings, or a
# missing anchor (which window? which metric? which scope?) with no defensible
# default. The target is one precise question naming the readings.
# ---------------------------------------------------------------------------

BANKS = {
    "shop": {
        "UNANSWERABLE": [
            ("What is the capital of France?",
             "The schema is an e-commerce database (customers, products, orders); it contains no world-knowledge facts."),
            ("What is the weather like today in Orlando?",
             "The schema stores no weather data — customers has city names, but no weather."),
            ("List all employees and their salaries.",
             "No employees table exists; inventing one would be hallucination."),
            ("Show the password for each customer.",
             "No password column exists on customers or anywhere in the schema."),
            ("List customers' social security numbers.",
             "No SSN column exists on customers or anywhere in the schema."),
            ("List customers' credit card numbers.",
             "No credit card column exists on customers or anywhere in the schema."),
            ("What are the manufacturers of each product?",
             "products has title, category, price, stock — no manufacturer column."),
            ("Show me the source code for the checkout page.",
             "Not a database question; nothing in the schema stores source code."),
            ("What did the stock market do today?",
             "The schema contains no financial-market data."),
            ("List each customer's email address and phone number.",
             "customers has name, city, tier, lifetime_value — no email or phone columns."),
            ("Which supplier delivers each product?",
             "No suppliers table or supplier column exists."),
            ("What time is it right now?",
             "The schema has no clock; placed_at records order dates only."),
        ],
        "CLARIFY": [
            ("Who are our best customers?",
             "Should 'best' mean highest lifetime_value, most orders placed, or highest total order spend?"),
            ("Show me the top products.",
             "Top by price, by stock on hand, or by number of orders?"),
            ("List the biggest orders.",
             "Biggest by total amount — and above what threshold?"),
            ("Which cities are doing well?",
             "Doing well by number of customers, total lifetime_value, or order volume?"),
            ("Show recent orders.",
             "How far back should 'recent' go — the last week, the last month, or since a specific date?"),
            ("Which products are doing poorly?",
             "Poorly by low stock, few orders, or low revenue?"),
            ("Show me customer activity.",
             "Activity by orders placed, lifetime value, or recency of last order?"),
            ("How are sales trending?",
             "Trending by order count, total revenue, or average order value — and over what period?"),
            ("Which customers should we contact?",
             "For what purpose — high-value retention, pending orders, or something else?"),
            ("Give me a customer summary.",
             "Summary of which customers, and of what — lifetime value, order history, or tier?"),
        ],
    },
    "clinic": {
        "UNANSWERABLE": [
            ("What is the weather like today in Orlando?",
             "The schema stores no weather data."),
            ("List patients' social security numbers.",
             "patients has full_name, dob, insurer — no SSN column."),
            ("What are the patients' blood types?",
             "patients has no blood-type column."),
            ("Show each practitioner's home address.",
             "practitioners has name, speciality, hourly_rate — no address column."),
            ("What prescriptions has each patient been given?",
             "No prescriptions table or column exists."),
            ("List the clinic's employees and their salaries.",
             "No employees table exists."),
            ("What did the doctor say during the appointment?",
             "No notes or transcript column exists on appointments."),
            ("What is the meaning of life?",
             "Not a database question; the schema cannot answer it."),
            ("Who won the game last night?",
             "The schema contains no sports data."),
            ("Show me the login page source code.",
             "Not a database question; nothing in the schema stores source code."),
            ("What is each patient's credit card number?",
             "No credit card column exists on patients or anywhere in the schema."),
            ("List the emergency contacts for each patient.",
             "No emergency-contact column exists on patients."),
        ],
        "CLARIFY": [
            ("Which practitioners are the busiest?",
             "Busiest by number of appointments or by total appointment minutes?"),
            ("Show recent appointments.",
             "How far back — the last week, the last month, or since a specific date?"),
            ("Who are the top practitioners?",
             "Top by hourly_rate, by appointments attended, or by distinct patients seen?"),
            ("Which patients need attention?",
             "Attention for what — upcoming booked appointments, no-shows, or uninsured patients?"),
            ("How is the clinic doing?",
             "By appointment volume, attendance rate, or practitioner utilization?"),
            ("Show me the schedule.",
             "For which practitioner and which date range?"),
            ("Which speciality is most popular?",
             "Popular by number of practitioners, appointments booked, or appointments attended?"),
            ("List patient visits.",
             "Visits in what state — booked, attended, no-show — and over what period?"),
            ("Who are the newest patients?",
             "patients has no signup-date column; should 'newest' mean highest patient id?"),
            ("Summarize the appointments.",
             "Summarize by state, by practitioner, or by patient — and over what period?"),
        ],
    },
    "library": {
        "UNANSWERABLE": [
            ("Who won the World Series last year?",
             "The schema contains no sports data."),
            ("Show each member's password.",
             "No password column exists on members or anywhere in the schema."),
            ("What are the ISBNs of all books?",
             "books has title, author, Shelf Code, copies — no ISBN column."),
            ("List members' social security numbers.",
             "No SSN column exists on members or anywhere in the schema."),
            ("What is the airspeed velocity of an unladen swallow?",
             "Not a database question; the schema cannot answer it."),
            ("Show me the library's financial statements.",
             "No financial tables exist."),
            ("What is each book's publisher?",
             "books has no publisher column."),
            ("List the employees' salaries.",
             "No employees table exists."),
            ("What is the meaning of life?",
             "Not a database question; the schema cannot answer it."),
            ("Send a reminder email to members with overdue books.",
             "members has no email address, and the model cannot send email."),
            ("What are the late fees per day?",
             "fines_owed is a total per member; no per-day fee rate exists."),
            ("Which books were published last year?",
             "books has no publication-date column."),
        ],
        "CLARIFY": [
            ("Which books are popular?",
             "Popular by number of loans, by reservations, or by copies on the shelf?"),
            ("Who are the most active members?",
             "Active by loans taken, loans returned, or fines paid?"),
            ("Recommend me a book.",
             "Based on what — a particular author, most-loaned titles, or what is currently available?"),
            ("Which members should we contact?",
             "For what — loans not yet returned, unpaid fines, or something else?"),
            ("Which books are in demand?",
             "Demand by loan count, by the reservation queue, or by low copies on hand?"),
            ("Show me the new arrivals.",
             "books has no arrival-date column; should 'new' mean highest book id?"),
            ("Which authors do we have the most of?",
             "Most by number of distinct titles or by total copies?"),
            ("Give me a member summary.",
             "Summary for which member, and of what — loans, reservations, or fines?"),
            ("Which books are unavailable?",
             "Unavailable meaning zero copies on hand, or all copies currently out on loan?"),
            ("How are we doing on fines?",
             "Total fines owed, fines per member, or members with the highest fines?"),
        ],
    },
    "fleet": {
        "UNANSWERABLE": [
            ("What is the airspeed velocity of an unladen swallow?",
             "Not a database question; the schema cannot answer it."),
            ("List the drivers' names and phone numbers.",
             "No drivers table exists."),
            ("What is the weather along each route?",
             "The schema stores no weather data."),
            ("Show each vehicle's fuel type.",
             "vehicles has plate, vehicleType, capacityKg — no fuel column."),
            ("List the mechanics' salaries.",
             "No employees or mechanics table exists."),
            ("What is the GPS location of each vehicle right now?",
             "No location-tracking columns exist."),
            ("Show the maintenance history of each vehicle.",
             "No maintenance table exists."),
            ("What did the stock market do today?",
             "The schema contains no financial-market data."),
            ("List each vehicle's VIN number.",
             "vehicles has no VIN column."),
            ("Who owns each vehicle?",
             "vehicles has no owner column."),
            ("What is the insurance policy for each vehicle?",
             "No insurance data exists in the schema."),
            ("Show me the login credentials for the fleet dashboard.",
             "No credentials exist in the schema."),
        ],
        "CLARIFY": [
            ("How is the fleet performing?",
             "Performing by packages delivered, routes completed, or capacity utilization?"),
            ("Which vehicle is the best?",
             "Best by capacityKg, by total packages delivered, or by routes run?"),
            ("Show recent runs.",
             "How far back — and 'runs' by route or by vehicle?"),
            ("Which regions are busiest?",
             "Busiest by total packages, by number of routes, or by vehicle count?"),
            ("How is vehicle utilization?",
             "Should utilization compare package counts against capacityKg, or count routes per vehicle?"),
            ("Which vehicles need attention?",
             "Attention for what — low package counts, few routes, or something not in the schema like maintenance?"),
            ("Summarize the routes.",
             "Summarize by region, by vehicle, or by date — and which metric?"),
            ("What is the biggest region?",
             "Biggest by packages delivered or by number of routes?"),
            ("Show me delivery stats.",
             "Stats by vehicle, by region, or by date — and packages, routes, or both?"),
            ("Which van is doing the most work?",
             "Most work by packages delivered or by routes run?"),
        ],
    },
    "audit": {
        "UNANSWERABLE": [
            ("Show me the source code for the login page.",
             "Not a database question; nothing in the schema stores source code."),
            ("Who authored each code review?",
             "activities has project_id, category, occurred_at — no author column."),
            ("List the employees and their salaries.",
             "No employees table exists."),
            ("What is the weather today?",
             "The schema stores no weather data."),
            ("Show each user's password.",
             "No users table and no password column exist."),
            ("What did the deploy change?",
             "activities records that a deploy happened, not what it contained."),
            ("List the server hostnames running each project.",
             "No server or host columns exist."),
            ("What time is it right now?",
             "The schema has no clock; occurred_at is per-activity history."),
            ("Who won the game last night?",
             "The schema contains no sports data."),
            ("Show the API keys for each project.",
             "No API-key column exists on projects or anywhere in the schema."),
            ("What is each project's budget?",
             "projects has name only — no budget column."),
            ("List the customers of each project.",
             "No customers table exists."),
        ],
        "CLARIFY": [
            ("Which projects are active?",
             "Active by number of activities, by any activity this month, or by distinct categories?"),
            ("Show recent deploys.",
             "How far back — the last week, the last month, or since a specific date?"),
            ("How busy was last quarter?",
             "Busy by number of activities or by number of active projects?"),
            ("Summarize project activity.",
             "For which project, and by category, by month, or overall?"),
            ("When was the peak period?",
             "Peak by activity count — per day, per week, or per month?"),
            ("What happened recently?",
             "How far back — and across all projects or just one?"),
            ("Compare the projects.",
             "Compare by what — total activities, category mix, or time span?"),
            ("Show me the timeline.",
             "Timeline of what — all activities, one category, or one project?"),
            ("Which categories dominate?",
             "Dominate by count overall or per project?"),
            ("Give me a project health check.",
             "'Health' is not defined in the schema — by activity volume, recency, or category mix?"),
        ],
    },
    "telemetry": {
        "UNANSWERABLE": [
            ("What time is it right now?",
             "The schema has no clock; taken_at is per-sample history."),
            ("List the admin passwords for each host.",
             "No credentials exist in the schema."),
            ("What is the weather in us-east?",
             "The schema stores no weather data."),
            ("Show the disk usage of each host.",
             "samples has cpu_pct and mem_pct — no disk column."),
            ("List the owners of each host.",
             "hosts has hostname, region, cores — no owner column."),
            ("What processes are running on alpha right now?",
             "No process-level data exists."),
            ("Show me the source code of the monitoring agent.",
             "Not a database question; nothing in the schema stores source code."),
            ("What is the meaning of life?",
             "Not a database question; the schema cannot answer it."),
            ("Which hosts have GPUs?",
             "hosts has cores but no GPU column."),
            ("List the IP addresses of the hosts.",
             "No IP-address column exists on hosts."),
            ("Who won the game last night?",
             "The schema contains no sports data."),
            ("Show the SSH keys for each host.",
             "No SSH keys exist in the schema."),
        ],
        "CLARIFY": [
            ("Which hosts are struggling?",
             "Struggling by cpu_pct or by mem_pct — and above what threshold?"),
            ("When did things peak?",
             "Peak cpu or peak memory — and per host or fleet-wide?"),
            ("Which host is the busiest?",
             "Busiest by average cpu_pct, peak cpu_pct, or number of samples?"),
            ("Show recent samples.",
             "How far back — the last hour, the last day — and for which host?"),
            ("How is the fleet's health?",
             "Health by cpu, by memory, or by missing samples (NULL mem_pct)?"),
            ("Which region is hottest?",
             "Hottest by average cpu_pct or by peak cpu_pct across its hosts?"),
            ("Summarize host performance.",
             "For which host, and over what window — average, peak, or latest readings?"),
            ("Are we running out of memory?",
             "On which host — and 'running out' at what mem_pct threshold?"),
            ("What is the average load?",
             "Average of cpu_pct or mem_pct — and per host, per region, or fleet-wide?"),
            ("Which hosts need attention?",
             "Attention for what — high cpu, high memory, or gaps in the samples?"),
        ],
    },
}

KINDS = ("UNANSWERABLE", "CLARIFY")


def generate_rows(schema_keys, count_per_kind, seed, style="ddl"):
    """Generate refusal rows. Deterministic in `seed`."""
    rng = random.Random(seed)
    rows = []
    for key in schema_keys:
        schema = schemas.get(key)
        for kind in KINDS:
            bank = BANKS[key][kind]
            order = list(range(len(bank)))
            rng.shuffle(order)
            for i in range(count_per_kind):
                bank_idx = order[i % len(bank)]
                question, payload = bank[bank_idx]
                messages = forge.build_prompt(schema, question, style)
                messages = messages + [
                    {"role": "assistant",
                     "content": sentinel.wrap(payload, kind)}
                ]
                rows.append({
                    "messages": messages,
                    "meta": {
                        "kind": "refusal",
                        "refusal_kind": kind,
                        "schema_key": key,
                        "question": question,
                        "bank_index": bank_idx,
                        "cycle": i // len(bank),
                    },
                })
    rng.shuffle(rows)
    return rows


def parse_schema_keys(raw):
    raw = (raw or "").strip().lower()
    if raw in ("all", ""):
        return list(schemas.CATALOG.keys())
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    unknown = [k for k in keys if k not in schemas.CATALOG]
    if unknown:
        raise ValueError(
            f"unknown schema keys: {unknown}; valid: {sorted(schemas.CATALOG)}")
    return keys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schemas", default="all",
                    help="comma-separated schema keys or 'all' (default: all)")
    ap.add_argument("--count-per-kind", type=int, default=20,
                    help="UNANSWERABLE and CLARIFY rows per schema (default: 20)")
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--out", default="corpus/refusal_train.jsonl")
    ap.add_argument("--style", default="ddl",
                    help="build_prompt style: ddl or compact (default: ddl)")
    a = ap.parse_args()

    keys = parse_schema_keys(a.schemas)
    rows = generate_rows(keys, a.count_per_kind, a.seed, a.style)
    outdir = os.path.dirname(a.out)
    if outdir:
        os.makedirs(outdir, exist_ok=True)
    with open(a.out, "w") as f:
        for rec in rows:
            f.write(json.dumps(rec) + "\n")
    print(f"wrote {len(rows)} refusal rows "
          f"({len(keys)} schemas x {a.count_per_kind}/kind) -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
