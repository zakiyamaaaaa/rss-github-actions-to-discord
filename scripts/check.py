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
SCRIPT_VERSION = "seen-urls-v4"
MAX_SEEN_URLS = 5000
# 1 サイト 1 回あたりの最大通知数（溜まっていた場合は新しい順にこの件数だけ通知し、残りは既読扱い）
MAX_NOTIFY_PER_SITE = 20
DISCORD_POST_INTERVAL = 1.0
GENERIC_LINK_TEXTS = {
    "readmore",
    "details",
    "learnmore",
    "more",
    "詳細を読む",
    "詳しく見る",
    "もっと読む",
}


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


def truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


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


def compact_text(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def is_generic_link_text(title: str) -> bool:
    compact = compact_text(title)
    if not compact:
        return True
    return any(compact == generic or compact == generic * 2 for generic in GENERIC_LINK_TEXTS)


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


def list_page_entries(
    page_url: str,
    *,
    link_selector: str | None = None,
    link_path_regex: str | None = None,
) -> list[tuple[str, str]]:
    """一覧ページ上の記事リンクをページ内の出現順で返す。[(title, link), ...]

    ピン留め（featured）記事が先頭に来るページもあるため、先頭 1 件ではなく全件を返す。
    """
    headers = {"User-Agent": "rss-github-actions-to-discord/1.0"}
    resp = requests.get(page_url, headers=headers, timeout=30)
    resp.raise_for_status()
    if urlparse(resp.url).path.rstrip("/") != urlparse(page_url).path.rstrip("/"):
        print(f"  注意: {page_url} は {resp.url} にリダイレクトされました（sites.yaml の URL 更新を検討）")

    soup = BeautifulSoup(resp.text, "html.parser")
    locale_prefix = ""
    path = urlparse(page_url).path
    match = re.match(r"^/([a-z]{2}-[A-Z]{2})/", path)
    if match:
        locale_prefix = f"/{match.group(1)}/"

    path_pattern = re.compile(link_path_regex) if link_path_regex else None
    selector = link_selector or 'a[href*="/index/"]'

    entries: list[tuple[str, str]] = []
    seen_hrefs: set[str] = set()
    for anchor in soup.select(selector):
        href = urljoin(page_url, anchor["href"])
        if normalize_url(href) in seen_hrefs:
            continue

        href_path = urlparse(href).path
        if path_pattern and not path_pattern.search(href_path):
            continue
        if not path_pattern and locale_prefix and locale_prefix not in href_path:
            continue
        # カード型リンクは見出し要素があればそれをタイトルにする
        heading = anchor.find(["h1", "h2", "h3", "h4"])
        source = heading if heading else anchor
        title = re.sub(r"\s+", " ", source.get_text(" ", strip=True))
        if len(title) < 8 or is_generic_link_text(title):
            continue
        seen_hrefs.add(normalize_url(href))
        entries.append((title, href))

    if not entries:
        raise RuntimeError(f"記事リンクを取得できません: {page_url}")
    return entries


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
    for _ in range(5):
        resp = requests.post(webhook_url, json=payload, timeout=30)
        if resp.status_code == 429:
            retry_after = float(resp.json().get("retry_after", 1.0))
            print(f"  Discord rate limit: {retry_after}s 待機")
            time.sleep(retry_after + 0.5)
            continue
        resp.raise_for_status()
        time.sleep(DISCORD_POST_INTERVAL)
        return
    resp.raise_for_status()


def process_entries(
    *,
    name: str,
    entries: list[tuple[str, str]],
    webhook_url: str,
    state: dict,
    prune_to_entries: bool,
    notify_on_first_run: bool = False,
) -> int:
    """entries（新しい順）のうち未読のものを Discord に通知し、state を更新する。"""
    seen = get_seen_urls(state, name)
    notifications = 0
    entry_urls = {normalize_url(link) for _, link in entries}

    def save() -> None:
        if prune_to_entries:
            # RSS: フィードから消えた URL は二度と新着にならないので捨てる。
            # （件数上限で切ると、フィードが上限より大きい場合に毎回同じ記事が「新着」扱いになる）
            set_seen_urls(state, name, seen & entry_urls)
        else:
            set_seen_urls(state, name, seen)

    if not seen:
        seen.update(entry_urls)
        save()
        if notify_on_first_run:
            title, link = entries[0]
            print(f"  初回通知: {title}")
            notify_discord(webhook_url, name, title, link)
            return 1
        print(f"  初回: {len(seen)} 件を baseline 登録（通知なし）")
        return 0

    new_entries = [
        (title, link)
        for title, link in entries
        if normalize_url(link) not in seen
    ]

    if not new_entries:
        print(f"  変更なし (seen={len(seen)})")
        return 0

    skipped = new_entries[MAX_NOTIFY_PER_SITE:]
    if skipped:
        print(f"  新着 {len(new_entries)} 件のうち古い {len(skipped)} 件は通知せず既読扱い")
        for _, link in skipped:
            seen.add(normalize_url(link))
        save()

    try:
        for title, link in reversed(new_entries[:MAX_NOTIFY_PER_SITE]):
            print(f"  新着: {title}")
            notify_discord(webhook_url, name, title, link)
            seen.add(normalize_url(link))
            notifications += 1
    finally:
        # 途中で失敗しても、通知済みの分は保存して重複通知を防ぐ
        save()
    return notifications


def process_rss_site(
    *,
    name: str,
    feed_url: str,
    webhook_url: str,
    state: dict,
) -> int:
    return process_entries(
        name=name,
        entries=list_rss_entries(feed_url),
        webhook_url=webhook_url,
        state=state,
        prune_to_entries=True,
    )


def process_page_site(
    *,
    name: str,
    page_url: str,
    webhook_url: str,
    state: dict,
    link_selector: str | None = None,
    link_path_regex: str | None = None,
    notify_on_first_run: bool = False,
) -> int:
    entries = list_page_entries(
        page_url,
        link_selector=link_selector,
        link_path_regex=link_path_regex,
    )
    # 一覧ページは並び替え・ピン留めで記事が出入りするので、既読は捨てずに保持する
    return process_entries(
        name=name,
        entries=entries,
        webhook_url=webhook_url,
        state=state,
        prune_to_entries=False,
        notify_on_first_run=notify_on_first_run,
    )


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
    failures: list[str] = []

    for site in sites:
        company = site["company"]
        name = site["name"]
        site_type = site.get("type", "rss")
        webhook_url = webhooks[company]
        print(f"Checking: [{company}] {name} ({site_type})")

        # 1 サイトの失敗で他サイトの処理・state 保存が止まらないようにする
        try:
            if site_type == "page":
                notifications += process_page_site(
                    name=name,
                    page_url=site["url"],
                    webhook_url=webhook_url,
                    state=state,
                    link_selector=site.get("link_selector"),
                    link_path_regex=site.get("link_path_regex"),
                    notify_on_first_run=truthy(site.get("notify_on_first_run")),
                )
            else:
                notifications += process_rss_site(
                    name=name,
                    feed_url=site["url"],
                    webhook_url=webhook_url,
                    state=state,
                )
        except Exception as e:  # noqa: BLE001
            print(f"  エラー: {type(e).__name__}: {e}", file=sys.stderr)
            print(f"::error title=check failed::[{company}] {name}: {e}")
            failures.append(name)

    save_state(state)
    print(f"state saved: {STATE_FILE}")
    print(f"完了: {notifications} 件通知")

    if failures:
        print(f"失敗したサイト: {', '.join(failures)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
