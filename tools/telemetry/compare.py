#!/usr/bin/env python3
"""Compare ALVR telemetry across multiple experiment runs.

Usage:
    python3 compare.py --run LABEL SERVER_DIR CLIENT_DIR [--run LABEL SERVER_DIR CLIENT_DIR ...]

SERVER_DIR is an ALVR server telemetry session directory (server*.jsonl).
CLIENT_DIR is an ALVR client (Quest) telemetry session directory (frames*.jsonl).
Pass "-" for either if the data is not available for a run.

Prints one markdown table so A/B experiment verdicts are objective.
"""

import argparse
import math
import statistics
import sys
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from analyze import iter_rows, percentile  # noqa: E402


def collect_client(directory):
    total = array("f")
    decode = array("f")
    queue = array("f")
    vsync = array("f")
    intervals = array("f")

    for row in iter_rows(directory, "frames"):
        if row.get("type") != "frame":
            continue
        if row.get("total_pipeline_latency_us"):
            total.append(row["total_pipeline_latency_us"] / 1000)
        if row.get("video_decode_us"):
            decode.append(row["video_decode_us"] / 1000)
        if row.get("video_decoder_queue_us"):
            queue.append(row["video_decoder_queue_us"] / 1000)
        if row.get("vsync_queue_us"):
            vsync.append(row["vsync_queue_us"] / 1000)
        if row.get("frame_interval_us"):
            intervals.append(row["frame_interval_us"] / 1000)

    spikes = float("nan")
    fps = float("nan")
    if intervals:
        median_interval = statistics.median(intervals)
        fps = 1000 / median_interval
        spikes = 100 * sum(1 for value in intervals if value > median_interval * 1.5) / len(
            intervals
        )

    return {
        "frames": len(total),
        "fps": fps,
        "spikes": spikes,
        "total": total,
        "decode": decode,
        "queue": queue,
        "vsync": vsync,
    }


def collect_server(directory):
    encoder = array("f")
    network = array("f")

    for row in iter_rows(directory, "server"):
        row_type = row.get("type")
        if row_type == "frame_encoded" and row.get("encoder_latency_us"):
            encoder.append(row["encoder_latency_us"] / 1000)
        elif row_type == "client_stats" and row.get("network_latency_us") is not None:
            network.append(row["network_latency_us"] / 1000)

    return {"encoder": encoder, "network": network}


def metric(values, p, digits=1):
    if not values:
        return "-"
    value = percentile(values, p)
    if math.isnan(value):
        return "-"
    return f"{value:.{digits}f}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run",
        nargs=3,
        action="append",
        metavar=("LABEL", "SERVER_DIR", "CLIENT_DIR"),
        required=True,
    )
    args = parser.parse_args()

    columns = [
        "p50 total",
        "p95 total",
        "p99 total",
        "spikes %",
        "decode p50",
        "decode p99",
        "queue p50",
        "vsync p50",
        "encode p50",
        "net p50",
        "fps",
        "frames",
    ]
    print("| run | " + " | ".join(columns) + " |")
    print("|" + "---|" * (len(columns) + 1))

    for label, server_path, client_path in args.run:
        client = (
            collect_client(Path(client_path))
            if client_path != "-"
            else {
                "frames": 0,
                "fps": float("nan"),
                "spikes": float("nan"),
                "total": [],
                "decode": [],
                "queue": [],
                "vsync": [],
            }
        )
        server = (
            collect_server(Path(server_path))
            if server_path != "-"
            else {"encoder": [], "network": []}
        )

        row = [
            label,
            metric(client["total"], 50),
            metric(client["total"], 95),
            metric(client["total"], 99),
            f"{client['spikes']:.2f}" if client["frames"] else "-",
            metric(client["decode"], 50),
            metric(client["decode"], 99),
            metric(client["queue"], 50),
            metric(client["vsync"], 50),
            metric(server["encoder"], 50),
            metric(server["network"], 50),
            f"{client['fps']:.0f}" if client["frames"] else "-",
            str(client["frames"]) if client["frames"] else "-",
        ]
        print("| " + " | ".join(row) + " |")


if __name__ == "__main__":
    main()
