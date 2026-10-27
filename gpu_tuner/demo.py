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

# ── the made-up machines and cards ───────────────────────────────────────────────────────────
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
    busy = _unit(salt ^ 0xA5A5, slot) < p_busy
    lo, hi = busy_rng if busy else idle_rng
    level = lo + (hi - lo) * _unit(salt ^ 0x5A5A, slot)
    if busy and burst:          # requests come and go within the slot
        level *= (1 - burst) + burst * (0.5 + 0.5 * math.sin(2 * math.pi * (t % 37) / 37 + slot))
    level += jitter * (2 * _unit(salt, t) - 1) * (1 if busy else 0.3)
    return busy, min(1.0, max(0.0, level))


class Sim:
    """One simulated card, stepped one second at a time."""

    def __init__(self, spec, t0):
        self.spec = spec
        self.salt = zlib.crc32(spec["uuid"].encode())
        self.limit_w = spec["limit_w"]
        self.clock_cap = spec["clock_cap_mhz"]
        # "emulate": the card's daemon drives a curve (until it writes for real); "driver": the
        # driver's own curve; ("set", pct): a written speed.
        self.fan = "emulate" if spec["fan_mode"] == "curve" and spec["nfans"] else "driver"
        self.persistence = 0
        self.slowdown = spec["thresholds"][1]
        self.curve = safety.default_curve(safety.safety_floor_for(self.slowdown)[1])
        self.temp = spec["ambient_c"] + 5.0
        self.energy_mj = (self.salt % 400_000) * 1_000_000
        self.t = None
        self.r = None
        self.advance(t0 - WARMUP_S)
        self.advance(t0)

    def fan_pct(self):
        sp = self.spec
        if not sp["nfans"]:
            return None
        if isinstance(self.fan, tuple):
            return self.fan[1]
        if self.fan == "emulate":
            return safety.fan_target("curve", self.temp, self.curve, 60,
                                     slowdown_c=self.slowdown, fan_min=30)[0]
        return int(round(min(100.0, max(30.0, 30 + (self.temp - 35) * 1.4))))

    def advance(self, t):
        t = int(t)
        if self.t is None:
            self.t = t - 1
        start = max(self.t + 1, t - 3600)      # a suspended process doesn't replay hours
        for s in range(start, t + 1):
            self._step(s)
        self.t = max(self.t, t)

    def refresh(self):
        """Recompute the current second's reading after a write, without stepping time."""
        if self.t is not None:
            self._step(self.t, commit=False)

    def _step(self, s, commit=True):
        sp, salt = self.spec, self.salt
        busy, level = workload(sp, salt, s)
        util = int(round(level * 100))
        demand = sp["idle_w"] + (sp["peak_w"] - sp["idle_w"]) * level
        capped = self.limit_w is not None and demand > self.limit_w and util >= 5
        power = min(demand, self.limit_w) if self.limit_w is not None else demand
        power *= 1 + 0.015 * (2 * _unit(salt ^ 0x1234, s) - 1)
        if self.limit_w is not None:
            power = min(power, self.limit_w)          # the firmware holds a capped card at its cap
        fan = self.fan_pct()
        if commit:
            target = sp["ambient_c"] + sp["c_per_w"] * power - (0.05 * (fan - 50) if fan is not None else 0)
            self.temp += (target - self.temp) * ALPHA
        if util < 5:
            clock = 180 + 15 * int(14 * _unit(salt ^ 0x77, s))
        else:
            clock = sp["boost_mhz"] * ((self.limit_w / demand) ** 0.5 if capped else 1.0)
            clock *= 1 + 0.008 * (2 * _unit(salt ^ 0x99, s) - 1)
        clock_capped = self.clock_cap is not None and clock > self.clock_cap
        if clock_capped:
            clock = self.clock_cap
        clock = int(round(clock / 15) * 15)
        slot = s // PATTERNS[sp["pattern"]][0]
        if sp["vram_mib"] is None:
            vram = None
        elif sp["pattern"] == "inference":       # an inference engine keeps its model and cache resident
            vram = int(sp["vram_mib"] * (0.86 + 0.02 * _unit(salt ^ 0x31, slot)))
        elif busy:
            vram = int(sp["vram_mib"] * (0.62 + 0.25 * _unit(salt ^ 0x31, slot)))
        else:
            vram = 600
        if commit:
            self.energy_mj += int(power * 1000)
        mask = (0x1 if util < 5 else 0) | (0x4 if capped else 0) | (0x2 if clock_capped else 0)
        # a system-on-chip GPU's package runs a few degrees above its die, more under load
        soc = round(self.temp + 3 + 0.1 * power, 1) if sp["vram_mib"] is None else None
        self.r = {"util": util, "mem_util": int(level * 55) if util >= 5 else 0, "power_w": power,
                  "temp": int(round(self.temp)), "fan": fan, "clock": clock,
                  "mem_clock": 14001 if util >= 5 else 405, "pstate": 0 if util >= 5 else 8,
                  "mask": mask, "vram": vram, "busy": busy, "level": level, "soc": soc,
                  "pcie_gen": 5 if util >= 5 else 1}


# ── a pynvml-shaped module over the simulation ───────────────────────────────────────────────
class DemoNVMLError(Exception):
    def __init__(self, value=None):
        super().__init__(value)
        self.value = value

    def __str__(self):
        return {NOT_SUPPORTED: "Not Supported"}.get(self.value, str(self.value))


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _unsupported():
    raise DemoNVMLError(NOT_SUPPORTED)


class DemoLib:
    """Just enough of pynvml for Nvml(), the node and the daemon. Every read advances the
    simulation to the current second; every write changes the simulated card."""
    NVMLError = DemoNVMLError
    NVML_ERROR_NOT_SUPPORTED = NOT_SUPPORTED
    NVML_TEMPERATURE_GPU = 0
    NVML_CLOCK_GRAPHICS, NVML_CLOCK_MEM = 0, 2
    NVML_TEMPERATURE_THRESHOLD_SHUTDOWN, NVML_TEMPERATURE_THRESHOLD_SLOWDOWN = 0, 1
    NVML_TEMPERATURE_THRESHOLD_GPU_MAX = 3
    NVML_VOLATILE_ECC = 0
    NVML_MEMORY_ERROR_TYPE_CORRECTED, NVML_MEMORY_ERROR_TYPE_UNCORRECTED = 0, 1

    def __init__(self, plan, clock=time.time):
        self.plan, self.clock = plan, clock
        self.lock = threading.Lock()
        now = int(clock())
        self.specs = [CARDS[c] for c in plan["cards"]]
        self.sims = [Sim(spec, now) for spec in self.specs]

    def _r(self, h):
        with self.lock:
            sim = self.sims[h]
            sim.advance(self.clock())
            return sim, sim.r

    def _spec(self, h):
        return self.specs[h]

    # lifecycle
    def nvmlInit(self): pass
    def nvmlShutdown(self): pass
    def nvmlSystemGetDriverVersion(self): return self.plan["driver"]
    def nvmlDeviceGetCount(self): return len(self.specs)
    def nvmlDeviceGetHandleByIndex(self, i): return i

    # static facts
    def nvmlDeviceGetUUID(self, h): return self._spec(h)["uuid"]
    def nvmlDeviceGetName(self, h): return self._spec(h)["name"]
    def nvmlDeviceGetPciInfo(self, h): return _Obj(busId=self._spec(h)["pci"])

    def nvmlDeviceGetVbiosVersion(self, h):
        return self._spec(h)["vbios"] or _unsupported()

    def nvmlDeviceGetNumFans(self, h):
        return self._spec(h)["nfans"] or _unsupported()

    def nvmlDeviceGetMinMaxFanSpeed(self, h, lo, hi):
        if not self._spec(h)["nfans"]:
            _unsupported()
        lo._obj.value, hi._obj.value = 30, 100      # byref() arguments

    def nvmlDeviceGetPowerManagementLimitConstraints(self, h):
        p = self._spec(h)["power"]
        return [p[0] * 1000, p[1] * 1000] if p else _unsupported()

    def nvmlDeviceGetPowerManagementDefaultLimit(self, h):
        p = self._spec(h)["power"]
        return p[2] * 1000 if p else _unsupported()

    def nvmlDeviceGetPowerManagementLimit(self, h):
        with self.lock:
            limit = self.sims[h].limit_w
        return limit * 1000 if limit is not None else _unsupported()

    def nvmlDeviceGetTemperatureThreshold(self, h, which):
        return self._spec(h)["thresholds"][which]

    def nvmlDeviceGetSupportedMemoryClocks(self, h):
        return [14001, 405] if self._spec(h)["power"] else _unsupported()

    def nvmlDeviceGetSupportedGraphicsClocks(self, h, _mem):
        return list(range(180, 3091, 15))

    def nvmlDeviceGetMemoryInfo(self, h):
        sp = self._spec(h)
        if sp["vram_mib"] is None:
            _unsupported()                          # unified memory, as on GB10
        _sim, r = self._r(h)
        total = sp["vram_mib"] << 20
        used = r["vram"] << 20
        return _Obj(used=used, total=total, free=total - used)

    # readings
    def nvmlDeviceGetTemperature(self, h, _sensor): return self._r(h)[1]["temp"]
    def nvmlDeviceGetPowerUsage(self, h): return int(self._r(h)[1]["power_w"] * 1000)

    def nvmlDeviceGetUtilizationRates(self, h):
        r = self._r(h)[1]
        return _Obj(gpu=r["util"], memory=r["mem_util"])

    def nvmlDeviceGetClockInfo(self, h, which):
        r = self._r(h)[1]
        if which == self.NVML_CLOCK_MEM:
            return r["mem_clock"] if self._spec(h)["vram_mib"] is not None else _unsupported()
        return r["clock"]

    def nvmlDeviceGetFanSpeed_v2(self, h, _fan):
        r = self._r(h)[1]
        return r["fan"] if r["fan"] is not None else _unsupported()

    nvmlDeviceGetTargetFanSpeed = nvmlDeviceGetFanSpeed_v2

    def nvmlDeviceGetPerformanceState(self, h): return self._r(h)[1]["pstate"]
    def nvmlDeviceGetCurrentClocksEventReasons(self, h): return self._r(h)[1]["mask"]

    def nvmlDeviceGetCurrPcieLinkGeneration(self, h):
        return 1 if self._spec(h)["pattern"] == "edge" else self._r(h)[1]["pcie_gen"]

    def nvmlDeviceGetMaxPcieLinkGeneration(self, h):
        return 1 if self._spec(h)["pattern"] == "edge" else 5

    def nvmlDeviceGetCurrPcieLinkWidth(self, h):
        return 1 if self._spec(h)["pattern"] == "edge" else 16      # GB10 reports x1 of x16

    def nvmlDeviceGetMaxPcieLinkWidth(self, h): return 16

    def nvmlDeviceGetTotalEnergyConsumption(self, h):
        with self.lock:
            sim = self.sims[h]
            sim.advance(self.clock())
            return sim.energy_mj

    def nvmlDeviceGetPersistenceMode(self, h):
        if self._spec(h)["pattern"] == "edge":
            _unsupported()
        with self.lock:
            return self.sims[h].persistence

    def nvmlDeviceGetEncoderUtilization(self, h): return (0, 167000)
    def nvmlDeviceGetDecoderUtilization(self, h): return (0, 167000)

    def nvmlDeviceGetEccMode(self, h):
        return (1, 1) if self._spec(h)["ecc"] else _unsupported()

    def nvmlDeviceGetTotalEccErrors(self, h, _kind, _counter):
        return 0

    def nvmlDeviceGetComputeRunningProcesses(self, h):
        return []                       # the demo's process list comes from processes() below

    def processes(self, h):
        """Made-up process entries with their names: nothing here reads /proc."""
        sp = self._spec(h)
        sim, r = self._r(h)
        if sp["pattern"] == "edge":
            mib = 26 * 1024
        elif r["vram"] is None or r["vram"] <= 600:
            return []
        else:
            mib = r["vram"] - 450
        return [{"pid": 4000 + sim.salt % 5000, "name": sp["proc"], "vram_mib": mib}]

    def unified_memory_mib(self):
        """(used, total) MiB of the simulated system RAM a unified-memory GPU shares."""
        for h, sp in enumerate(self.specs):
            if sp["vram_mib"] is None:
                _sim, r = self._r(h)
                return int((38 + 30 * r["level"]) * 1024), UNIFIED_TOTAL_MIB
        return None, None

    def soc_temp_c(self):
        """The simulated SoC temperature of a unified-memory GPU, in place of /sys/class/thermal."""
        for h, sp in enumerate(self.specs):
            if sp["vram_mib"] is None:
                return self._r(h)[1]["soc"]
        return None

    # writes: only ever to the simulation
    def nvmlDeviceSetPersistenceMode(self, h, v):
        if self._spec(h)["pattern"] == "edge":
            _unsupported()
        with self.lock:
            self.sims[h].persistence = int(v)

    def nvmlDeviceSetPowerManagementLimit(self, h, mw):
        if not self._spec(h)["power"]:
            _unsupported()
        with self.lock:
            self.sims[h].limit_w = int(round(mw / 1000))
            self.sims[h].refresh()

    def nvmlDeviceSetFanSpeed_v2(self, h, _fan, pct):
        if not self._spec(h)["nfans"]:
            _unsupported()
        with self.lock:
            self.sims[h].fan = ("set", int(pct))
            self.sims[h].refresh()

    def nvmlDeviceSetDefaultFanSpeed_v2(self, h, _fan):
        if not self._spec(h)["nfans"]:
            _unsupported()
        with self.lock:
            self.sims[h].fan = "driver"
            self.sims[h].refresh()

    def nvmlDeviceSetGpuLockedClocks(self, h, _lo, hi):
        with self.lock:
            self.sims[h].clock_cap = int(hi)
            self.sims[h].refresh()

    def nvmlDeviceResetGpuLockedClocks(self, h):
        with self.lock:
            self.sims[h].clock_cap = None
            self.sims[h].refresh()


# ── history for the charts ───────────────────────────────────────────────────────────────────
def history_rows(host_id, now_s, seconds=HISTORY_S):
    """Hub history rows (wall time, uuid layout, flat values) for the `seconds` before now_s,
    from the same simulation the live demo node runs, so the charts join up."""
    specs = [CARDS[c] for c in plan_for(host_id)["cards"]]
    start = int(now_s) - seconds
    sims = [Sim(spec, start) for spec in specs]
    layout = tuple(spec["uuid"] for spec in specs)
    rows = []
    for t in range(start + 1, int(now_s)):
        vals = []
        for sim in sims:
            sim.advance(t)
            r = sim.r
            vals += [r["temp"], r["fan"], round(r["power_w"], 1), sim.limit_w, r["clock"], r["util"], r["soc"]]
        rows.append((float(t), layout, tuple(vals)))
    return rows


# ── the demo node (`gpu-tuner demo-node`) ────────────────────────────────────────────────────
def _block_nvml():
    sys.modules["pynvml"] = None          # any `import pynvml` in a demo process now fails loudly


def _demo_nvml(lib):
    from .nvml import Nvml

    class DemoNvml(Nvml):
        def system_memory_mib(self):
            return self.n.unified_memory_mib()

        def soc_temp_c(self):
            return self.n.soc_temp_c()

        def processes(self, g):
            return self.n.processes(g.handle)

    return DemoNvml(lib=lib, meminfo=os.devnull, thermal=os.devnull)


def _demo_collector(nv, sock, plan):
    from . import node, proto

    class DemoCollector(node.Collector):
        def hello(self):
            remote = bool(plan["ssh"])
            # a made-up hostname; a remote demo machine looks like one reached with a restricted key
            return {"type": "hello", "proto": proto.PROTO, "version": __version__,
                    "host": {"hostname": plan["id"], "arch": plan["arch"], "driver": self.nv.driver,
                             "pynvml": "demo"},
                    "forced_cmd": remote, "via_ssh": remote,
                    "static": [g.static() for g in self.nv.gpus]}

        def sample(self):
            gpus = {}
            for g in self.nv.gpus:
                s = self.nv.sample(g)
                s["procs"] = sorted(self.nv.processes(g), key=lambda p: -(p["vram_mib"] or 0))[:6]
                gpus[g.uuid] = s
            return gpus

    return DemoCollector(nv, sock)


def _run_daemon(lib, plan, sock, state_dir):
    """The real gpu-tunerd logic, in-process, over the simulated cards, on a private socket."""
    from . import daemon as dm
    state = os.path.join(state_dir, f"{plan['id']}.state.json")
    config = os.path.join(state_dir, f"{plan['id']}.config.json")
    if not os.path.exists(config):
        dm.save_config(config, {"allowed_uid": os.getuid(), "gpu_budget_w": plan["budget_w"], "interval_s": 2.0})
    if not os.path.exists(state):
        gpus = {}
        for c in plan["cards"]:
            spec = CARDS[c]
            gpus[spec["uuid"]] = {"power_w": spec["limit_w"], "clock_cap_mhz": spec["clock_cap_mhz"],
                                  "fan": {"mode": spec["fan_mode"], "manual_pct": 60}}
        with open(state, "w") as f:
            json.dump({"version": 1, "gpus": gpus}, f)
    d = dm.Daemon(_demo_nvml(lib), dm.load_config(config), state, config)
    d.load_state()
    d.apply_startup()
    d.save_state()
    srv, _ino = dm.open_socket(sock, os.getuid())

    def loop():
        nxt = 0.0
        while True:
            now = time.monotonic()
            if now >= nxt:
                d.tick(now)
                nxt = now + 2.0
            srv.settimeout(0.5)
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            with conn:
                try:
                    dm.serve_one(conn, d, os.getuid())
                except OSError:
                    pass
    threading.Thread(target=loop, daemon=True, name="demo-daemon").start()


def node_main(argv=None):
    _block_nvml()
    ap = argparse.ArgumentParser(prog="gpu-tuner demo-node",
                                 description="one simulated machine for `gpu-tuner serve --demo`")
    ap.add_argument("--host", required=True, choices=[p["id"] for p in PLAN])
    ap.add_argument("--state-dir", required=True, help="the demo page's private temporary directory")
    args = ap.parse_args(argv)
    from . import daemon as dm, node
    dm.log = lambda msg: print(msg, file=sys.stderr, flush=True)    # stdout is the protocol channel
    plan = plan_for(args.host)
    lib = DemoLib(plan)
    sock = os.path.join(args.state_dir, f"{plan['id']}.sock")
    if plan["budget_w"] is not None:
        _run_daemon(lib, plan, sock, args.state_dir)
    c = _demo_collector(_demo_nvml(lib), sock, plan)
    return node.Node(c, sys.stdin.buffer, sys.stdout.buffer).run()


def node_argv(host_id, state_dir):
    from . import hosts
    return [sys.executable, hosts.ENTRY, "demo-node", "--host", host_id, "--state-dir", state_dir]


# ── the demo page (`gpu-tuner serve --demo`) ─────────────────────────────────────────────────
def build_hub(state_dir, now_s=None):
    from . import hub
    h = hub.Hub(hub_hosts(), lambda m: hub.NodeLink(m, node_argv(m["id"], state_dir)))
    now_s = int(time.time()) if now_s is None else now_s
    for p in PLAN:
        h.seed_history(p["id"], history_rows(p["id"], now_s))
    return h


def serve(port):
    _block_nvml()
    from . import server
    # SIGTERM (systemctl stop, kill) unwinds like Ctrl-C, so the demo nodes stop and the
    # temporary directory goes too.
    signal.signal(signal.SIGTERM, lambda _sig, _frm: sys.exit(0))
    state_dir = tempfile.mkdtemp(prefix="gpu-tuner-demo-")       # 0700, removed on the way out
    try:
        h = build_hub(state_dir)
        h.start()
        token = secrets.token_urlsafe(32)                         # in memory only, never a file
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port),
                                        server.make_handler(h, token, port, cookie=COOKIE, demo=True))
        except OSError as e:
            h.stop()
            sys.exit(f"gpu-tuner: cannot listen on 127.0.0.1:{port}: {e}")
        httpd.daemon_threads = True
        print(f"gpu-tuner {__version__} DEMO on http://127.0.0.1:{port}/  three simulated machines; "
              f"nothing here reaches a real GPU\n"
              f"sign in with: gpu-tuner open --demo", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            h.stop()
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)
    return 0
