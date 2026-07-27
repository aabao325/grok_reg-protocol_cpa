#!/usr/bin/env python3
"""Self-hosted fingerprint browser backend.

Each browser gets:
- Fresh user_data_dir (cookie/storage isolation per account)
- Random UA / platform / screen / timezone / languages / hardware concurrency / WebGL
- CDP injection (Page.addScriptToEvaluateOnNewDocument) for navigator/screen/WebGL/canvas/audio overrides
- Emulation.setTimezoneOverride for native timezone
- turnstilePatch extension loaded

Release: browser.quit(del_data=True) + shutil.rmtree(user_data_dir)
"""

from __future__ import annotations

import json
import os
import random
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse


USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
        "Win32",
        "Windows",
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
        "Win32",
        "Windows",
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
        "MacIntel",
        "macOS",
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
        "Linux x86_64",
        "Linux",
    ),
]

SCREEN_RESOLUTIONS = [
    (1920, 1080), (1680, 1050), (1536, 864), (1440, 900),
    (2560, 1440), (1366, 768), (1280, 720),
]

TIMEZONES = [
    ("America/New_York", -5), ("America/Los_Angeles", -8),
    ("America/Chicago", -6), ("Europe/London", 0),
    ("Europe/Berlin", 1), ("Asia/Tokyo", 9),
    ("Asia/Singapore", 8), ("Australia/Sydney", 10),
]

WEBGL_PROFILES = [
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11-30.0.101.1191"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1060 Direct3D11 vs_5_0 ps_5_0, D3D11-30.0.14.7111"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11-30.0.15002.1001"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) Iris Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11-30.0.101.1661"),
]


@dataclass
class Fingerprint:
    user_agent: str = ""
    platform: str = "Win32"
    os_family: str = "Windows"
    languages: list = field(default_factory=lambda: ["en-US", "en"])
    timezone: str = "America/New_York"
    timezone_offset: int = -300
    screen_width: int = 1920
    screen_height: int = 1080
    color_depth: int = 24
    pixel_ratio: float = 1.0
    hardware_concurrency: int = 8
    device_memory: int = 8
    webgl_vendor: str = ""
    webgl_renderer: str = ""
    canvas_seed: int = 0
    audio_seed: int = 0
    user_data_dir: str = ""


def generate_fingerprint(config: dict | None = None) -> Fingerprint:
    config = config or {}
    ua_pool = config.get("fingerprint_user_agents") or []
    if ua_pool:
        entry = random.choice(ua_pool)
        if isinstance(entry, str):
            ua, platform, os_family = entry, "Win32", "Windows"
        else:
            ua = entry[0]
            platform = entry[1] if len(entry) > 1 else "Win32"
            os_family = entry[2] if len(entry) > 2 else "Windows"
    else:
        ua, platform, os_family = random.choice(USER_AGENTS)

    screen = random.choice(SCREEN_RESOLUTIONS)
    tz = random.choice(TIMEZONES)
    webgl = random.choice(WEBGL_PROFILES)

    return Fingerprint(
        user_agent=ua,
        platform=platform,
        os_family=os_family,
        languages=["en-US", "en"],
        timezone=tz[0],
        timezone_offset=tz[1] * 60,
        screen_width=screen[0],
        screen_height=screen[1],
        color_depth=24,
        pixel_ratio=random.choice([1.0, 1.0, 1.0, 1.25, 1.5]),
        hardware_concurrency=random.choice([4, 8, 8, 12, 16]),
        device_memory=random.choice([4, 8, 8, 16]),
        webgl_vendor=webgl[0],
        webgl_renderer=webgl[1],
        canvas_seed=random.randint(1, 2**31),
        audio_seed=random.randint(1, 2**31),
    )


_counter_lock = threading.Lock()
_counter = 0


def assign_user_data_dir(config: dict, worker_id: int | str = 0) -> str:
    """Allocate a fresh user_data_dir for one browser instance."""
    global _counter
    cfg = config or {}
    root = (cfg.get("fingerprint_profile_root") or "").strip()
    if not root:
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".fp_profiles")
    os.makedirs(root, exist_ok=True)
    with _counter_lock:
        global _counter
        _counter += 1
        ts = int(time.time() * 1000) % 10_000_000
        name = f"fp-{worker_id}-{ts}-{_counter}"
    path = os.path.join(root, name)
    os.makedirs(path, exist_ok=True)
    return path


def build_chromium_options(fp: Fingerprint, config: dict, extension_path: str | None = None):
    from DrissionPage import ChromiumOptions

    opts = ChromiumOptions()
    opts.auto_port()
    opts.set_timeouts(base=1)
    try:
        opts.set_user_agent(fp.user_agent)
    except Exception:
        opts.set_argument(f"--user-agent={fp.user_agent}")

    if fp.user_data_dir:
        try:
            opts.set_user_data_path(fp.user_data_dir)
        except Exception:
            opts.set_argument(f"--user-data-dir={fp.user_data_dir}")

    opts.set_argument(f"--accept-lang={','.join(fp.languages)}")

    for flag in (
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--mute-audio",
        "--no-first-run",
        "--disable-background-networking",
        "--disable-blink-features=AutomationControlled",
    ):
        try:
            opts.set_argument(flag)
        except Exception:
            pass

    if bool(config.get("register_headless", False)):
        try:
            opts.headless(True)
        except Exception:
            opts.set_argument("--headless=new")
        opts.set_argument("--window-size=1280,900")
    else:
        opts.set_argument(f"--window-size={fp.screen_width},{fp.screen_height}")

    if extension_path and os.path.exists(extension_path):
        try:
            opts.add_extension(extension_path)
        except Exception:
            pass

    proxy = (config.get("proxy") or "").strip()
    if proxy:
        try:
            u = urlparse(proxy if "://" in proxy else f"http://{proxy}")
            host = u.hostname or ""
            if host:
                port = u.port or 80
                scheme = u.scheme or "http"
                opts.set_argument(f"--proxy-server={scheme}://{host}:{port}")
        except Exception:
            pass

    # set_user_data_path() clears auto_port; re-enable it last so Chromium
    # launches a fresh browser process on a random port instead of trying to
    # connect to an existing one at the (empty) address.
    try:
        opts.auto_port()
    except Exception:
        pass

    return opts


_OVERRIDE_JS = r"""
(() => {
  const fp = __FP__;
  try {
    const props = {
      userAgent: fp.userAgent,
      platform: fp.platform,
      language: fp.languages[0],
      languages: Object.freeze(fp.languages.slice()),
      hardwareConcurrency: fp.hardwareConcurrency,
      deviceMemory: fp.deviceMemory,
      appVersion: fp.userAgent.replace('Mozilla/', ''),
    };
    for (const [k, v] of Object.entries(props)) {
      try {
        Object.defineProperty(Navigator.prototype, k, { get: () => v, configurable: true });
      } catch (e) {}
    }
  } catch (e) {}
  try {
    Object.defineProperty(screen, 'width', { get: () => fp.screenWidth, configurable: true });
    Object.defineProperty(screen, 'height', { get: () => fp.screenHeight, configurable: true });
    Object.defineProperty(screen, 'availWidth', { get: () => fp.screenWidth, configurable: true });
    Object.defineProperty(screen, 'availHeight', { get: () => fp.screenHeight - 40, configurable: true });
    Object.defineProperty(screen, 'colorDepth', { get: () => fp.colorDepth, configurable: true });
    Object.defineProperty(screen, 'pixelDepth', { get: () => fp.colorDepth, configurable: true });
    Object.defineProperty(window, 'devicePixelRatio', { get: () => fp.pixelRatio, configurable: true });
  } catch (e) {}
  try {
    const VENDOR = 0x9245, RENDERER = 0x9246;
    const patch = (proto) => {
      const orig = proto.getParameter;
      proto.getParameter = function(p) {
        if (p === VENDOR) return fp.webglVendor;
        if (p === RENDERER) return fp.webglRenderer;
        return orig.call(this, p);
      };
    };
    if (typeof WebGLRenderingContext !== 'undefined') patch(WebGLRenderingContext.prototype);
    if (typeof WebGL2RenderingContext !== 'undefined') patch(WebGL2RenderingContext.prototype);
  } catch (e) {}
  try {
    const seed = fp.canvasSeed;
    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(...args) {
      try {
        const ctx = this.getContext('2d');
        if (ctx && this.width > 0 && this.height > 0) {
          const img = ctx.getImageData(0, 0, this.width, this.height);
          for (let i = 0; i < img.data.length; i += 4) {
            const n = (Math.sin(seed + i * 0.123) * 1000) % 1;
            img.data[i] = (img.data[i] + (n > 0 ? 1 : -1)) & 0xff;
          }
          ctx.putImageData(img, 0, 0);
        }
      } catch (e) {}
      return origToDataURL.apply(this, args);
    };
  } catch (e) {}
  try {
    const seed = fp.audioSeed;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (Ctx) {
      const origCreate = Ctx.prototype.createAnalyser;
      Ctx.prototype.createAnalyser = function() {
        const a = origCreate.call(this);
        const origGet = a.getFloatFrequencyData.bind(a);
        a.getFloatFrequencyData = function(arr) {
          origGet(arr);
          for (let i = 0; i < arr.length; i++) {
            arr[i] += ((Math.sin(seed + i * 0.7) * 1000) % 1) * 0.0001;
          }
        };
        return a;
      };
    }
  } catch (e) {}
})();
""".strip()


def override_script_for(fp: Fingerprint) -> str:
    fp_dict = {
        "userAgent": fp.user_agent,
        "platform": fp.platform,
        "languages": list(fp.languages),
        "hardwareConcurrency": fp.hardware_concurrency,
        "deviceMemory": fp.device_memory,
        "screenWidth": fp.screen_width,
        "screenHeight": fp.screen_height,
        "colorDepth": fp.color_depth,
        "pixelRatio": fp.pixel_ratio,
        "webglVendor": fp.webgl_vendor,
        "webglRenderer": fp.webgl_renderer,
        "canvasSeed": fp.canvas_seed,
        "audioSeed": fp.audio_seed,
    }
    return f"const __FP__ = {json.dumps(fp_dict)};\n{_OVERRIDE_JS}"


def inject_fingerprint(tab, fp: Fingerprint) -> bool:
    """Install fingerprint override script for the tab's future navigations."""
    if tab is None or fp is None:
        return False
    script = override_script_for(fp)
    injected = False
    try:
        tab.run_cdp("Page.addScriptToEvaluateOnNewDocument", source=script)
        injected = True
    except Exception:
        try:
            if hasattr(tab, "run_cdp_loaded"):
                tab.run_cdp_loaded("Page.addScriptToEvaluateOnNewDocument", source=script)
                injected = True
        except Exception:
            injected = False
    try:
        tab.run_cdp("Emulation.setTimezoneOverride", timezoneId=fp.timezone)
    except Exception:
        pass
    return injected


def inject_if_present(tab, browser) -> bool:
    """TabPool hook: re-inject fingerprint overrides on every new/reassigned tab."""
    fp = getattr(browser, "_fingerprint", None)
    if fp is None:
        return False
    return inject_fingerprint(tab, fp)


def create_fingerprint_browser(
    config: dict,
    extension_path: str | None = None,
    log: Callable[[str], None] | None = None,
    worker_id: int | str = 0,
):
    from DrissionPage import Chromium

    fp = generate_fingerprint(config)
    fp.user_data_dir = assign_user_data_dir(config, worker_id=worker_id)
    opts = build_chromium_options(fp, config, extension_path=extension_path)
    browser = Chromium(opts)
    try:
        browser._fingerprint = fp  # type: ignore[attr-defined]
    except Exception:
        pass
    if log:
        log(
            f"[fingerprint] ua={fp.user_agent[:48]} tz={fp.timezone} "
            f"screen={fp.screen_width}x{fp.screen_height} hw={fp.hardware_concurrency} "
            f"dir={os.path.basename(fp.user_data_dir)}"
        )
    # Inject on initial tab(s)
    try:
        for tid in (browser.tab_ids or []):
            try:
                inject_fingerprint(browser.get_tab(tid), fp)
            except Exception:
                pass
    except Exception as exc:
        if log:
            log(f"[fingerprint] initial inject failed: {exc}")
    return browser


def release_fingerprint_browser(browser, *, delete_profile: bool | None = None, log=None) -> None:
    fp = getattr(browser, "_fingerprint", None)
    user_data_dir = getattr(fp, "user_data_dir", "") if fp else ""
    try:
        quit_fn = getattr(browser, "quit", None)
        if callable(quit_fn):
            try:
                quit_fn(del_data=True)
            except TypeError:
                try:
                    quit_fn()
                except Exception:
                    pass
            except Exception:
                pass
    except Exception:
        pass
    if user_data_dir:
        do_delete = True if delete_profile is None else bool(delete_profile)
        if do_delete:
            try:
                shutil.rmtree(user_data_dir, ignore_errors=True)
                if log:
                    log(f"[fingerprint] removed profile {os.path.basename(user_data_dir)}")
            except Exception:
                pass
