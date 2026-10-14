"""Benchmark HROne endpoints over HTTP; uses only Python standard library."""
import argparse
import json
import statistics
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode


def request_json(url, timeout):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            elapsed = (time.perf_counter() - started) * 1000
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = None
            return response.status, elapsed, len(body), payload, None
    except urllib.error.HTTPError as exc:
        body = exc.read()
        elapsed = (time.perf_counter() - started) * 1000
        return exc.code, elapsed, len(body), None, body[:500].decode("utf-8", "replace")
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        return 0, elapsed, 0, None, str(exc)


def p95(values):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    cases = [
        ("attendance_page_100", "/attendance", {"page": 1, "page_size": 100}),
        ("attendance_date_range", "/attendance", {"date_from": "2026-01-01", "date_to": "2026-04-10", "page": 1, "page_size": 100}),
        ("attendance_status_filter", "/attendance", {"status": "PRESENT", "page": 1, "page_size": 100}),
        ("employee_monthly", "/analytics/employees/EMP0001/monthly", {"month": "2026-03"}),
        ("department_summary", "/analytics/departments/summary", {"month": "2026-03"}),
        ("late_leaderboard", "/analytics/leaderboard/late", {"month": "2026-03", "limit": 10}),
        ("department_trend_90d", "/analytics/departments/Engineering/trend", {"from": "2026-01-01", "to": "2026-03-31"}),
        ("explain_attendance_list", "/admin/explain/attendance_list", {"date_from": "2026-01-01", "date_to": "2026-04-10", "page": 1, "page_size": 100}),
    ]

    print(f"Base URL: {base}")
    print(f"Runs per case: {args.runs}; warmups: {args.warmups}")
    failures = 0
    for name, path, params in cases:
        url = f"{base}{path}?{urlencode(params)}"
        for _ in range(args.warmups):
            status, _, _, _, error = request_json(url, args.timeout)
            if status != 200:
                print(f"FAIL {name}: warmup HTTP {status}: {error or 'unexpected response'}")
                failures += 1
                break
        else:
            times = []
            sizes = []
            last_payload = None
            for _ in range(args.runs):
                status, elapsed, size, payload, error = request_json(url, args.timeout)
                if status != 200:
                    print(f"FAIL {name}: HTTP {status}: {error or 'unexpected response'}")
                    failures += 1
                    break
                times.append(elapsed)
                sizes.append(size)
                last_payload = payload
            if times:
                print(
                    f"{name}: median={statistics.median(times):.1f}ms "
                    f"p95={p95(times):.1f}ms min={min(times):.1f}ms "
                    f"response={max(sizes):,} bytes status=200"
                )
                if name == "explain_attendance_list" and isinstance(last_payload, dict):
                    explain = last_payload.get("explain", {})
                    stats = explain.get("executionStats", {}) if isinstance(explain, dict) else {}
                    print("  explain executionStats:", json.dumps({
                        "executionTimeMillis": stats.get("executionTimeMillis"),
                        "nReturned": stats.get("nReturned"),
                        "totalKeysExamined": stats.get("totalKeysExamined"),
                        "totalDocsExamined": stats.get("totalDocsExamined"),
                    }))

    print(f"Completed with {failures} failed case(s).")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
