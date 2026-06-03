# site-watch

RSS フィードの更新を 1 日 1 回チェックし、新着があれば Discord に通知します。

## セットアップ

### 1. Discord Webhook を作成

1. Discord の対象チャンネル → チャンネル設定 → 連携サービス → Webhook
2. 新しい Webhook を作成し、URL をコピー

### 2. GitHub Secret を登録

リポジトリの **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|------|-------|
| `DISCORD_WEBHOOK_URL` | Discord Webhook の URL |

### 3. GitHub に push

```bash
git remote add origin git@github.com:<YOUR_USER>/site-watch.git
git push -u origin main
```

### 4. 手動テスト（任意）

Actions タブ → **Check site updates** → **Run workflow**

初回実行は「最新記事を基準に保存」するだけで通知しません。2 回目以降で新着があれば Discord に届きます。

## 監視サイトの追加

`sites.yaml` に追記して commit してください。

```yaml
sites:
  - name: 表示名
    url: https://example.com/feed.xml
```

## ローカル実行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python scripts/check.py
```

## スケジュール

- 毎日 **09:00 JST**（00:00 UTC）
- 変更する場合は `.github/workflows/check.yml` の `cron` を編集
