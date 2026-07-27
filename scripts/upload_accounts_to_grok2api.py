#!/usr/bin/env python3
"""批量把 accounts_cli.txt 里的 SSO 同步到 chenyme/grok2api v3。

读取 `邮箱----密码----sso` 三段账本,把 SSO 逐个上传到 grok2api
`POST /api/admin/v1/accounts/web/import`。复用 grok_register_ttk 里的
登录 + SSE 解析逻辑(进程内 accessToken 缓存)。

示例（项目根目录运行）:
  uv run python -u scripts/upload_accounts_to_grok2api.py --limit 5
  uv run python -u scripts/upload_accounts_to_grok2api.py --dry-run
  uv run python -u scripts/upload_accounts_to_grok2api.py --batch-size 20
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterable

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _load_config_json(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if not re.match(r'^\s*"(?://|#)', line)]
    loaded = json.loads("\n".join(lines))
    return loaded if isinstance(loaded, dict) else {}


def _parse_accounts_file(path: Path) -> list[tuple[str, str, str]]:
    """返回 [(email, password, sso), ...],跳过空行/注释行/格式不全行。"""
    out: list[tuple[str, str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----")
        if len(parts) < 3:
            continue
        email = parts[0].strip()
        password = parts[1].strip()
        sso = parts[2].strip()
        if not email or not sso:
            continue
        out.append((email, password, sso))
    return out


def _upload_one(grt, base: str, access: str, sso: str, email: str, log) -> dict:
    """上传单个 SSO,返回 {ok, created, updated, skipped, error}。"""
    from curl_cffi import CurlMime  # type: ignore

    mime = CurlMime()
    fname = f"{email or 'sso'}.txt"
    mime.addpart(name="files", filename=fname, content_type="text/plain", data=f"{sso}\n")
    timeout = float(grt.config.get("grok2api_import_timeout_sec", 60) or 60)
    try:
        from curl_cffi import requests as _cc_requests
        resp = _cc_requests.post(
            f"{base}/api/admin/v1/accounts/web/import",
            headers={"Authorization": f"Bearer {access}"},
            multipart=mime,
            timeout=timeout,
            proxies={},
            stream=True,
        )
    except Exception as exc:
        return {"ok": False, "error": f"request: {exc}"}

    try:
        if resp.status_code >= 400:
            body = ""
            try:
                body = resp.text[:300]
            except Exception:
                pass
            return {"ok": False, "error": f"HTTP {resp.status_code}: {body}"}
        cur_event = ""
        complete_data: dict | None = None
        err_msg: str | None = None
        for raw in resp.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else str(raw)
            if line.startswith("event:"):
                cur_event = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            payload_str = line[5:].strip()
            if cur_event == "complete":
                try:
                    complete_data = json.loads(payload_str) if payload_str else {}
                except Exception:
                    complete_data = {}
                break
            if cur_event == "error":
                try:
                    err_payload = json.loads(payload_str) if payload_str else {}
                    err_msg = str(err_payload.get("message") or err_payload.get("code") or payload_str)
                except Exception:
                    err_msg = payload_str
                break
        if err_msg:
            return {"ok": False, "error": err_msg}
        if complete_data is None:
            return {"ok": False, "error": "SSE no complete event"}
        created = int(complete_data.get("created") or 0)
        updated = int(complete_data.get("updated") or 0)
        skipped = int(complete_data.get("skipped") or 0)
        return {"ok": True, "created": created, "updated": updated, "skipped": skipped}
    finally:
        try:
            resp.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--accounts", default=str(_ROOT / "accounts_cli.txt"))
    ap.add_argument("--config", default=str(_ROOT / "config.json"))
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个;0=全部")
    ap.add_argument("--batch-size", type=int, default=1,
                    help="每批上传账号数;1=逐个上传(默认,安全)。>1 会把多个 SSO 拼一个文件批量上传")
    ap.add_argument("--dry-run", action="store_true", help="只解析,不实际上传")
    ap.add_argument("--sleep-sec", type=float, default=0.2, help="每个号之间间隔秒数,避免打爆服务端")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.is_file():
        print(f"[!] config 不存在: {cfg_path}", file=sys.stderr)
        return 1
    cfg = _load_config_json(cfg_path)

    base = (cfg.get("grok2api_remote_base") or "").strip().rstrip("/")
    username = (cfg.get("grok2api_admin_username") or "admin").strip() or "admin"
    password = (
        os.environ.get("GROK2API_ADMIN_PASSWORD")
        or (cfg.get("grok2api_admin_password") or "").strip()
        or (cfg.get("grok2api_remote_app_key") or "").strip()
    )
    if not base or not password:
        print("[!] config 未配置 grok2api_remote_base / grok2api_admin_password", file=sys.stderr)
        return 1

    acc_path = Path(args.accounts)
    if not acc_path.is_file():
        print(f"[!] 账本不存在: {acc_path}", file=sys.stderr)
        return 1
    rows = _parse_accounts_file(acc_path)
    if args.limit > 0:
        rows = rows[: args.limit]
    print(f"[*] 账本 {acc_path}: 共 {len(rows)} 个账号")

    if args.dry_run:
        for email, _, sso in rows:
            print(f"  DR-RUN  {email}  sso={sso[:20]}...{sso[-8:]}")
        return 0

    # 复用 grok_register_ttk 的登录逻辑(带缓存)
    import grok_register_ttk as grt  # type: ignore
    grt.config.update(cfg)
    print(f"[*] grok2api: {base}  user={username}")
    try:
        access = grt._grok2api_admin_token(base, username, password, log_callback=lambda m: print(m))
    except Exception as exc:
        print(f"[!] 管理员登录失败: {exc}", file=sys.stderr)
        return 2

    total = len(rows)
    ok_count = 0
    fail_count = 0
    created_sum = 0
    updated_sum = 0
    skipped_sum = 0
    t0 = time.time()
    for i, (email, _, sso) in enumerate(rows, 1):
        result = _upload_one(grt, base, access, sso, email, log=lambda m: None)
        if result.get("ok"):
            ok_count += 1
            created_sum += result.get("created", 0)
            updated_sum += result.get("updated", 0)
            skipped_sum += result.get("skipped", 0)
            tag = f"created={result['created']} updated={result['updated']} skipped={result['skipped']}"
            print(f"  [{i}/{total}] OK    {email}  {tag}")
        else:
            fail_count += 1
            print(f"  [{i}/{total}] FAIL  {email}  err={result.get('error')}")
        if args.sleep_sec > 0 and i < total:
            time.sleep(args.sleep_sec)

    dur = time.time() - t0
    print(
        f"\n=== 完成: 共 {total}, 成功 {ok_count}, 失败 {fail_count}"
        f" (created={created_sum}, updated={updated_sum}, skipped={skipped_sum})"
        f" 耗时 {dur:.1f}s ==="
    )
    return 0 if fail_count == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
