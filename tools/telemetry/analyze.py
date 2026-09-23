#!/usr/bin/env python3
"""ALVR telemetry analyzer for Beat Saber sessions.

Usage:
    python3 analyze.py <session_dir> [--plots] [--out <report.md>]

Where <session_dir> contains any of:
    input.jsonl   (Quest client: per-poll controller/head poses + prediction residual)
    frames.jsonl  (Quest client: per-frame pipeline latencies)
    server.jsonl  (PC: encoder latency, network latency, tracking transport jitter)

Prints a markdown report. With --plots, writes PNG charts next to the logs.
"""

import argparse
import json
import math
import statistics
import sys
from pathlib import Path


def load_jsonl(path: Path):
    rows = []
    if not path.exists():
        return rows
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def percentile(values, p):
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * p / 100)))
    return ordered[idx]


def fmt(value, digits=1, suffix=""):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-"
    return f"{value:.{digits}f}{suffix}"


def angvel_magnitude_deg(sample):
    av = sample.get("av", [0, 0, 0])
    return math.sqrt(av[0] ** 2 + av[1] ** 2 + av[2] ** 2)


def residual_profile(samples, velocity_fn, residual_fn, buckets=((100, "0-100"), (500, "100-500"), (1000, "500-1000"), (2000, "1000-2000"), (float("inf"), "2000+"))):
    """Residual percentiles bucketed by angular velocity of the device."""
    rows = []
    lower = 0.0
    for limit, label in buckets:
        group = [
            residual_fn(s)
            for s in samples
            if lower < velocity_fn(s) <= limit and residual_fn(s) is not None
        ]
        rows.append(
            (
                label,
                percentile(group, 50),
                percentile(group, 95),
                percentile(group, 99),
                len(group),
            )
        )
        lower = limit
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--plots", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    session_dir = args.session_dir
    input_rows = load_jsonl(session_dir / "input.jsonl")
    frame_rows = load_jsonl(session_dir / "frames.jsonl")
    server_rows = load_jsonl(session_dir / "server.jsonl")

    headers = [r for r in input_rows + frame_rows + server_rows if r.get("type") == "header"]
    settings = headers[0]["settings"] if headers else {}

    report = []
    report.append(f"# ALVR telemetry report — `{session_dir}`")
    report.append("")
    if settings:
        report.append("## Session settings")
        for key, value in settings.items():
            report.append(f"- {key}: `{value}`")
        report.append("")

    # ---------- input.jsonl ----------
    input_data = [r for r in input_rows if r.get("type") == "input"]
    if input_data:
        report.append("## Tracking / wobble (input.jsonl)")
        duration_s = (input_data[-1]["unix_ms"] - input_data[0]["unix_ms"]) / 1000
        report.append(f"- Samples: {len(input_data)} over {duration_s:.1f}s "
                      f"({len(input_data) / max(duration_s, 1e-6):.0f} Hz)")

        for label, key in (("Head", "head"), ):
            samples = [r[key] for r in input_data]
            res_pos = [s["res_pos_mm"] for s in samples if s.get("res_pos_mm") is not None]
            res_ori = [s["res_ori_deg"] for s in samples if s.get("res_ori_deg") is not None]
            report.append(
                f"- {label} residual: p50 {fmt(percentile(res_pos, 50), 2, 'mm')} / "
                f"p95 {fmt(percentile(res_pos, 95), 2, 'mm')} / "
                f"p99 {fmt(percentile(res_pos, 99), 2, 'mm')}; "
                f"orient p50 {fmt(percentile(res_ori, 50), 3, '°')} / "
                f"p99 {fmt(percentile(res_ori, 99), 3, '°')}"
            )

        for hand_name, hand_id in (("Left hand", 0), ("Right hand", 1)):
            samples = [
                next(s for s in r["hands"] if s["id"] == hand_id)
                for r in input_data
                if any(h["id"] == hand_id for h in r["hands"])
            ]
            if not samples:
                continue
            res_pos = [s["res_pos_mm"] for s in samples if s.get("res_pos_mm") is not None]
            res_ori = [s["res_ori_deg"] for s in samples if s.get("res_ori_deg") is not None]
            report.append("")
            report.append(f"### {hand_name} (id={hand_id})")
            report.append(
                f"- Residual (position): p50 {fmt(percentile(res_pos, 50), 2, 'mm')} / "
                f"p95 {fmt(percentile(res_pos, 95), 2, 'mm')} / "
                f"p99 {fmt(percentile(res_pos, 99), 2, 'mm')}"
            )
            report.append(
                f"- Residual (orientation): p50 {fmt(percentile(res_ori, 50), 3, '°')} / "
                f"p95 {fmt(percentile(res_ori, 95), 3, '°')} / "
                f"p99 {fmt(percentile(res_ori, 99), 3, '°')}"
            )

            # Angular velocity ranges for the table below
            def velocity_floor(label):
                if label == "0-100":
                    return 0.0
                if label.endswith("+"):
                    return float(label[:-1].split("-")[-1])
                return float(label.split("-")[0])

            rows = residual_profile(
                samples, angvel_magnitude_deg, lambda s: s.get("res_pos_mm")
            )
            report.append("")
            report.append("| Angular velocity (°/s) | n | pos p50 | pos p95 | pos p99 | ori p50 | ori p99 |")
            report.append("|---|---|---|---|---|---|---|")
            for label, p50, p95, p99, n in rows:
                floor = velocity_floor(label)
                ceiling = _bucket_limit(label)
                ori_group = [
                    s.get("res_ori_deg")
                    for s in samples
                    if floor < angvel_magnitude_deg(s) <= ceiling
                    and s.get("res_ori_deg") is not None
                ]
                report.append(
                    f"| {label} | {n} | {fmt(p50, 2, 'mm')} | {fmt(p95, 2, 'mm')} | "
                    f"{fmt(p99, 2, 'mm')} | {fmt(percentile(ori_group, 50), 3, '°')} | "
                    f"{fmt(percentile(ori_group, 99), 3, '°')} |"
                )
        report.append("")

    # ---------- frames.jsonl ----------
    frame_data = [r for r in frame_rows if r.get("type") == "frame"]
    if frame_data:
        report.append("## Frame pipeline (frames.jsonl)")
        intervals_ms = [r["frame_interval_us"] / 1000 for r in frame_data if r["frame_interval_us"]]
        intervals_ms = [i for i in intervals_ms if i > 0]
        if intervals_ms:
            median_interval = statistics.median(intervals_ms)
            report.append(f"- Frames: {len(frame_data)}, median interval {median_interval:.2f} ms "
                          f"({1000 / median_interval:.1f} fps)")
            spikes = sum(1 for i in intervals_ms if i > median_interval * 1.5)
            report.append(f"- Frame time spikes (>1.5×): {spikes} ({100 * spikes / len(intervals_ms):.2f}%)")

        total = [r["total_pipeline_latency_us"] / 1000 for r in frame_data if r["total_pipeline_latency_us"]]
        report.append(
            f"- Total pipeline latency: p50 {fmt(percentile(total, 50))} ms / "
            f"p95 {fmt(percentile(total, 95))} ms / p99 {fmt(percentile(total, 99))} ms"
        )
        for label, key in (
            ("decode", "video_decode_us"),
            ("decoder queue", "video_decoder_queue_us"),
            ("render", "rendering_us"),
            ("vsync queue", "vsync_queue_us"),
        ):
            values = [r[key] / 1000 for r in frame_data if r[key]]
            report.append(
                f"- {label}: p50 {fmt(percentile(values, 50))} ms / p99 {fmt(percentile(values, 99))} ms"
            )
        report.append("")

    # ---------- server.jsonl ----------
    encoded = [r for r in server_rows if r.get("type") == "frame_encoded"]
    client_stats = [r for r in server_rows if r.get("type") == "client_stats"]
    tracking = [r for r in server_rows if r.get("type") == "tracking_received"]

    if any((encoded, client_stats, tracking)):
        report.append("## Server side (server.jsonl)")

    if encoded:
        enc = [r["encoder_latency_us"] / 1000 for r in encoded]
        sizes = [r["buffer_size"] for r in encoded]
        report.append(
            f"- Encoder: p50 {fmt(percentile(enc, 50))} ms / p95 {fmt(percentile(enc, 95))} ms / "
            f"p99 {fmt(percentile(enc, 99))} ms over {len(encoded)} frames; "
            f"mean frame size {statistics.mean(sizes) / 1000:.0f} kB"
        )
    if client_stats:
        net = [r["network_latency_us"] / 1000 for r in client_stats]
        report.append(
            f"- Network latency: p50 {fmt(percentile(net, 50))} ms / p95 {fmt(percentile(net, 95))} ms / "
            f"p99 {fmt(percentile(net, 99))} ms"
        )
    if tracking:
        inter = [r["interarrival_us"] / 1000 for r in tracking if r.get("interarrival_us")]
        if inter:
            median_inter = statistics.median(inter)
            jitter = [abs(i - median_inter) for i in inter]
            report.append(
                f"- Tracking transport: median interarrival {median_inter:.2f} ms, "
                f"jitter p95 {fmt(percentile(jitter, 95))} ms"
            )
    if any((encoded, client_stats, tracking)):
        report.append("")

    # ---------- plots ----------
    if args.plots:
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            print("matplotlib not available, skipping plots", file=sys.stderr)
            return finish(report, args.out)

        if input_data:
            fig, ax = plt.subplots(figsize=(10, 4))
            for hand_id, label, color in ((0, "left", "tab:blue"), (1, "right", "tab:orange")):
                points = [
                    (r["unix_ms"], next(s["res_pos_mm"] for s in r["hands"] if s["id"] == hand_id))
                    for r in input_data
                    if any(h["id"] == hand_id and h.get("res_pos_mm") is not None for h in r["hands"])
                ]
                if points:
                    t0 = points[0][0]
                    ax.plot(
                        [(t - t0) / 1000 for t, _ in points],
                        [v for _, v in points],
                        label=f"{label} hand",
                        color=color,
                        linewidth=0.6,
                    )
            ax.set_xlabel("session time (s)")
            ax.set_ylabel("controller pose residual (mm)")
            ax.set_title("Tracking revision residual (wobble)")
            ax.legend()
            fig.savefig(session_dir / "residual.png", dpi=120)

        if frame_data:
            fig, ax = plt.subplots(figsize=(10, 4))
            t0 = frame_data[0]["unix_ms"]
            total = [
                (r["unix_ms"], r["total_pipeline_latency_us"] / 1000)
                for r in frame_data
                if r["total_pipeline_latency_us"]
            ]
            ax.plot([(t - t0) / 1000 for t, _ in total], [v for _, v in total], linewidth=0.6)
            ax.set_xlabel("session time (s)")
            ax.set_ylabel("total pipeline latency (ms)")
            ax.set_title("End-to-end latency")
            fig.savefig(session_dir / "latency.png", dpi=120)

        report.append("## Plots")
        report.append("- `residual.png`, `latency.png` written to session dir")

    return finish(report, args.out)


def _bucket_limit(label):
    if label.endswith("+"):
        return float("inf")
    return float(label.split("-")[1])


def finish(report, out_path):
    text = "\n".join(report) + "\n"
    if out_path:
        out_path.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
