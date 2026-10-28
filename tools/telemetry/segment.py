#!/usr/bin/env python3
"""Segment a dual-boot experiment capture into schedule stages and compare them.

Usage:
    python3 segment.py <capture_dir> [--skip-start 3] [--out report.md]

<capture_dir> must contain:
    schedule.json          written by `apply.py sequence`
    server/<session_*>/    server telemetry copied by collect.ps1
    client/<session_*>/    Quest telemetry pulled by collect.ps1

The schedule timestamps are Windows-epoch (apply.py runs on the Windows PC).
Server logs use the same clock. Client (Quest) logs use their own clock, so each
client session is aligned through its header against the paired server session
header (both are written at stream start).

The first seconds of every stage are skipped because RealTimeConfig updates
arrive with up to 1 s delay and settings settle.
"""

import argparse
import json
import statistics
import sys
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from analyze import iter_rows, percentile  # noqa: E402


def session_dirs(capture_dir, kind):
    base = capture_dir / kind
    if not base.is_dir():
        return []
    return sorted(path for path in base.iterdir() if path.is_dir())


def first_header_time(directory, name):
    for row in iter_rows(directory, name):
        if row.get("type") == "header":
            return row.get("unix_ms")
    return None


def client_clock_offsets(capture_dir):
    server_headers = [
        (directory, first_header_time(directory, "server"))
        for directory in session_dirs(capture_dir, "server")
    ]
    server_headers = [(d, t) for d, t in server_headers if t is not None]

    offsets = {}
    for client_dir in session_dirs(capture_dir, "client"):
        client_time = first_header_time(client_dir, "frames")
        if client_time is None:
            offsets[client_dir] = 0
            continue

        paired = min(
            server_headers,
            key=lambda item: abs(item[1] - client_time),
            default=None,
        )
        if paired is None or abs(paired[1] - client_time) > 120_000:
            print(f"WARNING: could not pair {client_dir.name}, assuming no clock skew")
            offsets[client_dir] = 0
        else:
            offsets[client_dir] = paired[1] - client_time

    return offsets


def bucket_for(timestamp_ms, stages, skip_start_ms):
    for stage in stages:
        start = stage["apply_unix_ms"] + skip_start_ms
        if start <= timestamp_ms < stage["end_unix_ms"]:
            return stage["index"]
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("capture_dir", type=Path)
    parser.add_argument("--skip-start", type=float, default=3.0)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    capture_dir = args.capture_dir
    schedule = json.loads((capture_dir / "schedule.json").read_text())
    stages = schedule["stages"]
    skip_start_ms = int(args.skip_start * 1000)

    stage_data = {}
    for stage in stages:
        stage_data[stage["index"]] = {
            "label": stage["label"],
            "total": array("f"),
            "decode": array("f"),
            "queue": array("f"),
            "vsync": array("f"),
            "intervals": array("f"),
            "encoder": array("f"),
            "network": array("f"),
            "outside": 0,
        }

    offsets = client_clock_offsets(capture_dir)

    for client_dir in session_dirs(capture_dir, "client"):
        offset = offsets.get(client_dir, 0)
        for row in iter_rows(client_dir, "frames"):
            if row.get("type") != "frame":
                continue
            index = bucket_for(row.get("unix_ms", 0) + offset, stages, skip_start_ms)
            if index is None:
                continue
            data = stage_data[index]
            if row.get("total_pipeline_latency_us"):
                data["total"].append(row["total_pipeline_latency_us"] / 1000)
            if row.get("video_decode_us"):
                data["decode"].append(row["video_decode_us"] / 1000)
            if row.get("video_decoder_queue_us"):
                data["queue"].append(row["video_decoder_queue_us"] / 1000)
            if row.get("vsync_queue_us"):
                data["vsync"].append(row["vsync_queue_us"] / 1000)
            if row.get("frame_interval_us"):
                data["intervals"].append(row["frame_interval_us"] / 1000)

    for server_dir in session_dirs(capture_dir, "server"):
        for row in iter_rows(server_dir, "server"):
            index = bucket_for(row.get("unix_ms", 0), stages, skip_start_ms)
            if index is None:
                continue
            data = stage_data[index]
            if row.get("type") == "frame_encoded" and row.get("encoder_latency_us"):
                data["encoder"].append(row["encoder_latency_us"] / 1000)
            elif row.get("type") == "client_stats" and row.get("network_latency_us") is not None:
                data["network"].append(row["network_latency_us"] / 1000)

    def metric(values, p, digits=1):
        if not values:
            return "-"
        return f"{percentile(values, p):.{digits}f}"

    report = []
    report.append(f"# Segmented report — `{capture_dir}`")
    report.append("")
    report.append(f"- Stages: {len(stages)}")
    report.append(
        "| stage | seconds | frames | p50 total | p95 total | p99 total | spikes % | "
        "decode p50 | decode p99 | queue p50 | vsync p50 | encode p50 | net p50 |"
    )
    report.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")

    for stage in stages:
        data = stage_data[stage["index"]]
        duration = (stage["end_unix_ms"] - stage["apply_unix_ms"]) / 1000
        spikes = "-"
        if data["intervals"]:
            median_interval = statistics.median(data["intervals"])
            spikes = (
                f"{100 * sum(1 for v in data['intervals'] if v > median_interval * 1.5) / len(data['intervals']):.2f}"
            )
        report.append(
            f"| {stage['label']} | {duration:.0f} | {len(data['total'])} | "
            f"{metric(data['total'], 50)} | {metric(data['total'], 95)} | {metric(data['total'], 99)} | "
            f"{spikes} | {metric(data['decode'], 50)} | {metric(data['decode'], 99)} | "
            f"{metric(data['queue'], 50)} | {metric(data['vsync'], 50)} | "
            f"{metric(data['encoder'], 50)} | {metric(data['network'], 50)} |"
        )

    empty = [stage["label"] for stage in stages if not stage_data[stage["index"]]["total"]]
    if empty:
        report.append("")
        report.append(f"WARNING: no client frames found for: {', '.join(empty)}")

    text = "\n".join(report) + "\n"
    if args.out:
        args.out.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
