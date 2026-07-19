# rss-github-actions-to-discord

RSS またはニュース一覧ページの更新を 1 日 1 回チェックし、新着があれば Discord に通知します。  
企業（`company`）ごとに別チャンネルへ送る場合は、Webhook Secret も企業ごとに登録します。

## セットアップ

### 1. Discord Webhook を作成（企業ごと）

例: Anthropic 用チャンネルに Webhook を作成し、URL をコピー。

### 2. GitHub Secret を登録

リポジトリの **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|------|-------|
| `DISCORD_WEBHOOK_URL_ANTHROPIC` | Anthropic 用 Discord Webhook の URL |
| `DISCORD_WEBHOOK_URL_OPENAI` | OpenAI 用 Discord Webhook の URL |

命名規則: `DISCORD_WEBHOOK_URL_<COMPANY>`（`sites.yaml` の `company` を大文字にしたもの）

| company（sites.yaml） | Secret 名 |
|----------------------|-----------|
| `anthropic` | `DISCORD_WEBHOOK_URL_ANTHROPIC` |
| `openai` | `DISCORD_WEBHOOK_URL_OPENAI` |

新しい企業を追加するときは、Secret と `.github/workflows/check.yml` の `env` の両方に追記してください。

### 3. 変更を GitHub に反映

ローカルで編集しただけでは Actions は更新されません。必ず push してください。

GitHub 上の `scripts/check.py` に `DISCORD_WEBHOOK_URL が未設定` とあれば **古いコードが動いています**。push 後に再実行してください。

### 4. 手動テスト（任意）

Actions タブ → **Check site updates** → **Run workflow**

初回実行は「最新記事を基準に保存」するだけで通知しません。2 回目以降で新着があれば Discord に届きます。

ただし `sites.yaml` で `notify_on_first_run: true` を指定した監視対象は、初回にも最新記事を 1 件通知します。

## 監視サイトの追加

`sites.yaml` に追記して commit してください。

```yaml
# RSS
sites:
  - company: anthropic
    name: 表示名
    url: https://example.com/feed.xml

# HTML 一覧（RSS がないページ向け）
  - company: openai
    name: OpenAI News (日本語)
    type: page
    url: https://openai.com/ja-JP/news/

# HTML 一覧のリンク条件を指定する例
  - company: anthropic
    name: Claude Blog
    type: page
    url: https://claude.com/blog
    link_selector: a[href]
    link_path_regex: ^/blog/[^/?#]+/?$
    notify_on_first_run: true
```

別企業を追加する場合:

1. 上記のとおり `company` を指定
2. GitHub Secret `DISCORD_WEBHOOK_URL_<COMPANY>` を追加
3. `.github/workflows/check.yml` の `env` に同じ Secret を追加

## ローカル実行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DISCORD_WEBHOOK_URL_ANTHROPIC="https://discord.com/api/webhooks/..."
export DISCORD_WEBHOOK_URL_OPENAI="https://discord.com/api/webhooks/..."
python scripts/check.py
```

## スケジュール

- 毎日 **06:00 JST**（21:00 UTC）
- 変更する場合は `.github/workflows/check.yml` の `cron` を編集
