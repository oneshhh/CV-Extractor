#!/usr/bin/env python3
"""
safe_load_test.py

Safe, controlled load tester using asyncio + aiohttp.

Defaults are conservative. Increase values only after monitoring the server.

Usage:
    python safe_load_test.py

Configuration at top of file:
    TARGET_URL: URL to test (must include scheme, e.g. https://...)
    VUS: number of concurrent virtual users
    DURATION: overall test duration in seconds
    MAX_RPS_PER_VU: maximum requests per second per virtual user
    ERROR_THRESHOLD: fraction of total requests that triggers an automatic stop (0-1)

This script is provided for testing servers you own or have explicit permission to test.
Do NOT use it to attack or degrade services you don't control.
"""

import asyncio
import aiohttp
import time
import random
import statistics
import sys
from collections import defaultdict

### === CONFIG ===
TARGET_URL = "https://timesheet.globalxperts.cloud/"   # <-- set your target
VUS = 20                       # virtual users (concurrent tasks)
DURATION = 30                  # seconds
MAX_RPS_PER_VU = 10             # requests per second per VU (throttle)
ERROR_THRESHOLD = 0.10         # stop if > 10% of requests fail
REQUEST_TIMEOUT = 15           # seconds
USER_AGENT = "SafeLoadTester/1.0 (+https://globalxperts.cloud)"
### === END CONFIG ===

# Internal globals
_stats = {
    "total_requests": 0,
    "successful_requests": 0,
    "failed_requests": 0,
    "statuses": defaultdict(int),
    "latencies": [],  # ms
    "per_second": defaultdict(lambda: {"requests":0, "errors":0}),
}
_stop_flag = False

async def worker(name: int, session: aiohttp.ClientSession, stop_time: float):
    """
    A single virtual user worker loop.
    Each loop: wait randomized time (to desync), send request, collect stats,
    then sleep to maintain max RPS per VU.
    """
    global _stop_flag
    min_interval = 1.0 / MAX_RPS_PER_VU if MAX_RPS_PER_VU > 0 else 0.0

    # small initial jitter to avoid synchronized bursts
    await asyncio.sleep(random.uniform(0, min_interval))

    while not _stop_flag and time.time() < stop_time:
        start = time.time()
        sec = int(start)
        try:
            # perform GET request
            async with session.get(TARGET_URL, timeout=REQUEST_TIMEOUT) as resp:
                status = resp.status
                text = None
                # optionally read a small portion (not the whole body) to measure time
                await resp.content.read(1)
                latency_ms = (time.time() - start) * 1000.0

                _stats["total_requests"] += 1
                _stats["latencies"].append(latency_ms)
                _stats["statuses"][status] += 1
                _stats["successful_requests"] += 1 if 200 <= status < 400 else 0
                if not (200 <= status < 400):
                    _stats["failed_requests"] += 1
                    _stats["per_second"][sec]["errors"] += 1
                _stats["per_second"][sec]["requests"] += 1

        except asyncio.TimeoutError:
            _stats["total_requests"] += 1
            _stats["failed_requests"] += 1
            _stats["per_second"][sec]["errors"] += 1
            _stats["per_second"][sec]["requests"] += 1
        except aiohttp.ClientError:
            _stats["total_requests"] += 1
            _stats["failed_requests"] += 1
            _stats["per_second"][sec]["errors"] += 1
            _stats["per_second"][sec]["requests"] += 1
        except Exception:
            _stats["total_requests"] += 1
            _stats["failed_requests"] += 1
            _stats["per_second"][sec]["errors"] += 1
            _stats["per_second"][sec]["requests"] += 1

        # check error threshold and set stop flag if exceeded
        if _stats["total_requests"] > 0:
            err_rate = _stats["failed_requests"] / _stats["total_requests"]
            if err_rate > ERROR_THRESHOLD:
                print(f"\n[STOP] Error rate exceeded threshold: {err_rate:.2%} > {ERROR_THRESHOLD:.2%}")
                _stop_flag = True
                break

        # sleep to maintain max RPS per VU plus small random jitter
        elapsed = time.time() - start
        to_sleep = max(0.0, min_interval - elapsed)
        # add tiny random jitter to avoid perfect sync
        to_sleep += random.uniform(0, min_interval * 0.1)
        if to_sleep > 0:
            await asyncio.sleep(to_sleep)

async def reporter(total_duration: int, interval: int = 1):
    """
    Periodic reporter that prints per-second statistics.
    """
    start = time.time()
    last_print = start
    while not _stop_flag and time.time() - start < total_duration:
        await asyncio.sleep(interval)
        now = int(time.time())
        # compute last interval stats
        reqs = _stats["per_second"].get(now - 1, {"requests":0})["requests"]
        errs = _stats["per_second"].get(now - 1, {"errors":0})["errors"]
        total = _stats["total_requests"]
        failures = _stats["failed_requests"]
        avg_latency = statistics.mean(_stats["latencies"]) if _stats["latencies"] else 0.0
        print(f"[{time.strftime('%H:%M:%S')}] last-sec reqs={reqs} errs={errs} total={total} failures={failures} avg-lat-ms={avg_latency:.1f}")
        last_print = now

async def run_test():
    global _stop_flag
    _stop_flag = False
    stop_time = time.time() + DURATION

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    headers = {"User-Agent": USER_AGENT}

    connector = aiohttp.TCPConnector(limit=0)  # no connection limit at connector level
    async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers) as session:
        # start workers and reporter
        tasks = []
        for i in range(VUS):
            tasks.append(asyncio.create_task(worker(i, session, stop_time)))
        tasks.append(asyncio.create_task(reporter(DURATION)))
        # wait until all workers finish or stop flag triggered
        await asyncio.gather(*tasks, return_exceptions=True)

def print_summary():
    total = _stats["total_requests"]
    succ = _stats["successful_requests"]
    fail = _stats["failed_requests"]
    statuses = dict(_stats["statuses"])
    latencies = _stats["latencies"]
    avg = statistics.mean(latencies) if latencies else 0
    p95 = statistics.quantiles(latencies, n=100)[94] if len(latencies) >= 100 else (max(latencies) if latencies else 0)
    print("\n=== Test Summary ===")
    print(f"Target: {TARGET_URL}")
    print(f"Duration requested: {DURATION}s")
    print(f"VUs: {VUS}, max RPS per VU: {MAX_RPS_PER_VU}")
    print(f"Total requests: {total}")
    print(f"Successful requests (2xx-3xx): {succ}")
    print(f"Failed requests: {fail}")
    print(f"Status codes: {statuses}")
    print(f"Avg latency (ms): {avg:.1f}")
    print(f"P95 latency (ms): {p95:.1f}")
    if total:
        print(f"Error rate: {fail/total:.2%}")

def main():
    print("SAFE LOAD TESTER")
    print("Target:", TARGET_URL)
    print(f"VUs={VUS}, Duration={DURATION}s, MAX_RPS_PER_VU={MAX_RPS_PER_VU}, ERROR_THRESHOLD={ERROR_THRESHOLD:.2%}")
    print("Starting in 3 seconds... (press Ctrl+C to abort)")
    try:
        time.sleep(3)
        asyncio.run(run_test())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    finally:
        print_summary()

if __name__ == "__main__":
    main()
