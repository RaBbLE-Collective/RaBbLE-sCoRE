#!/usr/bin/env python3
"""
RaBbLE API test client — v0.0.0.0
Exercises all server endpoints. Requires httpx (in requirements.txt).

Usage:
  python3 api_test.py                        # localhost:8000
  python3 api_test.py https://score.joinrabble.world
  python3 api_test.py http://localhost:8000 --verbose
"""

import sys
import json
import time
import argparse
import httpx

GREEN = "\033[0;32m"
RED   = "\033[0;31m"
CYAN  = "\033[0;36m"
DIM   = "\033[2m"
RESET = "\033[0m"

passed = 0
failed = 0


def ok(label: str, detail: str = "") -> None:
    global passed
    passed += 1
    suffix = f"  {DIM}{detail}{RESET}" if detail else ""
    print(f"  {GREEN}✓{RESET}  {label}{suffix}")


def fail(label: str, detail: str = "") -> None:
    global failed
    failed += 1
    suffix = f"  {DIM}{detail}{RESET}" if detail else ""
    print(f"  {RED}✗{RESET}  {label}{suffix}")


def section(title: str) -> None:
    print(f"\n{CYAN}── {title} ──{RESET}")


def run(base: str, verbose: bool = False) -> int:
    client = httpx.Client(base_url=base, timeout=30)
    print(f"\n  RaBbLE API test → {base}")

    # ── Health ────────────────────────────────────────────────────────────────
    section("Health")
    r = client.get("/health")
    if r.status_code == 200 and r.json().get("status") == "ok":
        ok("Health check", f"version={r.json().get('version')}")
    else:
        fail("Health check — server not responding", str(r.status_code))
        print(f"\n  {RED}Cannot reach server. Aborting.{RESET}\n")
        return 1

    # ── Auth: register ────────────────────────────────────────────────────────
    section("Auth: register")
    email = f"test_{int(time.time())}@rabble.dev"
    r = client.post("/api/v1/auth/register",
                    json={"name": "Test User", "email": email})
    if r.status_code == 201 and "api_key" in r.json():
        api_key = r.json()["api_key"]
        ok("Register", f"tier={r.json().get('tier')}")
    else:
        fail("Register", f"HTTP {r.status_code}: {r.text[:80]}")
        return 1

    # duplicate rejected
    r2 = client.post("/api/v1/auth/register",
                     json={"name": "Dup", "email": email})
    if r2.status_code == 409:
        ok("Duplicate email → 409")
    else:
        fail("Duplicate email not rejected", f"HTTP {r2.status_code}")

    # ── Auth: token ───────────────────────────────────────────────────────────
    section("Auth: token")
    r = client.post("/api/v1/auth/token", json={"api_key": api_key})
    if r.status_code == 200 and "token" in r.json():
        jwt = r.json()["token"]
        ok("Token exchange → JWT")
    else:
        fail("Token exchange", f"HTTP {r.status_code}: {r.text[:80]}")
        return 1

    r = client.post("/api/v1/auth/token", json={"api_key": "rbbl_invalid"})
    if r.status_code == 401:
        ok("Bad key → 401")
    else:
        fail("Bad key not rejected", f"HTTP {r.status_code}")

    auth_headers_jwt  = {"Authorization": f"Bearer {jwt}"}
    auth_headers_key  = {"X-API-Key": api_key}

    # ── Status ────────────────────────────────────────────────────────────────
    section("Status")
    r = client.get("/api/v1/status", headers=auth_headers_jwt)
    if r.status_code == 200:
        data = r.json()
        ok("Status (JWT)", f"providers={data.get('providers')}")
        if verbose:
            print(f"    {json.dumps(data, indent=2)}")
    else:
        fail("Status (JWT)", f"HTTP {r.status_code}")

    r = client.get("/api/v1/status", headers=auth_headers_key)
    if r.status_code == 200:
        ok("Status (X-API-Key)")
    else:
        fail("Status (X-API-Key)", f"HTTP {r.status_code}")

    r = client.get("/api/v1/status")
    if r.status_code == 401:
        ok("Unauthenticated → 401")
    else:
        fail("Unauthenticated not blocked", f"HTTP {r.status_code}")

    # ── Chat (non-streaming) ──────────────────────────────────────────────────
    section("Chat — non-streaming")
    r = client.post("/api/v1/chat/complete", headers=auth_headers_jwt,
                    json={"messages": [{"role": "user", "content": "Hello RaBbLE. What are you?"}],
                          "model_tier": "fast"})
    if r.status_code == 200 and r.json().get("content"):
        snippet = r.json()["content"][:60].replace("\n", " ")
        ok("Chat complete", f"'{snippet}...'")
    else:
        fail("Chat complete", f"HTTP {r.status_code}: {r.text[:80]}")

    r = client.post("/api/v1/chat/complete", headers=auth_headers_jwt,
                    json={"messages": [{"role": "user", "content": "hi"}],
                          "model_tier": "turbo"})
    if r.status_code == 422:
        ok("Invalid model_tier → 422")
    else:
        fail("Invalid model_tier not rejected", f"HTTP {r.status_code}")

    # ── Chat (streaming) ──────────────────────────────────────────────────────
    section("Chat — streaming")
    r = client.post("/api/v1/chat", headers=auth_headers_jwt,
                    json={"messages": [{"role": "user", "content": "Name one thing you find interesting."}],
                          "model_tier": "fast", "workflow_type": "brainstorm"})
    if r.status_code == 200 and len(r.text) > 0:
        ok("Streaming chat", f"{len(r.text)} chars received")
    else:
        fail("Streaming chat", f"HTTP {r.status_code}: {r.text[:80]}")

    # ── Workflows ─────────────────────────────────────────────────────────────
    section("Workflows")
    r = client.post("/api/v1/workflows", headers=auth_headers_jwt,
                    json={"goal": "Build a 10-minute morning routine",
                          "workflow_type": "thrive", "model_tier": "fast"})
    wf_id = None
    if r.status_code == 201 and r.json().get("id"):
        wf_id = r.json()["id"]
        ok("Workflow created", f"id={wf_id} state={r.json().get('state')}")
    else:
        fail("Workflow create", f"HTTP {r.status_code}: {r.text[:80]}")

    if wf_id:
        r = client.post(f"/api/v1/workflows/{wf_id}/step", headers=auth_headers_jwt,
                        json={"message": "I have 15 minutes and prefer calm activities."})
        if r.status_code == 200:
            ok("Workflow step", f"state={r.json().get('state')}")
        else:
            fail("Workflow step", f"HTTP {r.status_code}: {r.text[:80]}")

        r = client.get(f"/api/v1/workflows/{wf_id}", headers=auth_headers_jwt)
        if r.status_code == 200:
            ok("Workflow GET", f"step_count={r.json().get('step_count')}")
        else:
            fail("Workflow GET", f"HTTP {r.status_code}")

        r = client.delete(f"/api/v1/workflows/{wf_id}", headers=auth_headers_jwt)
        if r.status_code == 200 and r.json().get("status") == "cancelled":
            ok("Workflow cancel")
        else:
            fail("Workflow cancel", f"HTTP {r.status_code}: {r.text[:40]}")

    # ── Summary ───────────────────────────────────────────────────────────────
    total = passed + failed
    print(f"\n  {'─' * 36}")
    print(f"  {GREEN}Passed{RESET}: {passed}   {RED}Failed{RESET}: {failed}   Total: {total}")
    print(f"  {'─' * 36}\n")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RaBbLE API test client")
    parser.add_argument("base", nargs="?", default="http://localhost:8000",
                        help="API base URL (default: http://localhost:8000)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print full response bodies")
    args = parser.parse_args()
    sys.exit(run(args.base, args.verbose))
