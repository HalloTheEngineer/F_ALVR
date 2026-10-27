#!/usr/bin/env python3
"""ALVR telemetry analyzer for Beat Saber sessions.

Usage:
    python3 analyze.py <dir> [<dir> ...] [--plots] [--out <report.md>]

Each <dir> is an ALVR telemetry session directory. Provide the server session dir and/or the
client (Quest) session dir; both are auto-detected. Rotated files (server_r*.jsonl,
input_r*.jsonl, frames_r*.jsonl) are read automatically.

Files inside a session dir:
    input.jsonl   (Quest client: per-poll controller/head poses + prediction residual)
    frames.jsonl  (Quest client: per-frame pipeline latencies)
    server.jsonl  (PC: encoder latency, network latency, tracking transport jitter)

Prints a markdown report. With --plots, writes PNG charts into the first given directory.
Reads line by line, so multi-GB sessions are fine.
"""

import argparse
import json
import math
import statistics
from array import array
from pathlib import Path

PLOT_POINTS = 20000

# alvr_common::inputs: hash_string("/user/hand/left") and hash_string("/user/hand/right").
# Stable across builds; used to tell the controllers apart in the client logs.
HAND_LEFT_ID = 16510716142922041250
HAND_RIGHT_ID = 17777980629871082968


def data_files(directory: Path, name: str):
    rotated = sorted(
        directory.glob(f"{name}_r*.jsonl"),
        key=lambda path: int(path.stem.rsplit("_r", 1)[1]),
    )
    main = directory / f"{name}.jsonl"
    files = [*rotated]
    if main.exists():
        files.append(main)
    return files


def iter_rows(directory: Path, name: str):
    for path in data_files(directory, name):
        with open(path) as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    pass


def percentile(values, p):
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round((len(ordered) - 1) * p / 100)))]


def fmt(value, digits=1, suffix=""):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-"
    return f"{value:.{digits}f}{suffix}"


class Series:
    """Downsampled time series for plots plus full-data percentile arrays."""

    def __init__(self):
        self.downsample = 64
        self.count = 0
        self.plot_x = array("d")
        self.plot_y = array("f")
        self.values = array("f")

    def add(self, timestamp_ms, value):
        if value is None:
            return
        self.values.append(value)
        if self.count % self.downsample == 0:
            self.plot_x.append(timestamp_ms)
            self.plot_y.append(value)
        self.count += 1

    def p(self, p):
        return percentile(self.values, p)


class HandSeries:
    def __init__(self):
        self.angular_velocity = array("f")
        self.residual_position = array("f")
        self.residual_orientation = array("f")
        self.residual_position_time = Series()
        self.samples = 0
        self.invalid_residuals = 0

    def add(self, timestamp_ms, sample, has_residual):
        self.samples += 1
        if not has_residual:
            self.invalid_residuals += 1
            self.angular_velocity.append(-1.0)
            return
        # The client logs velocities in rad/s
        angular_velocity = math.degrees(
            math.sqrt(sum(component**2 for component in sample.get("av", [0, 0, 0])))
        )
        self.angular_velocity.append(angular_velocity)
        self.residual_position.append(sample["res_pos_mm"])
        self.residual_orientation.append(sample["res_ori_deg"])
        self.residual_position_time.add(timestamp_ms, sample["res_pos_mm"])


def velocity_bucket(angular_velocity_deg_s):
    for limit in (100, 500, 1000, 2000):
        if angular_velocity_deg_s <= limit:
            return f"<={limit}"
    return ">2000"


def analyze_input(directory: Path, report: list):
    hands = [HandSeries(), HandSeries(), HandSeries()]  # left, right, head
    first_timestamp = None
    last_timestamp = None
    sample_count = 0

    for row in iter_rows(directory, "input"):
        if row.get("type") != "input":
            continue
        timestamp_ms = row.get("unix_ms", 0)
        if first_timestamp is None:
            first_timestamp = timestamp_ms
        last_timestamp = timestamp_ms
        sample_count += 1

        head = row.get("head")
        if head is not None:
            hands[2].add(timestamp_ms, head, head.get("res_pos_mm") is not None)

        for hand_id, device_id in enumerate((HAND_LEFT_ID, HAND_RIGHT_ID)):
            samples = row.get("hands", [])
            hand_sample = next((s for s in samples if s.get("id") == device_id), None)
            if hand_sample is None:
                continue
            hands[hand_id].add(
                timestamp_ms, hand_sample, hand_sample.get("res_pos_mm") is not None
            )

    if sample_count == 0:
        return None

    duration_s = max((last_timestamp - first_timestamp) / 1000, 1e-6)
    report.append("## Tracking / wobble (client input)")
    report.append(
        f"- {sample_count} polls over {duration_s:.0f}s ({sample_count / duration_s:.0f} Hz)"
    )

    for hand_name, hand in (
        ("Left hand", hands[0]),
        ("Right hand", hands[1]),
        ("Head", hands[2]),
    ):
        if not hand.residual_position:
            continue
        report.append("")
        report.append(f"### {hand_name}")
        report.append(
            f"- Residual position: p50 {fmt(hand.residual_position_time.p(50), 2, ' mm')} / "
            f"p95 {fmt(hand.residual_position_time.p(95), 2, ' mm')} / "
            f"p99 {fmt(hand.residual_position_time.p(99), 2, ' mm')}"
        )
        report.append(
            f"- Residual orientation: p50 {fmt(percentile(hand.residual_orientation, 50), 3, '°')} / "
            f"p95 {fmt(percentile(hand.residual_orientation, 95), 3, '°')} / "
            f"p99 {fmt(percentile(hand.residual_orientation, 99), 3, '°')}"
        )
        report.append(f"- Samples: {hand.samples}, invalid/missing: {hand.invalid_residuals}")

        buckets = {}
        for angular_velocity, position, orientation in zip(
            hand.angular_velocity, hand.residual_position, hand.residual_orientation
        ):
            if angular_velocity < 0:
                continue
            label = velocity_bucket(angular_velocity)
            buckets.setdefault(label, ([], []))
            buckets[label][0].append(position)
            buckets[label][1].append(orientation)

        report.append("")
        report.append("| Angular velocity (°/s) | n | pos p50 | pos p99 | ori p50 | ori p99 |")
        report.append("|---|---|---|---|---|---|")
        for label in sorted(buckets, key=lambda l: 0 if l.startswith("<=") else 1):
            positions, orientations = buckets[label]
            report.append(
                f"| {label} | {len(positions)} | "
                f"{fmt(percentile(positions, 50), 2, ' mm')} | "
                f"{fmt(percentile(positions, 99), 2, ' mm')} | "
                f"{fmt(percentile(orientations, 50), 3, '°')} | "
                f"{fmt(percentile(orientations, 99), 3, '°')} |"
            )

    return hands


def analyze_frames(directory: Path, report: list):
    intervals = Series()
    total = Series()
    decode = Series()
    queue = Series()
    rendering = Series()
    vsync = Series()

    for row in iter_rows(directory, "frames"):
        if row.get("type") != "frame":
            continue
        timestamp_ms = row.get("unix_ms", 0)
        if row.get("frame_interval_us"):
            intervals.add(timestamp_ms, row["frame_interval_us"] / 1000)
        for series, key in (
            (total, "total_pipeline_latency_us"),
            (decode, "video_decode_us"),
            (queue, "video_decoder_queue_us"),
            (rendering, "rendering_us"),
            (vsync, "vsync_queue_us"),
        ):
            if row.get(key):
                series.add(timestamp_ms, row[key] / 1000)

    if not total.values:
        return None

    report.append("")
    report.append("## Frame pipeline (client frames)")
    if intervals.values:
        median_interval = statistics.median(intervals.values)
        spikes = sum(1 for value in intervals.values if value > median_interval * 1.5)
        report.append(
            f"- Frames: {len(total.values)}, median interval {median_interval:.2f} ms "
            f"({1000 / median_interval:.1f} fps), spikes >1.5x: {spikes} "
            f"({100 * spikes / len(intervals.values):.2f}%)"
        )
    report.append(
        f"- Total pipeline latency: p50 {fmt(total.p(50))} ms / p95 {fmt(total.p(95))} ms / "
        f"p99 {fmt(total.p(99))} ms"
    )
    for label, series in (
        ("decode", decode),
        ("decoder queue", queue),
        ("render", rendering),
        ("vsync queue", vsync),
    ):
        if series.values:
            report.append(
                f"- {label}: p50 {fmt(series.p(50))} ms / p99 {fmt(series.p(99))} ms"
            )

    return {"total": total, "intervals": intervals}


def analyze_server(directory: Path, report: list):
    encoder = Series()
    sizes = array("f")
    network = Series()
    game = Series()
    interarrival = Series()
    bitrate_buckets = {}
    first_timestamp = None
    last_timestamp = None

    for row in iter_rows(directory, "server"):
        row_type = row.get("type")
        timestamp_ms = row.get("unix_ms", 0)
        if first_timestamp is None:
            first_timestamp = timestamp_ms
        last_timestamp = timestamp_ms

        if row_type == "frame_encoded":
            encoder.add(timestamp_ms, row["encoder_latency_us"] / 1000)
            sizes.append(row.get("buffer_size", 0))
            if first_timestamp is not None:
                bucket = (timestamp_ms - first_timestamp) // 1000
                bitrate_buckets[bucket] = bitrate_buckets.get(bucket, 0) + row.get(
                    "buffer_size", 0
                )
        elif row_type == "client_stats":
            if row.get("network_latency_us") is not None:
                network.add(timestamp_ms, row["network_latency_us"] / 1000)
            if row.get("game_latency_us") is not None:
                game.add(timestamp_ms, row["game_latency_us"] / 1000)
        elif row_type == "tracking_received":
            if row.get("interarrival_us"):
                interarrival.add(timestamp_ms, row["interarrival_us"] / 1000)

    if not (encoder.values or network.values or interarrival.values):
        return None

    report.append("")
    report.append("## Server side")
    if encoder.values:
        report.append(
            f"- Encoder: p50 {fmt(encoder.p(50))} ms / p95 {fmt(encoder.p(95))} ms / "
            f"p99 {fmt(encoder.p(99))} ms over {len(encoder.values)} frames; "
            f"mean frame {statistics.mean(sizes) / 1000:.1f} kB"
        )
        slow = sum(1 for value in encoder.values if value > 10)
        report.append(f"- Encoder frames >10 ms: {slow} ({100 * slow / len(encoder.values):.2f}%)")
    if bitrate_buckets:
        rates = [bits * 8 / 1e6 for bits in bitrate_buckets.values()]
        report.append(
            f"- Encoded bitrate: p50 {fmt(percentile(rates, 50))} Mbps / "
            f"p95 {fmt(percentile(rates, 95))} Mbps / max {fmt(max(rates))} Mbps"
        )
    if network.values:
        report.append(
            f"- Network latency: p50 {fmt(network.p(50))} ms / p95 {fmt(network.p(95))} ms / "
            f"p99 {fmt(network.p(99))} ms"
        )
    if game.values:
        report.append(f"- Game render latency: p50 {fmt(game.p(50))} ms / p99 {fmt(game.p(99))} ms")
    if interarrival.values:
        median_interval = statistics.median(interarrival.values)
        jitter = [abs(value - median_interval) for value in interarrival.values]
        report.append(
            f"- Tracking transport: median interarrival {median_interval:.2f} ms, "
            f"jitter p95 {fmt(percentile(jitter, 95))} ms"
        )

    return {"encoder": encoder}


def find_session_dirs(paths):
    server_dir = None
    client_dir = None
    for path in paths:
        if server_dir is None and data_files(path, "server"):
            server_dir = path
        if client_dir is None and data_files(path, "input"):
            client_dir = path
    return server_dir, client_dir


def write_plots(report: list, hands, frames, server, out_dir: Path):
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        report.append("")
        report.append("_matplotlib not available, plots skipped_")
        return

    figure, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=False)

    if hands and hands[0].residual_position_time.plot_x:
        for hand, label, color in ((hands[0], "left", "tab:blue"), (hands[1], "right", "tab:orange")):
            if not hand.residual_position_time.plot_x:
                continue
            t0 = hand.residual_position_time.plot_x[0]
            axes[0].plot(
                [(t - t0) / 1000 for t in hand.residual_position_time.plot_x],
                list(hand.residual_position_time.plot_y),
                label=f"{label} hand",
                color=color,
                linewidth=0.6,
            )
        axes[0].set_ylabel("pose residual (mm)")
        axes[0].set_title("Tracking revision residual (wobble)")
        axes[0].legend()

    if frames and frames["total"].plot_x:
        series = frames["total"]
        t0 = series.plot_x[0]
        axes[1].plot(
            [(t - t0) / 1000 for t in series.plot_x],
            list(series.plot_y),
            linewidth=0.6,
        )
        axes[1].set_xlabel("session time (s)")
        axes[1].set_ylabel("total pipeline latency (ms)")
        axes[1].set_title("End-to-end latency")

    figure.tight_layout()
    figure.savefig(out_dir / "latency_analysis.png", dpi=120)
    plt.close(figure)

    report.append("")
    report.append(f"- `latency_analysis.png` written to `{out_dir}`")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--plots", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    server_dir, client_dir = find_session_dirs(args.paths)

    report = [f"# ALVR telemetry report"]
    report.append("")
    if server_dir:
        report.append(f"- Server session: `{server_dir}`")
    if client_dir:
        report.append(f"- Client session: `{client_dir}`")

    # Session settings from any header
    for source in (server_dir, client_dir):
        if source is None:
            continue
        for row in iter_rows(source, "server" if source == server_dir else "input"):
            if row.get("type") == "header":
                report.append("")
                report.append("## Session settings")
                for key, value in row.get("settings", {}).items():
                    report.append(f"- {key}: `{value}`")
                if row.get("role"):
                    report.append(f"- role: `{row['role']}`")
                break
        else:
            continue
        break

    hands = analyze_input(client_dir, report) if client_dir else None
    frames = analyze_frames(client_dir, report) if client_dir else None
    server = analyze_server(server_dir, report) if server_dir else None

    if args.plots:
        write_plots(report, hands, frames, server, args.paths[0])

    text = "\n".join(report) + "\n"
    if args.out:
        args.out.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
