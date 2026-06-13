#!/usr/bin/env python3
"""RSS フィードの新着を検知して Discord に通知する。"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
SITES_FILE = ROOT / "sites.yaml"
STATE_FILE = Path(os.environ.get("STATE_FILE", ROOT / ".state" / "last.json"))
SCRIPT_VERSION = "seen-urls-v3"
MAX_SEEN_URLS = 500


def webhook_env_key(company: str) -> str:
    slug = company.strip().upper().replace("-", "_")
    return f"DISCORD_WEBHOOK_URL_{slug}"


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", "", ""))


def load_webhooks(sites: list[dict]) -> dict[str, str]:
    companies = {site["company"] for site in sites}
    webhooks: dict[str, str] = {}
    missing: list[str] = []

    for company in sorted(companies):
        env_key = webhook_env_key(company)
        url = os.environ.get(env_key, "").strip()
        if not url:
            missing.append(env_key)
        else:
            webhooks[company] = url

    if missing:
        print("未設定の Webhook Secret:", ", ".join(missing), file=sys.stderr)
        sys.exit(1)

    return webhooks


def load_sites() -> list[dict]:
    with SITES_FILE.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    sites = data.get("sites") or []
    if not sites:
        print("sites.yaml に監視対象がありません", file=sys.stderr)
        sys.exit(1)

    for site in sites:
        if not site.get("company"):
            print(f"company が未設定です: {site}", file=sys.stderr)
            sys.exit(1)
        site_type = site.get("type", "rss")
        if site_type not in {"rss", "page"}:
            print(f"未対応の type です ({site_type}): {site}", file=sys.stderr)
            sys.exit(1)

    return sites


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    with STATE_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_seen_urls(state: dict, name: str) -> set[str]:
    value = state.get(name)
    if value is None:
        return set()
    if isinstance(value, str):
        return {normalize_url(value)}
    if isinstance(value, list):
        return {normalize_url(url) for url in value}
    raise RuntimeError(f"不正な state 形式です ({name}): {type(value).__name__}")


def set_seen_urls(state: dict, name: str, seen: set[str]) -> None:
    state[name] = sorted(seen)[-MAX_SEEN_URLS:]


def entry_sort_key(entry) -> float:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return time.mktime(parsed)
    return 0.0


def list_rss_entries(feed_url: str) -> list[tuple[str, str]]:
    """戻り値: [(title, link), ...] 新しい順"""
    parsed = feedparser.parse(feed_url)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"フィードの取得に失敗: {feed_url}")
    if not parsed.entries:
        raise RuntimeError(f"エントリがありません: {feed_url}")

    entries: list[tuple[str, str]] = []
    for entry in sorted(parsed.entries, key=entry_sort_key, reverse=True):
        link = entry.get("link")
        if not link:
            continue
        title = str(entry.get("title", "(no title)"))
        entries.append((title, str(link)))

    if not entries:
        raise RuntimeError(f"有効なエントリがありません: {feed_url}")
    return entries


def latest_entry_page(page_url: str) -> tuple[str, str]:
    headers = {"User-Agent": "rss-github-actions-to-discord/1.0"}
    resp = requests.get(page_url, headers=headers, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    locale_prefix = ""
    path = urlparse(page_url).path
    match = re.match(r"^/([a-z]{2}-[A-Z]{2})/", path)
    if match:
        locale_prefix = f"/{match.group(1)}/"

    seen_hrefs: set[str] = set()
    for anchor in soup.select('a[href*="/index/"]'):
        href = urljoin(page_url, anchor["href"])
        if href in seen_hrefs:
            continue
        if locale_prefix and locale_prefix not in urlparse(href).path:
            continue
        title = re.sub(r"\s+", " ", anchor.get_text(strip=True))
        if len(title) < 8:
            continue
        return title, href

    raise RuntimeError(f"記事リンクを取得できません: {page_url}")


def notify_discord(webhook_url: str, name: str, title: str, link: str) -> None:
    payload = {
        "content": f"**{name}** に新しい記事があります",
        "embeds": [
            {
                "title": title[:256],
                "url": link,
                "color": 0x5865F2,
            }
        ],
    }
    resp = requests.post(webhook_url, json=payload, timeout=30)
    resp.raise_for_status()


def process_rss_site(
    *,
    name: str,
    feed_url: str,
    webhook_url: str,
    state: dict,
) -> int:
    entries = list_rss_entries(feed_url)
    seen = get_seen_urls(state, name)
    notifications = 0

    if not seen:
        for _, link in entries:
            seen.add(normalize_url(link))
        set_seen_urls(state, name, seen)
        print(f"  初回: フィード内 {len(seen)} 件を baseline 登録（通知なし）")
        return 0

    new_entries = [
        (title, link)
        for title, link in entries
        if normalize_url(link) not in seen
    ]

    if not new_entries:
        print(f"  変更なし (seen={len(seen)})")
        return 0

    for title, link in reversed(new_entries):
        print(f"  新着: {title}")
        notify_discord(webhook_url, name, title, link)
        seen.add(normalize_url(link))
        notifications += 1

    set_seen_urls(state, name, seen)
    return notifications


def process_page_site(
    *,
    name: str,
    page_url: str,
    webhook_url: str,
    state: dict,
) -> int:
    title, link = latest_entry_page(page_url)
    normalized = normalize_url(link)
    seen = get_seen_urls(state, name)

    if not seen:
        seen.add(normalized)
        set_seen_urls(state, name, seen)
        print(f"  初回: baseline 登録 ({normalized})")
        return 0

    if normalized in seen:
        print(f"  変更なし (seen={len(seen)})")
        return 0

    print(f"  新着: {title}")
    notify_discord(webhook_url, name, title, link)
    seen.add(normalized)
    set_seen_urls(state, name, seen)
    return 1


def main() -> None:
    print(f"check.py {SCRIPT_VERSION}")
    print(f"sites file: {SITES_FILE} (exists={SITES_FILE.exists()})")
    print(f"state file: {STATE_FILE} (exists={STATE_FILE.exists()})")

    sites = load_sites()
    print(f"loaded {len(sites)} site(s):")
    for site in sites:
        print(f"  - company={site['company']}, name={site['name']}, type={site.get('type', 'rss')}")

    webhooks = load_webhooks(sites)
    print(f"webhooks ready for: {', '.join(sorted(webhooks))}")

    state = load_state()
    if state:
        print(f"restored state keys: {', '.join(sorted(state))}")
    else:
        print("restored state: (empty)")

    notifications = 0

    for site in sites:
        company = site["company"]
        name = site["name"]
        site_type = site.get("type", "rss")
        webhook_url = webhooks[company]
        print(f"Checking: [{company}] {name} ({site_type})")

        if site_type == "page":
            notifications += process_page_site(
                name=name,
                page_url=site["url"],
                webhook_url=webhook_url,
                state=state,
            )
        else:
            notifications += process_rss_site(
                name=name,
                feed_url=site["url"],
                webhook_url=webhook_url,
                state=state,
            )

    save_state(state)
    print(f"state saved: {STATE_FILE}")
    print(f"完了: {notifications} 件通知")


if __name__ == "__main__":
    main()
