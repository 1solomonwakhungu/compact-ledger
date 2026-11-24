#!/usr/bin/env python3
"""Reproducible latency, footprint, and eval benchmark for compact-ledger.

Run with:
    uv run python evals/bench.py
    uv run python evals/bench.py --json
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from compact_ledger.evals import run_eval_suite
from compact_ledger.hooks import handle_pre_compact, handle_session_start
from compact_ledger.renderer import render_snapshot
from compact_ledger.store import SessionStore


def _build_realistic_transcript(path: Path, *, repeat_noise: int = 1) -> int:
    """Write a realistic multi-turn Claude Code JSONL transcript and return byte size."""
    sid = "bench-session"
    pytest_fail_1 = (
        "============================= test session starts ==============================\n"
        "=================================== FAILURES ===================================\n"
        "________________________________ test_tax_cap __________________________________\n"
        ">       assert compute_tax_cap() == 100\n"
        "E       assert 400 == 100\n\n"
        "tests/test_billing.py:88: AssertionError\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_billing.py::test_tax_cap - assert 400 == 100\n"
        "============================== 1 failed in 0.05s ===============================\n"
    )
    pytest_fail_2 = pytest_fail_1.replace("400", "200")
    records: list[dict[str, Any]] = [
        {
            "type": "user",
            "sessionId": sid,
            "timestamp": "2026-11-24T10:00:00Z",
            "message": {
                "role": "user",
                "content": (
                    "Refactor the billing rollover service.\n\n"
                    "## Rules\n"
                    "- Use June 30 as the billing cutoff date.\n"
                    "- Never touch migrations under db/migrations/.\n"
                    "- Don't add any new third-party dependencies.\n"
                    "- All money math must use Decimal, never floats.\n"
                ),
            },
        },
        {
            "type": "user",
            "sessionId": sid,
            "timestamp": "2026-11-24T10:01:00Z",
            "message": {
                "role": "user",
                "content": "Actually use July 31 instead of June 30 for the billing cutoff.",
            },
        },
        {
            "type": "user",
            "sessionId": sid,
            "timestamp": "2026-11-24T10:02:00Z",
            "message": {"role": "user", "content": "ok sounds good"},
        },
    ]

    for i in range(repeat_noise):
        records.extend(
            [
                {
                    "type": "assistant",
                    "sessionId": sid,
                    "timestamp": f"2026-11-24T10:{10 + (i % 40):02d}:01Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": f"e1_{i}",
                                "name": "Edit",
                                "input": {
                                    "file_path": "/repo/src/billing/tax.py",
                                    "old_string": "return 400",
                                    "new_string": "return 200",
                                },
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "sessionId": sid,
                    "timestamp": f"2026-11-24T10:{10 + (i % 40):02d}:02Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": f"e1_{i}", "content": "ok"}
                        ],
                    },
                },
                {
                    "type": "assistant",
                    "sessionId": sid,
                    "timestamp": f"2026-11-24T10:{10 + (i % 40):02d}:03Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": f"b1_{i}",
                                "name": "Bash",
                                "input": {"command": "pytest tests/test_billing.py"},
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "sessionId": sid,
                    "timestamp": f"2026-11-24T10:{10 + (i % 40):02d}:04Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": f"b1_{i}",
                                "is_error": True,
                                "content": pytest_fail_1 if i % 2 == 0 else pytest_fail_2,
                            }
                        ],
                    },
                },
                {
                    "type": "assistant",
                    "sessionId": sid,
                    "timestamp": f"2026-11-24T10:{10 + (i % 40):02d}:05Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": f"e2_{i}",
                                "name": "Edit",
                                "input": {
                                    "file_path": "/repo/src/billing/tax.py",
                                    "old_string": "return 200",
                                    "new_string": "return 400",
                                },
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "sessionId": sid,
                    "timestamp": f"2026-11-24T10:{10 + (i % 40):02d}:06Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": f"e2_{i}", "content": "ok"}
                        ],
                    },
                },
            ]
        )

    raw = "\n".join(json.dumps(r) for r in records) + "\n"
    path.write_text(raw, encoding="utf-8")
    return len(raw.encode("utf-8"))


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[idx]


def _bench_ms(fn: Any, *, iterations: int, warmup: int = 3) -> dict[str, float]:
    for _ in range(warmup):
        fn()
    samples: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return {
        "median_ms": round(statistics.median(samples), 2),
        "p95_ms": round(_percentile(samples, 95.0), 2),
        "min_ms": round(min(samples), 2),
    }


def _bench_subprocess_ms(
    cmd: list[str],
    *,
    stdin_text: str | None = None,
    iterations: int = 15,
    warmup: int = 2,
) -> dict[str, float]:
    def _run() -> None:
        proc = subprocess.run(
            cmd,
            input=stdin_text,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"Benchmark command failed ({proc.returncode}): {proc.stderr}")

    return _bench_ms(_run, iterations=iterations, warmup=warmup)


def run_benchmarks() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="compact-ledger-bench-") as tmp:
        root = Path(tmp)
        store_dir = root / "store"
        store = SessionStore(store_dir=store_dir)

        small_transcript = root / "small.jsonl"
        small_bytes = _build_realistic_transcript(small_transcript, repeat_noise=2)

        large_transcript = root / "large.jsonl"
        large_bytes = _build_realistic_transcript(large_transcript, repeat_noise=80)

        pre_small_payload = json.dumps(
            {
                "session_id": "bench-session",
                "transcript_path": str(small_transcript),
                "cwd": str(root),
            }
        )
        pre_large_payload = json.dumps(
            {
                "session_id": "bench-large",
                "transcript_path": str(large_transcript),
                "cwd": str(root),
            }
        )
        start_payload = json.dumps(
            {
                "session_id": "bench-session",
                "source": "compact",
                "cwd": str(root),
            }
        )

        # Seed session with a manual pin and snapshot
        store.add_manual_pin(
            "bench-session", "Zero third-party runtime dependencies.", tags=["arch"]
        )
        handle_pre_compact(pre_small_payload, store_dir=store_dir, strict=True)
        handle_pre_compact(pre_large_payload, store_dir=store_dir, strict=True)
        snap = store.load_snapshot("bench-session")
        large_snap = store.load_snapshot("bench-large")
        assert snap is not None and large_snap is not None

        rendered_default = render_snapshot(snap, max_chars=6000)
        rendered_large = render_snapshot(large_snap, max_chars=6000)
        rendered_1200 = render_snapshot(snap, max_chars=1200)
        rendered_400 = render_snapshot(snap, max_chars=400)
        snapshot_file_bytes = store.session_path("bench-session").stat().st_size

        # In-process benchmarks (use distinct session/pin writes so fsync + atomic replace run)
        pin_write_counter = 0

        def _pin_atomic_write() -> None:
            nonlocal pin_write_counter
            pin_write_counter += 1
            store.add_manual_pin(
                "bench-pin-write",
                f"Rule #{pin_write_counter}: never touch db/migrations/",
                is_negative=True,
            )

        pin_inproc = _bench_ms(_pin_atomic_write, iterations=50)
        inject_inproc = _bench_ms(
            lambda: render_snapshot(snap, max_chars=6000),
            iterations=200,
        )
        pre_small_inproc = _bench_ms(
            lambda: handle_pre_compact(pre_small_payload, store_dir=store_dir, strict=True),
            iterations=50,
        )
        start_inproc = _bench_ms(
            lambda: handle_session_start(start_payload, store_dir=store_dir, strict=True),
            iterations=100,
        )
        pre_large_inproc = _bench_ms(
            lambda: handle_pre_compact(pre_large_payload, store_dir=store_dir, strict=True),
            iterations=20,
        )

        # Cold CLI subprocess benchmarks (includes Python interpreter startup)
        py_cmd = [sys.executable, "-m", "compact_ledger"]
        cli_pin_counter = 0

        def _cli_pin_cmd() -> None:
            nonlocal cli_pin_counter
            cli_pin_counter += 1
            proc = subprocess.run(
                [
                    *py_cmd,
                    "pin",
                    "--store-dir",
                    str(store_dir),
                    "--session-id",
                    "bench-cli-pin",
                    f"CLI rule #{cli_pin_counter}: keep runtime deps at zero.",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"CLI pin failed ({proc.returncode}): {proc.stderr}")

        pin_cli = _bench_ms(_cli_pin_cmd, iterations=15, warmup=2)
        inject_cli = _bench_subprocess_ms(
            [*py_cmd, "inject", "--store-dir", str(store_dir), "--session-id", "bench-session"],
            iterations=15,
        )
        pre_cli = _bench_subprocess_ms(
            [*py_cmd, "hook", "pre-compact", "--store-dir", str(store_dir), "--strict"],
            stdin_text=pre_small_payload,
            iterations=15,
        )
        start_cli = _bench_subprocess_ms(
            [*py_cmd, "hook", "session-start", "--store-dir", str(store_dir), "--strict"],
            stdin_text=start_payload,
            iterations=15,
        )

        eval_report = run_eval_suite()

        return {
            "environment": {
                "python": platform.python_version(),
                "platform": f"{platform.system()} {platform.machine()}",
            },
            "in_process": {
                "pin_atomic_write": pin_inproc,
                "inject_render_6000": inject_inproc,
                "hook_pre_compact_small": {**pre_small_inproc, "transcript_bytes": small_bytes},
                "hook_session_start": start_inproc,
                "hook_pre_compact_large": {**pre_large_inproc, "transcript_bytes": large_bytes},
            },
            "cli_subprocess": {
                "pin": pin_cli,
                "inject": inject_cli,
                "hook_pre_compact_small": pre_cli,
                "hook_session_start": start_cli,
            },
            "footprint": {
                "small_transcript_bytes": small_bytes,
                "large_transcript_bytes": large_bytes,
                "snapshot_json_bytes": snapshot_file_bytes,
                "rendered_default_chars": len(rendered_default),
                "rendered_default_approx_tokens": round(len(rendered_default) / 4),
                "rendered_large_chars": len(rendered_large),
                "rendered_large_approx_tokens": round(len(rendered_large) / 4),
                "rendered_1200_chars": len(rendered_1200),
                "rendered_400_chars": len(rendered_400),
            },
            "eval_suite": {
                "passed": eval_report.passed,
                "total": eval_report.total,
                "pass_rate": round(eval_report.pass_rate, 2),
                "elapsed_ms": round(eval_report.elapsed_ms, 2),
                "categories": eval_report.category_summary(),
            },
        }


def _fmt_ms(stats: dict[str, Any]) -> str:
    return f"{stats['median_ms']:6.2f} ms / {stats['p95_ms']:6.2f} ms"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run reproducible compact-ledger benchmarks.")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Print JSON output.")
    args = parser.parse_args(argv)

    data = run_benchmarks()
    if args.as_json:
        print(json.dumps(data, indent=2))
        return 0

    env = data["environment"]
    print(f"compact-ledger benchmark ({env['platform']}, Python {env['python']})")
    print("-" * 72)
    print("In-process latency (median / p95):")
    ip = data["in_process"]
    small_b = ip["hook_pre_compact_small"]["transcript_bytes"]
    large_b = ip["hook_pre_compact_large"]["transcript_bytes"]
    rows = [
        ("add_manual_pin (atomic JSON+fsync)", ip["pin_atomic_write"]),
        ("render_snapshot (6000 cap)", ip["inject_render_6000"]),
        (f"hook pre-compact ({small_b:,} B)", ip["hook_pre_compact_small"]),
        (f"hook pre-compact ({large_b:,} B)", ip["hook_pre_compact_large"]),
        ("hook session-start (load+render)", ip["hook_session_start"]),
    ]
    for label, stats in rows:
        print(f"  {label:<36} {_fmt_ms(stats)}")

    print("\nCold CLI subprocess latency including Python startup (median / p95):")
    cli = data["cli_subprocess"]
    for label, key in (
        ("compact-ledger pin", "pin"),
        ("compact-ledger inject", "inject"),
        ("compact-ledger hook pre-compact", "hook_pre_compact_small"),
        ("compact-ledger hook session-start", "hook_session_start"),
    ):
        print(f"  {label:<36} {_fmt_ms(cli[key])}")

    fp = data["footprint"]
    print("\nFootprint:")
    print(
        f"  Small transcript ({fp['small_transcript_bytes']:,} B) -> "
        f"snapshot JSON {fp['snapshot_json_bytes']:,} B -> "
        f"Markdown {fp['rendered_default_chars']:,} chars "
        f"(~{fp['rendered_default_approx_tokens']} tokens)"
    )
    print(
        f"  Large transcript ({fp['large_transcript_bytes']:,} B) -> "
        f"Markdown {fp['rendered_large_chars']:,} chars "
        f"(~{fp['rendered_large_approx_tokens']} tokens)"
    )
    print(
        f"  Strict cap check: <=1200 -> {fp['rendered_1200_chars']} chars, "
        f"<=400 -> {fp['rendered_400_chars']} chars"
    )

    ev = data["eval_suite"]
    print(
        f"\nEval suite: {ev['passed']}/{ev['total']} passed ({ev['pass_rate']:.1f}%) "
        f"in {ev['elapsed_ms']:.1f} ms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
