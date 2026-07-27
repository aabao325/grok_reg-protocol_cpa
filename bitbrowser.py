#!/usr/bin/env python3
"""BitBrowser Local API client + DrissionPage attach helpers.

Default Local API: http://127.0.0.1:54345
Docs: https://doc.bitbrowser.cn/api-jie-kou-wen-dang/liu-lan-qi-jie-kou.md
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse


DEFAULT_API = "http://127.0.0.1:54345"


class BitBrowserError(RuntimeError):
    pass


class BitBrowserClient:
    def __init__(self, api_base: str = DEFAULT_API, timeout: float = 60.0):
        self.api_base = (api_base or DEFAULT_API).rstrip("/")
        self.timeout = float(timeout or 60.0)

    def post(self, path: str, body: dict | None = None) -> dict:
        url = f"{self.api_base}{path if path.startswith('/') else '/' + path}"
        data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            raise BitBrowserError(
                f"BitBrowser API 不可达 {url}: {exc}. 请确认比特浏览器已启动且 Local API 端口正确"
            ) from exc
        except Exception as exc:
            raise BitBrowserError(f"BitBrowser API 请求失败 {url}: {exc}") from exc

        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise BitBrowserError(f"BitBrowser API 返回非 JSON: {raw[:200]}") from exc

        if not isinstance(payload, dict):
            raise BitBrowserError(f"BitBrowser API 返回异常: {payload!r}")
        if not payload.get("success", False):
            raise BitBrowserError(payload.get("msg") or payload.get("message") or str(payload))
        return payload

    def health(self) -> bool:
        try:
            self.post("/health", {})
            return True
        except Exception:
            return False

    def list_browsers(
        self,
        page: int = 0,
        page_size: int = 100,
        name: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {"page": int(page), "pageSize": min(int(page_size), 100)}
        if name:
            body["name"] = name
        data = self.post("/browser/list", body).get("data") or {}
        return data if isinstance(data, dict) else {}

    def find_by_name(self, name: str) -> str | None:
        name = (name or "").strip()
        if not name:
            return None
        data = self.list_browsers(page=0, page_size=100, name=name)
        for item in data.get("list") or []:
            if str(item.get("name") or "").strip() == name:
                bid = str(item.get("id") or "").strip()
                if bid:
                    return bid
        # fallback: fuzzy contains
        for item in data.get("list") or []:
            if name in str(item.get("name") or ""):
                bid = str(item.get("id") or "").strip()
                if bid:
                    return bid
        return None

    def detail(self, browser_id: str) -> dict:
        data = self.post("/browser/detail", {"id": browser_id}).get("data")
        return data if isinstance(data, dict) else {}

    def create(self, body: dict) -> str:
        """Create profile via /browser/update without id. Returns new id."""
        payload = dict(body or {})
        payload.pop("id", None)
        if "browserFingerPrint" not in payload:
            payload["browserFingerPrint"] = {}
        data = self.post("/browser/update", payload).get("data")
        if isinstance(data, dict):
            bid = str(data.get("id") or "").strip()
            if bid:
                return bid
        if isinstance(data, str) and data.strip():
            return data.strip()
        # some versions return id at top-level data as object with id only after list
        raise BitBrowserError(f"创建窗口成功但未返回 id: {data!r}")

    def update(self, browser_id: str, body: dict) -> dict:
        payload = dict(body or {})
        payload["id"] = browser_id
        if "browserFingerPrint" not in payload:
            # keep existing fingerprint on update unless caller sets it
            pass
        return self.post("/browser/update", payload)

    def open(
        self,
        browser_id: str,
        *,
        args: list[str] | None = None,
        load_extensions: bool = True,
        extract_ip: bool = False,
    ) -> dict:
        body: dict[str, Any] = {"id": browser_id}
        if args:
            body["args"] = list(args)
        if load_extensions:
            body["loadExtensions"] = True
        if extract_ip:
            body["extractIp"] = True
        data = self.post("/browser/open", body).get("data")
        if not isinstance(data, dict) or not data.get("http"):
            raise BitBrowserError(f"打开窗口未返回 http 调试地址: {data!r}")
        return data

    def close(self, browser_id: str) -> None:
        self.post("/browser/close", {"id": browser_id})

    def delete(self, browser_id: str) -> None:
        self.post("/browser/delete", {"id": browser_id})


def parse_proxy_for_bitbrowser(proxy: str) -> dict[str, Any]:
    """Map config proxy URL to BitBrowser custom proxy fields."""
    proxy = (proxy or "").strip()
    if not proxy:
        return {"proxyMethod": 2, "proxyType": "noproxy"}
    u = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    host = u.hostname or ""
    if not host:
        return {"proxyMethod": 2, "proxyType": "noproxy"}
    scheme = (u.scheme or "http").lower()
    if scheme in ("socks5", "socks5h"):
        proxy_type = "socks5"
    elif scheme == "https":
        proxy_type = "https"
    else:
        proxy_type = "http"
    port = u.port or (443 if proxy_type == "https" else 80)
    out: dict[str, Any] = {
        "proxyMethod": 2,
        "proxyType": proxy_type,
        "host": host,
        "port": int(port),
        "proxyUserName": u.username or "",
        "proxyPassword": u.password or "",
    }
    return out


class BitBrowserIdPool:
    """Claim/release fixed browser ids across worker threads."""

    def __init__(self, ids: list[str]):
        self._ids = [i.strip() for i in ids if str(i).strip()]
        self._lock = threading.Lock()
        self._in_use: set[str] = set()

    def claim(self, preferred: str | None = None) -> str:
        with self._lock:
            if preferred and preferred in self._ids and preferred not in self._in_use:
                self._in_use.add(preferred)
                return preferred
            for bid in self._ids:
                if bid not in self._in_use:
                    self._in_use.add(bid)
                    return bid
        raise BitBrowserError(
            f"BitBrowser 窗口池已耗尽（共 {len(self._ids)} 个）。"
            "请增加 bitbrowser_browser_ids，或把 threads 调低，或开启 bitbrowser_auto_create"
        )

    def release(self, browser_id: str) -> None:
        with self._lock:
            self._in_use.discard(browser_id)


_pool_lock = threading.Lock()
_id_pool: BitBrowserIdPool | None = None


def reset_id_pool() -> None:
    global _id_pool
    with _pool_lock:
        _id_pool = None


def get_id_pool(ids: list[str]) -> BitBrowserIdPool:
    global _id_pool
    with _pool_lock:
        if _id_pool is None:
            _id_pool = BitBrowserIdPool(ids)
        return _id_pool


def build_open_args(
    *,
    extension_path: str | None = None,
    headless: bool = False,
    extra_args: list[str] | None = None,
    slim: bool = True,
) -> list[str]:
    args: list[str] = []
    if slim:
        args.extend(
            [
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-dev-shm-usage",
                "--disable-images",
                "--mute-audio",
                "--no-first-run",
            ]
        )
    if headless:
        args.append("--headless=new")
        args.append("--window-size=1280,900")
    if extension_path:
        args.append(f"--load-extension={extension_path}")
    if extra_args:
        for a in extra_args:
            a = str(a).strip()
            if a and a not in args:
                args.append(a)
    return args


def resolve_browser_id(client: BitBrowserClient, cfg: dict, log=None) -> tuple[str, dict]:
    """Resolve which BitBrowser profile to open.

    Returns (browser_id, meta) where meta may include:
      - from_pool / ephemeral / name
    """
    cfg = cfg or {}
    meta: dict[str, Any] = {"ephemeral": False, "from_pool": False}

    ids_raw = cfg.get("bitbrowser_browser_ids") or []
    if isinstance(ids_raw, str):
        ids = [x.strip() for x in ids_raw.split(",") if x.strip()]
    else:
        ids = [str(x).strip() for x in ids_raw if str(x).strip()]

    single = str(cfg.get("bitbrowser_browser_id") or "").strip()
    if single and single not in ids:
        ids = [single, *ids]

    if ids:
        pool = get_id_pool(ids)
        bid = pool.claim()
        meta["from_pool"] = True
        meta["pool_size"] = len(ids)
        if log:
            log(f"[bitbrowser] 使用窗口池 id={bid[:8]}… ({len(ids)} 个)")
        return bid, meta

    name = str(cfg.get("bitbrowser_name") or "").strip()
    if name:
        found = client.find_by_name(name)
        if found:
            if log:
                log(f"[bitbrowser] 按名称命中 name={name} id={found[:8]}…")
            return found, meta
        if not cfg.get("bitbrowser_auto_create", True):
            raise BitBrowserError(f"未找到名为 {name!r} 的 BitBrowser 窗口，且 auto_create=false")

    auto_create = bool(cfg.get("bitbrowser_auto_create", True))
    if not auto_create:
        raise BitBrowserError(
            "未配置 bitbrowser_browser_id / bitbrowser_browser_ids / 可命中的 bitbrowser_name，"
            "且 bitbrowser_auto_create=false"
        )

    # create ephemeral profile
    create_name = name or str(cfg.get("bitbrowser_create_name") or "grok-reg").strip() or "grok-reg"
    # unique suffix to avoid name clash under concurrency
    create_name = f"{create_name}-{int(time.time() * 1000) % 10_000_000}-{threading.get_ident() % 10000}"
    body: dict[str, Any] = {
        "name": create_name,
        "remark": "auto by grok_reg-protocol_cpa",
        "platform": "https://grok.com/",
        "platformIcon": "other",
        "url": "",
        "browserFingerPrint": {},
        **parse_proxy_for_bitbrowser(str(cfg.get("proxy") or "")),
    }
    # optional group
    group_id = str(cfg.get("bitbrowser_group_id") or "").strip()
    if group_id:
        body["groupId"] = group_id
    bid = client.create(body)
    meta["ephemeral"] = bool(cfg.get("bitbrowser_delete_on_release", True))
    meta["name"] = create_name
    if log:
        log(f"[bitbrowser] 自动创建窗口 name={create_name} id={bid[:8]}… ephemeral={meta['ephemeral']}")
    return bid, meta


def open_and_attach(
    cfg: dict | None = None,
    *,
    extension_path: str | None = None,
    log=None,
):
    """Open a BitBrowser profile and attach DrissionPage Chromium to its debugger.

    Returns (browser, meta) where meta has browser_id / http / ephemeral / from_pool.
    """
    from DrissionPage import Chromium, ChromiumOptions

    cfg = dict(cfg or {})
    api = str(cfg.get("bitbrowser_api") or DEFAULT_API).strip() or DEFAULT_API
    timeout = float(cfg.get("bitbrowser_timeout") or 60)
    client = BitBrowserClient(api_base=api, timeout=timeout)

    if not client.health():
        raise BitBrowserError(f"BitBrowser Local API 不健康: {api}")

    browser_id, meta = resolve_browser_id(client, cfg, log=log)

    # optional: sync proxy from config onto existing profile before open
    if cfg.get("bitbrowser_sync_proxy", False):
        try:
            proxy_fields = parse_proxy_for_bitbrowser(str(cfg.get("proxy") or ""))
            client.post(
                "/browser/update/partial",
                {"ids": [browser_id], **proxy_fields},
            )
        except Exception as exc:
            if log:
                log(f"[bitbrowser] sync proxy 失败（忽略）: {exc}")

    extra_args = cfg.get("bitbrowser_args") or []
    if isinstance(extra_args, str):
        extra_args = [extra_args]
    headless = bool(cfg.get("register_headless", False))
    load_ext = bool(cfg.get("bitbrowser_load_extensions", True))
    use_turnstile = bool(cfg.get("bitbrowser_load_turnstile_patch", True))
    ext = extension_path if use_turnstile else None
    args = build_open_args(
        extension_path=ext,
        headless=headless,
        extra_args=list(extra_args),
        slim=bool(cfg.get("bitbrowser_slim_args", True)),
    )

    # close first if leftover process (best-effort)
    try:
        client.close(browser_id)
        time.sleep(0.3)
    except Exception:
        pass

    opened = client.open(
        browser_id,
        args=args,
        load_extensions=load_ext,
        extract_ip=bool(cfg.get("bitbrowser_extract_ip", False)),
    )
    http_addr = str(opened.get("http") or "").strip()
    if log:
        log(f"[bitbrowser] opened id={browser_id[:8]}… http={http_addr} pid={opened.get('pid')}")

    co = ChromiumOptions()
    co.set_address(http_addr)
    co.existing_only(True)
    try:
        co.set_timeouts(base=1)
    except Exception:
        pass

    # retry attach — chrome may need a moment after open
    last_exc: Exception | None = None
    browser = None
    for attempt in range(1, 6):
        try:
            browser = Chromium(co)
            break
        except Exception as exc:
            last_exc = exc
            time.sleep(0.4 * attempt)
    if browser is None:
        try:
            client.close(browser_id)
        except Exception:
            pass
        if meta.get("from_pool"):
            try:
                global _id_pool
                if _id_pool is not None:
                    _id_pool.release(browser_id)
            except Exception:
                pass
        raise BitBrowserError(f"DrissionPage 接管 BitBrowser 失败 ({http_addr}): {last_exc}")

    meta.update(
        {
            "browser_id": browser_id,
            "http": http_addr,
            "ws": opened.get("ws"),
            "pid": opened.get("pid"),
            "client": client,
        }
    )
    # stash for TabPool release
    try:
        browser._bitbrowser_meta = meta  # type: ignore[attr-defined]
        browser._bitbrowser_id = browser_id  # type: ignore[attr-defined]
    except Exception:
        pass
    return browser, meta


def release_attached(browser, *, delete_ephemeral: bool | None = None, log=None) -> None:
    """Close BitBrowser window via API; free pool slot; optionally delete ephemeral profile."""
    meta = getattr(browser, "_bitbrowser_meta", None) or {}
    browser_id = str(getattr(browser, "_bitbrowser_id", None) or meta.get("browser_id") or "").strip()
    client: BitBrowserClient | None = meta.get("client")
    if client is None:
        api = DEFAULT_API
        try:
            client = BitBrowserClient(api_base=api)
        except Exception:
            client = None

    if browser_id and client is not None:
        try:
            client.close(browser_id)
            if log:
                log(f"[bitbrowser] closed id={browser_id[:8]}…")
        except Exception as exc:
            if log:
                log(f"[bitbrowser] close 失败: {exc}")

        do_delete = meta.get("ephemeral", False) if delete_ephemeral is None else bool(delete_ephemeral)
        if do_delete:
            try:
                client.delete(browser_id)
                if log:
                    log(f"[bitbrowser] deleted ephemeral id={browser_id[:8]}…")
            except Exception as exc:
                if log:
                    log(f"[bitbrowser] delete 失败: {exc}")

        if meta.get("from_pool"):
            try:
                # release into existing pool if any
                global _id_pool
                if _id_pool is not None:
                    _id_pool.release(browser_id)
            except Exception:
                pass

    # Do not browser.quit(del_data=True): BitBrowser owns the profile user-data.
    # Best-effort disconnect only.
    try:
        quit_fn = getattr(browser, "quit", None)
        if callable(quit_fn):
            try:
                quit_fn(timeout=2, force=True, del_data=False)
            except TypeError:
                try:
                    quit_fn()
                except Exception:
                    pass
    except Exception:
        pass
