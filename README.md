# 基準価額予報 (fund-forecast)

主要インデックス投資信託（12グループ・63本）の翌営業日基準価額を、公示仲値（MURC TTM）・市場指数（JPX TOPIX / MSCI / Solactive / Yahoo Finance）・先物（大阪取引所 / CME / COMEX）および投資信託協会の基準価額データから試算・公開するサイトです。

- 公開ページ: https://msmttng.github.io/fund-forecast/
- トリプルNASDAQ専用 購入ジャッジ: https://msmttng.github.io/fund-forecast/triple.html
- トリプルNASDAQ 注文記録: https://msmttng.github.io/fund-forecast/triple_orders.html
- 更新頻度: GitHub Actions の常駐ループにより 5 分ごとに自動更新（休場時間帯は 30 分間隔、`data` ブランチ配信）

## 構成

- `funds.json`: 対象ファンド定義（ISIN・協会コード・連動指数・倍率・ヘッジ有無・計算方式）
- `portfolio.json`: トリプルNASDAQのリバランス共通設定（平常基準比率・モード・許容バンド・最低売買額）
- `scripts/build_data.py`: 市場データ・基準価額CSVを取得し `docs/data.json` および答え合わせログ `docs/history/forecast_log.json` を更新
- `scripts/triple_advisor.py`: `auAMトリプル・NASDAQ100 為替ヘッジ無し` 専用の購入指示指数（0〜100点）・推奨目標比率 `docs/triple_data.json` を生成し、GAS（Google スプレッドシート）の注文記録と連携して約定確定処理（`settle`）および Discord Webhook 通知を送信
- `scripts/loop_update.py`: GitHub Actions 上で 5 分おきに最新 `data.json` / `triple_data.json` を生成し、履歴なしの `data` ブランチへ配信
- `scripts/backtest.py`: 直近 60 営業日の過去データによる予想精度バックテストスクリプト
- `docs/index.html`: 全ファンド一覧フロントエンド画面（単一HTML、60秒自動再読込、ライト／ダーク対応）
- `docs/triple.html`: トリプルNASDAQ専用 リバランス＆購入ジャッジ画面（GAS 注文記録から保有口数・現金・約定待ちを自動反映）
- `docs/triple_orders.html`: トリプルNASDAQ 注文記録画面（買付・売却注文の登録・取消・編集・残高と平均取得単価の自動計算）
- `.github/workflows/update.yml`: データ自動更新・Discord通知ワークフロー（Repository Secrets の `DISCORD_WEBHOOK_URL`・`ORDERS_URL`・`ORDERS_KEY` を使用）
