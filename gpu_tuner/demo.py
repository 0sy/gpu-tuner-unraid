"""gpu-tuner demo mode: the real page, hub, node and daemon code over simulated GPUs.

   gpu-tuner serve --demo [--port 8766]     then: gpu-tuner open --demo

Three made-up machines (a two-card workstation, a one-card render box and a monitor-only GB10
edge box) with readings that move like real workloads and six hours of history from the moment
the page starts. Use it to look around, take screenshots or try the controls.

What it never touches: NVML (the `pynvml` module is blocked in every demo process), this
machine's gpu-tunerd and its socket, ssh, and the network. Each demo machine is a
`gpu-tuner demo-node` subprocess that runs the REAL node code over `DemoLib`, a pynvml-shaped
simulation, and, where the machine has a "daemon", the REAL gpu-tunerd logic in-process on a
socket in a private temporary directory. So every change on the page goes through the same
safety checks as on real hardware, and then lands on a simulated card.

Host labels, the hostname a node reports and the process names are all made up: nothing from
the machine running the demo appears on the page.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
import zlib
from http.server import ThreadingHTTPServer

from . import __version__, safety

DEMO_PORT = 8766                   # beside a real page on 8765, never on it
COOKIE = "gpu_tuner_demo"          # its own cookie: signing in here never signs you out of the real page
HISTORY_S = 6 * 3600
WARMUP_S = 900                     # temperatures settle before the first reading anyone sees
ALPHA = 1 - math.exp(-1 / 40)      # one-second step of a ~40 s thermal time constant
NOT_SUPPORTED = 3

# ── the made-up machines and cards ────────────────────────────────────────────────────
PRO_THRESHOLDS = {0: 98, 1: 95, 3: 92}         # shutdown, slowdown, GPU max (NVML constant ids)

CARDS = {
    "maxq": {"uuid": "GPU-de300000-0000-4000-8000-00000000a001",
             "name": "NVIDIA RTX PRO 6000 Blackwell Max-Q Workstation Edition",
             "pattern": "training", "idle_w": 22, "peak_w": 318, "power": (250, 325, 300),
             "limit_w": 300, "boost_mhz": 2000, "clock_cap_mhz": None, "fan_mode": "curve", "nfans": 1,
             "thresholds": PRO_THRESHOLDS, "ambient_c": 30, "c_per_w": 0.165, "vram_mib": 97887,
             "pci": "00000000:01:00.0", "vbios": "98.02.2F.00.01", "proc": "python3", "ecc": True},
    "ws": {"uuid": "GPU-de300000-0000-4000-8000-00000000a002",
           "name": "NVIDIA RTX PRO 6000 Blackwell Workstation Edition",
           "pattern": "inference", "idle_w": 28, "peak_w": 600, "power": (150, 600, 600),
           "limit_w": 450, "boost_mhz": 2600, "clock_cap_mhz": None, "fan_mode": "curve", "nfans": 2,
           "thresholds": PRO_THRESHOLDS, "ambient_c": 30, "c_per_w": 0.095, "vram_mib": 97887,
           "pci": "00000000:21:00.0", "vbios": "98.02.2F.00.02", "proc": "vllm", "ecc": True},
    "5090": {"uuid": "GPU-de300000-0000-4000-8000-00000000b001", "name": "NVIDIA GeForce RTX 5090",
             "pattern": "render", "idle_w": 24, "peak_w": 575, "power": (400, 600, 575),
             "limit_w": 500, "boost_mhz": 2850, "clock_cap_mhz": 2400, "fan_mode": "auto", "nfans": 2,
             "thresholds": {0: 97, 1: 92, 3: 90}, "ambient_c": 28, "c_per_w": 0.085, "vram_mib": 32607,
             "pci": "00000000:01:00.0", "vbios": "98.02.11.00.01", "proc": "blender", "ecc": False},
    "gb10": {"uuid": "GPU-de300000-0000-4000-8000-00000000c001", "name": "NVIDIA GB10",
             "pattern": "edge", "idle_w": 6, "peak_w": 72, "power": None, "limit_w": None,
             "boost_mhz": 2400, "clock_cap_mhz": None, "fan_mode": "auto", "nfans": 0,
             # what real GB10s report: T.Limit above slowdown and shutdown
             "thresholds": {0: 90, 1: 86, 3: 99}, "ambient_c": 35, "c_per_w": 0.42, "vram_mib": None,
             "pci": "0000000F:01:00.0", "vbios": "", "proc": "llama-server", "ecc": False},
}

PLAN = (
    {"id": "local", "label": "Workstation", "ssh": None, "port": None, "arch": "x86_64",
     "driver": "595.84", "cards": ("maxq", "ws"), "budget_w": 750,
     "wall": {"non_gpu_dc_w": 350, "psu_efficiency": 0.92, "circuits": {"15 A": 1440, "20 A": 1920}}},
    {"id": "render-box", "label": "Render box", "ssh": "render-box", "port": None, "arch": "x86_64",
     "driver": "595.84", "cards": ("5090",), "budget_w": 575, "wall": None},
    {"id": "edge-gb10", "label": "Edge GB10", "ssh": "edge-gb10", "port": None, "arch": "aarch64",
     "driver": "580.95.05", "cards": ("gb10",), "budget_w": None, "wall": None},   # no daemon: monitor-only
)
UNIFIED_TOTAL_MIB = 119 * 1024

# slot seconds, P(busy), busy level range, idle level range, jitter, burstiness
PATTERNS = {
    "training": (2700, 0.8, (0.92, 0.99), (0.0, 0.02), 0.03, 0.0),
    "inference": (600, 0.9, (0.30, 0.80), (0.02, 0.08), 0.08, 0.5),
    "render": (900, 0.6, (0.85, 1.00), (0.0, 0.03), 0.05, 0.0),
    "edge": (300, 0.85, (0.10, 0.55), (0.02, 0.05), 0.12, 0.6),
}


def plan_for(host_id):
    for p in PLAN:
        if p["id"] == host_id:
            return p
    raise KeyError(host_id)


def hub_hosts():
    """The demo machines as the hub sees hosts.json entries."""
    return [{k: p[k] for k in ("id", "label", "ssh", "port", "wall")} for p in PLAN]


# ── the simulation: deterministic in whole wall-clock seconds ────────────────────────────────
def _unit(salt, t):
    """A repeatable pseudo-random number in [0, 1) for (salt, t): same answer in every process."""
    x = (int(t) * 0x9E3779B1 + salt * 0x85EBCA77) & 0xFFFFFFFF
    x ^= x >> 15
    x = (x * 0x2C1B3C6D) & 0xFFFFFFFF
    x ^= x >> 12
    x = (x * 0x297A2D39) & 0xFFFFFFFF
    x ^= x >> 15
    return x / 2 ** 32


def workload(spec, salt, t):
    """(busy, load 0..1) of this card at second t."""
    slot_len, p_busy, busy_rng, idle_rng, jitter, burst = PATTERNS[spec["pattern"]]
    slot = t // slot_len
    # Keep the demo plausible while guaranteeing a few idle windows in each six-hour history.
    forced_idle = {
        "training": slot % 5 == 0,
        "inference": slot % 9 == 0,
        "render": slot % 7 == 0,
        "edge": slot % 8 == 0,
    }.get(spec["pattern"], False)
    busy = (not forced_idle) and (_unit(salt ^ 0xA5A5, slot) < p_busy)
    lo, hi = busy_rng if busy else idle_rng
    level = lo + (hi - lo) * _unit(salt ^ 0x5A5A, slot)
    if busy and burst:          # requests come and go within the slot
        level *= (1 - burst) + burst * (0.5 + 0.5 * math.sin(2 * math.pi * (t % 37) / 37 + slot))
    level += jitter * (2 * _unit(salt, t) - 1) * (1 if busy else 0.3)
    return busy, min(1.0, max(0.0, level))
