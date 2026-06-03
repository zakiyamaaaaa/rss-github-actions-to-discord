#!/usr/bin/env python3
"""RSS フィードの新着を検知して Discord に通知する。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
SITES_FILE = ROOT / "sites.yaml"
STATE_FILE = Path(os.environ.get("STATE_FILE", ROOT / ".state" / "last.json"))


def webhook_env_key(company: str) -> str:
    slug = company.strip().upper().replace("-", "_")
    return f"DISCORD_WEBHOOK_URL_{slug}"


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

    return sites


def load_state() -> dict[str, str]:
    if not STATE_FILE.exists():
        return {}
    with STATE_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict[str, str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def latest_entry(feed_url: str) -> tuple[str, str, str]:
    """戻り値: (entry_id, title, link)"""
    parsed = feedparser.parse(feed_url)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"フィードの取得に失敗: {feed_url}")

    if not parsed.entries:
        raise RuntimeError(f"エントリがありません: {feed_url}")

    entry = parsed.entries[0]
    entry_id = entry.get("id") or entry.get("link") or entry.get("title")
    if not entry_id:
        raise RuntimeError(f"エントリ ID を特定できません: {feed_url}")

    title = entry.get("title", "(no title)")
    link = entry.get("link", feed_url)
    return str(entry_id), str(title), str(link)


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


def main() -> None:
    sites = load_sites()
    webhooks = load_webhooks(sites)
    state = load_state()
    updated = False
    notifications = 0

    for site in sites:
        company = site["company"]
        name = site["name"]
        url = site["url"]
        webhook_url = webhooks[company]
        print(f"Checking: [{company}] {name}")

        entry_id, title, link = latest_entry(url)
        previous = state.get(name)

        if previous is None:
            print(f"  初回: 状態を保存 ({entry_id})")
            state[name] = entry_id
            updated = True
            continue

        if entry_id == previous:
            print("  変更なし")
            continue

        print(f"  新着: {title}")
        notify_discord(webhook_url, name, title, link)
        state[name] = entry_id
        updated = True
        notifications += 1

    if updated:
        save_state(state)

    print(f"完了: {notifications} 件通知")


if __name__ == "__main__":
    main()
