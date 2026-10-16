#!/usr/bin/env python3
"""Measure rendering CPU work without GPIO, serial devices or service changes.

Example (baseline directory contains the original emotions.py/matrix_renderer.py):
    python -B scripts/benchmark_rendering.py --baseline /path/to/original/pi \
        --output benchmarks/rendering.json

The real render loop uses deterministic audio, a virtual 30 Hz clock and a dummy
matrix. Timings exclude startup, frame pacing, GPIO transfer and VSync. They are
not physical display FPS measurements. Settings come from source defaults.
"""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import random
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch
import warnings

import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
PI_DIR = ROOT / "pi"
SEED = 314159


def load_environment(source):
    """Load real defaults and rendering modules without import-time side effects."""
    modules = {}
    replacements = {"rgbmatrix": SimpleNamespace(RGBMatrix=None, RGBMatrixOptions=None)}
    with patch.dict(sys.modules, replacements), patch("os.makedirs"), \
            patch("os.path.exists", return_value=False), patch("threading.Thread.start"):
        for name in ("config", "state", "emotions", "boot_animation", "animations",
                     "postfx", "dino_game", "matrix_renderer"):
            path = (source if name in ("emotions", "matrix_renderer") else PI_DIR) / f"{name}.py"
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            modules[name] = module
    modules["initial_state"] = copy.deepcopy(modules["state"].state)
    modules["source"] = str(source)
    modules["hashes"] = {
        name: hashlib.sha256((source / name).read_bytes()).hexdigest()
        for name in ("emotions.py", "matrix_renderer.py")
    }
    return modules


class VirtualClock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1_700_000_000 + self.now

    def sleep(self, seconds):
        pass


class LoopProbe:
    """Act as matrix, canvas and stop flag while timing the real loop body."""
    def __init__(self, environment, clock, frames, warmup, verify):
        self.env = environment
        self.clock = clock
        self.limit = frames + warmup
        self.warmup = warmup
        self.verify = verify
        self.count = 0
        self.samples = []
        self.digest = hashlib.sha256()
        self.brightness = environment["config"].BRIGHTNESS
        self.image = None
        self.cleared = False
        band = np.arange(environment["config"].BANDS, dtype=np.float32)
        self.audio = np.asarray([
            (0.16 + 0.12 * np.sin(band * 0.7 + frame * 0.11))
            * (1.8 if frame % 24 < 2 else 1.0)
            for frame in range(self.limit)
        ], dtype=np.float32)

    def is_set(self):
        if self.count >= self.limit:
            return True
        shared = self.env["state"]
        shared.target_bars[:] = self.audio[self.count]
        shared.state["emotion"] = ("angry", "happy", "sad", "surprised")[(self.count // 90) % 4]
        self.started_ns = time.perf_counter_ns()
        return False

    def CreateFrameCanvas(self):
        return self

    def SetImage(self, image):
        self.image = image

    def SwapOnVSync(self, canvas):
        elapsed_us = (time.perf_counter_ns() - self.started_ns) / 1000
        if self.count >= self.warmup:
            self.samples.append(elapsed_us)
        # Pixel checks are performed outside the measured interval.
        if self.verify:
            if self.image.size != (128, 32) or self.image.mode != "RGB":
                raise AssertionError("Unexpected output frame format")
            self.digest.update(self.image.tobytes())
        self.count += 1
        self.clock.now = 1000.0 + self.count / 30
        return canvas

    def Clear(self):
        self.cleared = True


SCENARIOS = {
    "solid": {"effect": "solid"},
    "gradient": {"effect": "gradient", "gradient_angle": 45},
    "rainbow": {"effect": "rainbow"},
    "party_rainbow": {"effect": "rainbow", "party": True},
    "text_rainbow_x3": {"mode": "text", "text_content": "HELLO PROTOGEN!",
                        "settings": {"text_scale": 3, "text_effect": "rainbow"}},
}


def run_loop(env, scenario, frames, warmup=0, verify=False):
    renderer = env["matrix_renderer"]
    shared = env["state"]
    shared.state.clear()
    shared.state.update(copy.deepcopy(env["initial_state"]))
    shared.state["connected"] = True
    for key, value in SCENARIOS[scenario].items():
        if key == "settings":
            shared.state[key].update(value)
        else:
            shared.state[key] = value
    random.seed(SEED)
    clock = VirtualClock()
    probe = LoopProbe(env, clock, frames, warmup, verify)
    # No hardware thread or boot animation is started in benchmark mode.
    renderer.threading = SimpleNamespace(
        Thread=lambda **kwargs: SimpleNamespace(start=lambda: None))
    renderer.time = clock
    env["animations"].time = clock
    renderer.stop_event = probe
    renderer._build_matrix = lambda: probe
    serial_stub = SimpleNamespace(send_command=lambda command: None)
    with patch.dict(sys.modules, {"state": shared, "serial_proto": serial_stub}), \
            patch.object(env["boot_animation"], "BootSequence",
                         lambda: SimpleNamespace(done=True)):
        renderer._render_loop()
    if shared.state["errors"]:
        raise RuntimeError(f"Renderer reported errors: {shared.state['errors']}")
    if not probe.cleared or probe.count != frames + warmup:
        raise AssertionError("Render loop did not finish normally")
    return probe.samples, probe.digest.hexdigest()


def micro_cases(env):
    renderer = env["matrix_renderer"]
    emotions = env["emotions"]
    diag = np.indices((32, 64)).sum(axis=0).astype(np.int32)
    colors = [[255, 0, 0], [0, 0, 255]]

    def rainbow():
        if hasattr(renderer, "_make_rainbow_map"):
            return renderer._make_rainbow_map(diag, 37.25)
        hue = ((diag * 16 - 37.25) % 256).astype(np.uint8)
        hsv = np.stack([hue, np.full_like(hue, 255), np.full_like(hue, 255)], axis=-1)
        return np.asarray(Image.fromarray(hsv, mode="HSV").convert("RGB"),
                          dtype=np.float32)

    return {
        "animated_face": lambda: emotions.get_face_alpha_animated(
            "angry", "happy", 0.3, 0.4, 0.2, 1.0, 0.5),
        "static_face": lambda: emotions.get_face_alpha("angry", 0.4),
        "gradient_unchanged": lambda: renderer._make_gradient_map(colors, 45),
        "rainbow_fill": rainbow,
        "rainbow_text_x1": lambda: renderer._render_text(
            "HELLO PROTOGEN!", 128, (255, 0, 0), 128, 1, "rainbow", 1.234),
        "rainbow_text_x3": lambda: renderer._render_text(
            "HELLO PROTOGEN!", 128, (255, 0, 0), 128, 3, "rainbow", 1.234),
    }


def batch_time(function, iterations):
    started = time.perf_counter_ns()
    for _ in range(iterations):
        function()
    return (time.perf_counter_ns() - started) / iterations / 1000


def describe(samples):
    return {
        "samples": len(samples),
        "median_us": float(np.median(samples)),
        "p95_us": float(np.percentile(samples, 95)),
        "mean_us": float(np.mean(samples)),
        "min_us": float(np.min(samples)),
        "max_us": float(np.max(samples)),
    }


def machine_info():
    processor = platform.processor()
    model = None
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                processor = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        except OSError:
            pass
    if sys.platform.startswith("linux"):
        cpuinfo = Path("/proc/cpuinfo")
        if cpuinfo.exists():
            for line in cpuinfo.read_text().splitlines():
                if line.startswith("model name"):
                    processor = line.partition(":")[2].strip()
                elif line.startswith("Model"):
                    model = line.partition(":")[2].strip()
    return {"platform": platform.platform(), "processor": processor, "model": model,
            "logical_cpus": os.cpu_count(), "python": platform.python_version(),
            "numpy": np.__version__, "pillow": Image.__version__}


def markdown_report(report):
    lines = ["# Rendering benchmark", "", f"UTC: {report['timestamp_utc']}", "",
             f"Environment: `{report['machine']}`", "", report["scope"], "",
             f"Parameters: `{report['parameters']}`", "",
             "## Actual render loop (CPU work only)", "",
             "| Scenario | Before median, ms | After median, ms | After p95, ms | Speedup |",
             "|---|---:|---:|---:|---:|"]
    for name, values in report["loops"].items():
        after = values["after"]
        before = values.get("before")
        before_text = f"{before['median_us'] / 1000:.3f}" if before else "—"
        speedup = f"{before['median_us'] / after['median_us']:.2f}x" if before else "—"
        lines.append(f"| {name} | {before_text} | {after['median_us'] / 1000:.3f} | "
                     f"{after['p95_us'] / 1000:.3f} | {speedup} |")
    lines += ["", "## Individual operations", "",
              "Values are medians of batch averages; unchanged gradients use a warm cache.", "",
              "| Operation | Before, us | After, us | Speedup |", "|---|---:|---:|---:|"]
    for name, values in report["micro"].items():
        after = values["after"]["median_us"]
        before = values.get("before", {}).get("median_us")
        before_text = f"{before:.2f}" if before else "—"
        speedup = f"{before / after:.2f}x" if before else "—"
        lines.append(f"| {name} | {before_text} | {after:.2f} | {speedup} |")
    lines += ["", "## Validation", "",
              "| Scenario | Frames per version | Baseline pixel equality |",
              "|---|---:|---|"]
    for name, values in report["validation"].items():
        checked = "Pass" if "before" in values["sha256"] else "Not checked (no baseline)"
        lines.append(f"| {name} | {values['frames_per_version']} | {checked} |")
    lines += ["",
              "Loop p95 is calculated across individual frames. Old/new run order alternates "
              "between repeats. Random seeds, audio and virtual clock match between versions. "
              "The 128x32 output uses two mirrored 64x32 panels. GPIO copy, VSync, frame pacing, "
              "boot and concurrent web/serial activity are excluded. Physical refresh rate "
              "and overall Raspberry Pi CPU usage cannot be inferred from these values.", ""]
    return "\n".join(lines)


def main():
    warnings.filterwarnings("ignore", category=DeprecationWarning,
                            message=".*'mode' parameter.*")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--frames", type=int, default=600)
    parser.add_argument("--warmup", type=int, default=120)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--iterations", type=int, default=1000)
    args = parser.parse_args()
    if min(args.frames, args.repeats, args.iterations) < 1 or args.warmup < 0:
        parser.error("frames, repeats and iterations must be positive; warmup must be nonnegative")
    environments = {"after": load_environment(PI_DIR)}
    if args.baseline:
        environments["before"] = load_environment(args.baseline.resolve())
    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "machine": machine_info(),
        "scope": "Real render loop with synthetic input and dummy matrix; no GPIO/VSync or sleep. "
                 "This measures CPU-side frame construction, not hardware display FPS.",
        "parameters": {"frames_per_repeat": args.frames, "warmup_frames": args.warmup,
                       "repeats": args.repeats, "micro_iterations_per_repeat": args.iterations,
                       "seed": SEED, "virtual_fps": 30},
        "sources": {label: {"path": env["source"], "sha256": env["hashes"]}
                    for label, env in environments.items()},
        "validation": {}, "loops": {}, "micro": {},
    }
    cases = {label: micro_cases(env) for label, env in environments.items()}
    for name in SCENARIOS:
        hashes = {label: run_loop(env, name, 180, verify=True)[1]
                  for label, env in environments.items()}
        if len(set(hashes.values())) != 1:
            raise AssertionError(f"Frame pixels differ in {name}: {hashes}")
        report["validation"][name] = {"frames_per_version": 180, "sha256": hashes}
    print("Validation: all 180-frame sequences " + (
        "match the baseline." if args.baseline else "render successfully (no baseline)."), flush=True)
    for name in SCENARIOS:
        samples = {label: [] for label in environments}
        repeat_means = {label: [] for label in environments}
        for repeat_index in range(args.repeats):
            labels = list(environments)[::1 if repeat_index % 2 == 0 else -1]
            for label in labels:
                batch, _ = run_loop(environments[label], name, args.frames, args.warmup)
                samples[label].extend(batch)
                repeat_means[label].append(float(np.mean(batch)))
        report["loops"][name] = {
            label: {**describe(values), "repeat_mean_us": repeat_means[label],
                    "frames_above_33_33_ms": sum(value > 1e6 / 30 for value in values)}
            for label, values in samples.items()
        }
        print(f"Loop {name}: " + ", ".join(
            f"{label} {np.median(values) / 1000:.3f} ms" for label, values in samples.items()), flush=True)
    for name in cases["after"]:
        if "before" in cases:
            np.testing.assert_array_equal(cases["before"][name](), cases["after"][name]())
        samples = {label: [] for label in environments}
        for label in samples:
            for _ in range(100):
                cases[label][name]()
        for repeat_index in range(args.repeats):
            labels = list(environments)[::1 if repeat_index % 2 == 0 else -1]
            for label in labels:
                samples[label].append(batch_time(cases[label][name], args.iterations))
        report["micro"][name] = {label: {**describe(values), "batch_mean_us": values}
                                 for label, values in samples.items()}
        print(f"Operation {name}: " + ", ".join(
            f"{label} {np.median(values):.2f} us" for label, values in samples.items()), flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        args.output.with_suffix(".md").write_text(markdown_report(report), encoding="utf-8")
        print(f"Reports saved: {args.output} and {args.output.with_suffix('.md')}")
    else:
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
