#!/usr/bin/env python3
"""单独测试 Hotmail/Outlook 邮箱验证码获取能力。

不启动浏览器、不注册账号，只验证：
  1. OAuth2 token 刷新（IMAP scope / Graph scope）是否成功
  2. access_token 是否为 JWT（Graph 需要）
  3. Graph API / IMAP 能否拉到邮件
  4. 能否从邮件中提取验证码

用法：
  uv run python scripts/test_hotmail_code.py                    # 测试第一个账号
  uv run python scripts/test_hotmail_code.py --email foo@h.com  # 指定账号
  uv run python scripts/test_hotmail_code.py --mode graph        # 只测 Graph
  uv run python scripts/test_hotmail_code.py --mode imap         # 只测 IMAP
  uv run python scripts/test_hotmail_code.py --mode auto         # 测 auto（先 Graph 后 IMAP）
  uv run python scripts/test_hotmail_code.py --send-test         # 先发测试邮件再拉取
"""

import argparse
import os
import sys
import time

# 确保能 import 项目根目录的模块
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)


def log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def load_config():
    import json

    config_path = os.path.join(_PROJECT_ROOT, "config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_accounts():
    import grok_register_ttk as g

    accounts = g._hotmail_load_accounts(force=True)
    return accounts


def find_account(accounts, email_filter=None):
    if not email_filter:
        if not accounts:
            raise Exception("mail_credentials.txt 中无有效账号")
        return accounts[0]
    for acc in accounts:
        if email_filter.lower() in acc["email"].lower():
            return acc
    raise Exception(f"未找到匹配 '{email_filter}' 的账号")


def test_token_refresh(account, for_graph, log_fn=print):
    import grok_register_ttk as g

    label = "Graph" if for_graph else "IMAP"
    log_fn(f"\n{'='*60}")
    log_fn(f"[1] 测试 OAuth2 token 刷新 ({label})")
    log_fn(f"{'='*60}")
    log_fn(f"  邮箱: {account['email']}")
    log_fn(f"  ClientID: {account['client_id'][:8]}...")
    log_fn(f"  RefreshToken: {account['refresh_token'][:12]}...")

    try:
        token = g.hotmail_refresh_access_token(
            account, log_callback=lambda msg: log_fn(f"  {msg}"), for_graph=for_graph
        )
        is_jwt = g._is_jwt_token(token)
        log_fn(f"  access_token: {token[:40]}...")
        log_fn(f"  是否 JWT: {'是' if is_jwt else '否 (MSA token，Graph /users/ 端点仍可用)'}")
        if is_jwt:
            # 尝试解码 JWT payload 看看 scope/audience
            try:
                import base64
                import json as json_mod

                parts = token.split(".")
                payload_b64 = parts[1]
                padding = 4 - len(payload_b64) % 4
                if padding != 4:
                    payload_b64 += "=" * padding
                payload = json_mod.loads(base64.urlsafe_b64decode(payload_b64))
                log_fn(f"  JWT aud: {payload.get('aud', '?')}")
                log_fn(f"  JWT scp: {payload.get('scp', '?')}")
                log_fn(f"  JWT iss: {payload.get('iss', '?')}")
            except Exception:
                pass
        return token, is_jwt
    except Exception as exc:
        log_fn(f"  失败: {exc}")
        return None, False


def test_graph_fetch(account, access_token, target_email, log_fn=print):
    import grok_register_ttk as g

    log_fn(f"\n{'='*60}")
    log_fn(f"[2] 测试 Graph API 邮件拉取")
    log_fn(f"{'='*60}")
    log_fn(f"  端点: {g._hotmail_graph_get_endpoint()}/users/{account['email']}/mailFolders/inbox/messages")
    log_fn(f"  目标邮箱: {target_email}")

    try:
        code = g._hotmail_graph_get_code(
            account["email"],
            target_email,
            access_token,
            log_callback=lambda msg: log_fn(f"  {msg}"),
        )
        if code:
            log_fn(f"  结果: 提取到验证码 -> {code}")
        else:
            log_fn(f"  结果: 未找到验证码（可能邮箱中没有 xAI 验证码邮件）")
        return code
    except Exception as exc:
        log_fn(f"  失败: {exc}")
        return None


def test_imap_fetch(account, access_token, target_email, log_fn=print):
    import grok_register_ttk as g

    log_fn(f"\n{'='*60}")
    log_fn(f"[2] 测试 IMAP XOAUTH2 邮件拉取")
    log_fn(f"{'='*60}")
    hosts = g._hotmail_get_imap_hosts()
    log_fn(f"  IMAP hosts: {hosts}")
    log_fn(f"  目标邮箱: {target_email}")

    for host in hosts:
        log_fn(f"\n  --- 尝试 host: {host} ---")
        try:
            code = g._hotmail_imap_get_code(
                account["email"],
                target_email,
                access_token,
                log_callback=lambda msg: log_fn(f"  {msg}"),
                host=host,
            )
            if code:
                log_fn(f"  结果: 提取到验证码 -> {code}")
                return code
            else:
                log_fn(f"  结果: 未找到验证码（host={host}）")
                break  # 成功连接但没码，不用换 host
        except Exception as exc:
            log_fn(f"  失败: {exc}")
            continue
    return None


def send_test_email(account, log_fn=print):
    """用 smtplib 给自己发一封带假验证码的测试邮件。"""
    import smtplib
    from email.mime.text import MIMEText
    from email.utils import formatdate

    log_fn(f"\n{'='*60}")
    log_fn(f"[0] 发送测试邮件")
    log_fn(f"{'='*60}")

    email_addr = account["email"]
    password = account.get("password", "")
    if not password:
        log_fn("  账号无密码，跳过发送测试邮件")
        return False

    test_code = "TEST-999"
    msg = MIMEText(
        f"This is a test email for verification code extraction.\n\n"
        f"Your confirmation code is {test_code}\n\n"
        f"Valid for 10 minutes.",
        "plain",
        "utf-8",
    )
    msg["Subject"] = f"xAI confirmation code {test_code}"
    msg["From"] = email_addr
    msg["To"] = email_addr
    msg["Date"] = formatdate(localtime=True)

    try:
        # Outlook SMTP
        with smtplib.SMTP("smtp-mail.outlook.com", 587, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(email_addr, password)
            server.sendmail(email_addr, [email_addr], msg.as_string())
        log_fn(f"  已发送测试邮件到 {email_addr}（验证码: {test_code}）")
        log_fn(f"  等待 10 秒让邮件到达...")
        time.sleep(10)
        return test_code
    except Exception as exc:
        log_fn(f"  发送失败（密码登录可能被微软禁用）: {exc}")
        log_fn(f"  跳过，直接拉取现有邮件")
        return False


def main():
    parser = argparse.ArgumentParser(description="测试 Hotmail/Outlook 邮箱验证码获取")
    parser.add_argument("--email", default=None, help="指定账号（模糊匹配）")
    parser.add_argument(
        "--mode",
        choices=["graph", "imap", "auto"],
        default="auto",
        help="测试模式（默认 auto：先 Graph 后 IMAP）",
    )
    parser.add_argument(
        "--send-test",
        action="store_true",
        help="先给自己发一封测试验证码邮件再拉取",
    )
    parser.add_argument(
        "--target",
        default=None,
        help="目标收件邮箱（默认用主邮箱；测试别名时指定 alias+xxx@hotmail.com）",
    )
    args = parser.parse_args()

    # 加载配置
    cfg = load_config()
    log(f"当前配置:")
    log(f"  hotmail_mail_fetch_mode = {cfg.get('hotmail_mail_fetch_mode', 'imap')}")
    log(f"  hotmail_graph_endpoint   = {cfg.get('hotmail_graph_endpoint', 'https://graph.microsoft.com/v1.0')}")
    log(f"  hotmail_imap_hosts       = {cfg.get('hotmail_imap_hosts', 'outlook.office365.com,imap-mail.outlook.com')}")
    log(f"  hotmail_recent_seconds   = {cfg.get('hotmail_recent_seconds', 900)}")
    log(f"  hotmail_imap_last_n      = {cfg.get('hotmail_imap_last_n', 30)}")

    # 加载账号
    accounts = load_accounts()
    log(f"\nmail_credentials.txt 中有 {len(accounts)} 个账号")
    account = find_account(accounts, args.email)
    target_email = args.target or account["email"]

    # 可选：发送测试邮件
    if args.send_test:
        send_test_email(account, log_fn=log)

    # 1. Graph 通道
    graph_code = None
    if args.mode in ("graph", "auto"):
        graph_token, is_jwt = test_token_refresh(account, for_graph=True, log_fn=log)
        if graph_token:
            # MSA token（非 JWT）也可用于 Graph /users/{email}/ 端点，直接尝试拉取
            graph_code = test_graph_fetch(account, graph_token, target_email, log_fn=log)

    # 2. IMAP 通道
    imap_code = None
    if args.mode in ("imap", "auto"):
        imap_token, _ = test_token_refresh(account, for_graph=False, log_fn=log)
        if imap_token:
            imap_code = test_imap_fetch(account, imap_token, target_email, log_fn=log)

    # 3. 总结
    log(f"\n{'='*60}")
    log(f"测试总结")
    log(f"{'='*60}")
    log(f"  账号: {account['email']}")
    log(f"  模式: {args.mode}")
    log(f"  Graph 验证码: {graph_code or '未获取'}")
    log(f"  IMAP  验证码: {imap_code or '未获取'}")
    if graph_code or imap_code:
        log(f"\n  验证码获取成功!")
    else:
        log(f"\n  未获取到验证码（可能邮箱中没有 xAI 验证码邮件）")
        log(f"  提示: 用 --send-test 参数发送测试邮件验证拉取链路")


if __name__ == "__main__":
    main()
