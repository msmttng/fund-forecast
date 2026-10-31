# 基準価額予報 (fund-forecast)

主要インデックス投資信託（12グループ・63本）の翌営業日基準価額を、公示仲値（MURC TTM）・市場指数（JPX TOPIX / MSCI / Solactive / Yahoo Finance）・先物（大阪取引所 / CME / COMEX）および投資信託協会の基準価額データから試算・公開するサイトです。

- 公開ページ: https://msmttng.github.io/fund-forecast/
- 更新頻度: GitHub Actions の常駐ループにより 5 分ごとに自動更新（休場時間帯は 30 分間隔、`data` ブランチ配信）

## 構成

- `funds.json`: 対象ファンド定義（ISIN・協会コード・連動指数・倍率・ヘッジ有無・計算方式）
- `scripts/build_data.py`: 市場データ・基準価額CSVを取得し `docs/data.json` および答え合わせログ `docs/history/forecast_log.json` を更新
- `scripts/loop_update.py`: GitHub Actions 上で 5 分おきに最新 `data.json` を生成し、履歴なしの `data` ブランチへ配信
- `scripts/backtest.py`: 直近 60 営業日の過去データによる予想精度バックテストスクリプト
- `docs/index.html`: フロントエンド画面（単一HTML、60秒自動再読込、ライト／ダーク対応）
- `.github/workflows/update.yml`: データ自動更新ワークフロー
