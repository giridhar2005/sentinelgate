#!/usr/bin/env python3
"""Automated Playwright Walkthrough & Demo Recorder for SentinelGate.

Generates:
1. docs/dashboard-preview.png (High-resolution obsidian dashboard preview)
2. docs/demo.gif (Optimized < 4MB motion demo capturing agent firewall, cache hit, failover, and ROI estimator)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = ROOT / "docs"
FRAMES_DIR = ROOT / "tmp_frames"
PORT = 8000
BASE_URL = f"http://127.0.0.1:{PORT}"


def wait_for_server(timeout: float = 12.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = httpx.get(f"{BASE_URL}/health", timeout=0.5)
            if r.status_code == 200:
                return True
        except Exception:
            time.sleep(0.3)
    return False


def seed_telemetry():
    """Seed initial realistic requests so dashboard metrics and trace table look rich."""
    client = httpx.Client(base_url=BASE_URL, timeout=5.0)

    # 1. Normal prompt (MISS)
    client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [
                {
                    "role": "user",
                    "content": "What is connection pooling in HTTP client architectures?",
                }
            ],
        },
        headers={"X-No-Cache": "true"},
    )

    # 2. Paraphrased prompt (HIT)
    client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "user", "content": "Can you explain HTTP client connection pools simply?"}
            ],
        },
    )

    # 3. Secret Redaction
    client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [
                {
                    "role": "user",
                    "content": "Verify connection string postgresql://admin:secretPass123@db.prod.internal:5432/core and key sk-proj-998877665544332211",
                }
            ],
        },
    )

    # 4. Auto-route simple prompt
    client.post(
        "/v1/chat/completions",
        json={
            "model": "auto",
            "messages": [
                {
                    "role": "user",
                    "content": "Classify this feedback as positive, neutral, or negative: 'Great product!'",
                }
            ],
        },
    )


def record_walkthrough():
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    if FRAMES_DIR.exists():
        shutil.rmtree(FRAMES_DIR)
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)

    print("[1/5] Starting SentinelGate server in zero-key sandbox demo mode...")
    env = os.environ.copy()
    env["DEMO_MODE"] = "true"
    env["PYTHONPATH"] = str(ROOT)

    server_proc = subprocess.Popen(
        [
            str(ROOT / ".venv" / "bin" / "python"),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
            "--log-level",
            "warning",
        ],
        cwd=str(ROOT),
        env=env,
    )

    frame_idx = 0

    def capture_frame(page, count: int = 1, delay: float = 0.25):
        nonlocal frame_idx
        for _ in range(count):
            frame_path = FRAMES_DIR / f"frame_{frame_idx:04d}.png"
            page.screenshot(path=str(frame_path))
            frame_idx += 1
            if delay > 0:
                time.sleep(delay)

    try:
        if not wait_for_server():
            raise RuntimeError("Server failed to respond to /health on port 8000 within timeout.")

        print("[2/5] Seeding telemetry baseline...")
        seed_telemetry()
        time.sleep(1.0)

        print("[3/5] Automating Playwright browser walkthrough (1280x800)...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 800}, device_scale_factor=1)

            # Step A: Load Dashboard
            page.goto(f"{BASE_URL}/dashboard")
            page.wait_for_selector("#stat-total-requests")
            page.wait_for_timeout(1000)

            # High-res static preview
            preview_path = DOCS_DIR / "dashboard-preview.png"
            page.screenshot(path=str(preview_path))
            print(f"  ✓ Saved static dashboard preview: {preview_path}")

            # Hold initial state for 4 frames
            capture_frame(page, count=4, delay=0.2)

            # Step B: Agent Tool Attack Preset
            print("  -> Triggering Agent Tool Attack preset...")
            page.evaluate("runTestPreset('F')")
            page.wait_for_timeout(600)
            capture_frame(page, count=6, delay=0.25)

            # Step C: Paraphrased Prompt Preset (Cache Hit < 2ms)
            print("  -> Triggering Paraphrased Prompt (Cache Hit)...")
            page.evaluate("runTestPreset('B')")
            page.wait_for_timeout(600)
            capture_frame(page, count=6, delay=0.25)

            # Step D: Chaos Fault Outage Toggle (Simulate 429 Failover)
            print("  -> Toggling Outage Failover...")
            page.evaluate("toggleChaosFault(true)")
            page.wait_for_timeout(300)
            page.evaluate("runTestPreset('A')")
            page.wait_for_timeout(700)
            capture_frame(page, count=6, delay=0.25)
            page.evaluate("toggleChaosFault(false)")

            # Step E: Enterprise ROI Estimator Tab
            print("  -> Navigating to Enterprise ROI Estimator Tab...")
            page.evaluate("switchTab('roi')")
            page.wait_for_timeout(400)
            capture_frame(page, count=4, delay=0.2)

            # Move Sliders dynamically
            page.evaluate("setRoiPreset(100000, 2500)")
            page.wait_for_timeout(400)
            capture_frame(page, count=4, delay=0.2)

            page.evaluate("setRoiPreset(500000, 3500)")
            page.wait_for_timeout(500)
            capture_frame(page, count=8, delay=0.25)

            # Step F: Back to Request Traces & Trace Inspector Replay
            print("  -> Opening Request Trace Inspector & Shadow Replay...")
            page.evaluate("switchTab('traces')")
            page.wait_for_timeout(500)
            page.evaluate("if (cachedTraces.length > 0) openTraceDrawer(0);")
            page.wait_for_timeout(400)
            page.evaluate("executeShadowReplay()")
            page.wait_for_timeout(600)
            capture_frame(page, count=6, delay=0.25)

            browser.close()

        print(f"[4/5] Captured {frame_idx} frames. Compiling high-definition GIF (< 4MB)...")
        gif_path = DOCS_DIR / "demo.gif"
        ffmpeg_bin = shutil.which("ffmpeg") or "/Users/giridhar/.local/bin/ffmpeg"

        # 2-pass palette generation for sharp obsidian UI lines and crisp monospace text
        cmd = [
            ffmpeg_bin,
            "-y",
            "-framerate",
            "4",
            "-i",
            str(FRAMES_DIR / "frame_%04d.png"),
            "-filter_complex",
            "[0:v] fps=4,scale=1100:-1:flags=lanczos,split [a][b];[a] palettegen=max_colors=128:stats_mode=diff [p];[b][p] paletteuse=dither=bayer:bayer_scale=3",
            str(gif_path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"FFmpeg error: {res.stderr}")
            # Fallback PIL
            from PIL import Image

            frames = [Image.open(f) for f in sorted(FRAMES_DIR.glob("frame_*.png"))]
            frames[0].save(
                gif_path,
                save_all=True,
                append_images=frames[1:],
                optimize=True,
                duration=250,
                loop=0,
            )

        size_bytes = gif_path.stat().st_size
        size_mb = size_bytes / (1024 * 1024)
        print(f"[5/5] ✓ Generated {gif_path} ({size_mb:.2f} MB)")
        if size_mb > 4.0:
            print("  Warning: GIF exceeds 4MB, re-optimizing...")
            cmd_reopt = [
                ffmpeg_bin,
                "-y",
                "-framerate",
                "4",
                "-i",
                str(FRAMES_DIR / "frame_%04d.png"),
                "-filter_complex",
                "[0:v] fps=4,scale=960:-1:flags=lanczos,split [a][b];[a] palettegen=max_colors=96:stats_mode=diff [p];[b][p] paletteuse=dither=bayer:bayer_scale=3",
                str(gif_path),
            ]
            subprocess.run(cmd_reopt, capture_output=True)
            print(f"  ✓ Re-optimized size: {gif_path.stat().st_size / (1024 * 1024):.2f} MB")

    finally:
        server_proc.terminate()
        server_proc.wait(timeout=3.0)
        if FRAMES_DIR.exists():
            shutil.rmtree(FRAMES_DIR)
        print("Cleanup complete.")


if __name__ == "__main__":
    record_walkthrough()
