# 基準価額予報 (fund-forecast)

主要インデックス投資信託の翌営業日基準価額を、市場指数・先物・為替レート（Yahoo Finance）および投資信託協会の基準価額データから試算・公開するサイトです。

- 公開ページ: https://msmttng.github.io/fund-forecast/
- 更新頻度: GitHub Actions により 15 分ごとに自動更新

## 構成

- `funds.json`: 対象ファンド定義（ISIN・協会コード・連動指数・倍率・ヘッジ有無・計算方式）
- `scripts/build_data.py`: 市場データ・基準価額CSVを取得し `docs/data.json` を生成
- `docs/index.html`: フロントエンド画面（単一HTML、ライト／ダーク対応）
- `.github/workflows/update.yml`: 15分ごとのデータ自動更新ワークフロー
