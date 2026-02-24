# 気象神社 セットアップガイド

## ファイル構成
```
tenkura-jinja/
├── app.py          # メインサーバ
├── .env            # APIキー（Gitに上げない）
├── .gitignore
├── requirements.txt
├── start.bat       # Windows起動スクリプト
└── README.md
```

## 初回セットアップ

### 1. 起動
`start.bat` をダブルクリック（初回はライブラリインストールで少し時間かかる）

### 2. ローカル確認
http://localhost:5000 にアクセスして神社が表示されればOK

---

## Cloudflare Tunnel（外部公開・固定URL）

### インストール
1. https://github.com/cloudflare/cloudflared/releases から
   `cloudflared-windows-amd64.exe` をダウンロード
2. `C:\cloudflared\cloudflared.exe` に置く

### 固定URLで公開（無料・アカウント不要）
```
C:\cloudflared\cloudflared.exe tunnel --url http://localhost:5000
```
→ `https://xxxx-xxxx-xxxx.trycloudflare.com` というURLが発行される

### 注意
- 無料の trycloudflare.com はPC再起動でURLが変わる
- URLを固定したい場合はCloudflareアカウント登録してNamed Tunnelを使う（無料）

---

## Windows自動起動設定（タスクスケジューラ）

PCの電源を入れたら自動で神社を起動する設定：

1. `自動起動.bat` を作成：
```bat
@echo off
cd /d C:\Users\[ユーザー名]\tenkura-jinja
call venv\Scripts\activate.bat
start /min python app.py
```

2. タスクスケジューラ → 新しいタスク作成
   - トリガー: ログオン時
   - 操作: `自動起動.bat` を実行
   - 「最上位の特権で実行」にチェック

---

## 更新について

- 気象データは1時間ごとに自動更新
- ブラウザで `/refresh` にアクセスすると手動更新
- Geminiが毎回神託を生成するのでおみくじは更新のたびに変わる

## APIクレジット消費目安

- meteoblue: 1回のリクエストで約200クレジット消費
  - 1時間更新 × 24時間 = 4,800クレジット/日
  - 10,000,000クレジット残り → 約2,000日分（5年以上）
- Gemini 2.0 Flash: 1回約0.01円以下
  - 24回/日 ≈ 0.24円/日
