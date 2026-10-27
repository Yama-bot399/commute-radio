# 通勤ラジオ

興味のあるジャンルのニュースと天気を、毎朝「2人の掛け合いラジオ」（MP3）にして、自分専用のポッドキャストとして届けるツールです。
GitHub Actions が毎朝自動で番組を作り、Android の AntennaPod が自動でダウンロードします。車では Bluetooth や Android Auto で聞くだけです。

```
毎朝5時ごろ（GitHub Actions・PC不要）
  ① ジャンルごとのキーワードで Googleニュース（と好きなRSS）から記事を集める
  ② Open-Meteo から天気を取得（通勤時間帯の降水確率、雪・霧・凍結の注意）
  ③ Gemini API が話題を選び、ラジオの台本を書く
  ④ edge-tts で2人の声の音声（MP3）にする
  ⑤ GitHub Pages に自分専用ポッドキャストとして公開
        ↓
  AntennaPod（Android）が自動ダウンロード → 車で再生
```

## 費用

| もの | 費用 |
|---|---|
| GitHub（公開リポジトリ・Actions・Pages） | 無料 |
| Gemini API | 無料枠で動きます（1回の作成で2回ほど呼び出し） |
| Gemini の音声合成（自然な声） | 無料枠で動きます（1回の番組で3回ほど呼び出し） |
| edge-tts（予備の読み上げ音声）、Open-Meteo（天気） | 無料・キー不要 |
| AntennaPod | 無料 |

Gemini の無料枠では、送った内容が Google のサービス改善に使われることがあります。送るのは公開ニュースの見出しと設定内容だけです。

---

## セットアップ（初回だけ・30分ほど）

PC のブラウザで進めるのがおすすめです。

### 1. Gemini の APIキーを作る

1. [Google AI Studio の APIキー画面](https://aistudio.google.com/apikey) を開き、Google アカウントでログイン
2. 「APIキーを作成」を押し、表示されたキーをコピーしておく

### 2. GitHub にリポジトリを作り、ファイルを入れる

1. GitHub にログインし、右上の「＋」→「New repository」
2. Repository name を決める（例: `commute-radio`）
3. **Public** を選んで「Create repository」（無料で GitHub Pages を使うため）
4. 表示された画面の「uploading an existing file」を押し、このフォルダの中身を**すべて**ドラッグ＆ドロップ →「Commit changes」
   - `.github` フォルダ（中に `workflows/daily.yml`）も必ず入れてください
   - うまく入らないときは「Add file」→「Create new file」で、名前に `.github/workflows/daily.yml` と入力し、中身を貼り付けて保存

### 3. APIキーを登録する

リポジトリの「Settings」→「Secrets and variables」→「Actions」→「New repository secret」

- Name: `GEMINI_API_KEY`
- Secret: 手順1でコピーしたキー

### 4. 1回目を手動で作る

「Actions」タブ → 左の「通勤ラジオを作成」→「Run workflow」→ 緑の「Run workflow」。
3〜5分で緑のチェックが付けば成功です。

### 5. GitHub Pages を有効にする

「Settings」→「Pages」→ Source を「Deploy from a branch」、Branch を `gh-pages` と `/ (root)` にして「Save」。
1〜2分後に `https://<ユーザー名>.github.io/<リポジトリ名>/` で番組ページが開きます（スマホのホーム画面に追加しておくと便利です）。

### 6. スマホの AntennaPod に登録する

1. Google Play から「AntennaPod」をインストール（Android Auto で使うなら Google Play 版を選んでください）
2. スマホで番組ページを開き「AntennaPodで登録」をタップ
   （または AntennaPod の「ポッドキャストを追加」→「RSSアドレスで追加」にフィードのURLを入力）
3. AntennaPod の設定で、ダウンロードの「自動ダウンロード」をオンにし、ポッドキャストの更新間隔を「1時間ごと」など短めにしておく

これで、平日の朝になると新しい回がスマホに届きます。

### 7. スマホから設定を変えられるようにする（おすすめ）

番組ページの「設定」タブで、ジャンルの追加・ON/OFF・キーワード・長さなどを変えられます。保存には GitHub のトークンが必要です。スマホの Chrome で GitHub にログインしてから進めてください。

1. 番組ページの「設定」タブを開き、「GitHubでトークンを作る」を押す（名前・有効期限1年・権限が入力済みの作成画面が開きます）
2. Repository access: 「Only select repositories」→ このリポジトリだけを選ぶ
3. Permissions に Contents と Actions が **Read and write** で入っていることを確認
4. 「Generate token」→ 表示された `github_pat_…` をコピー
5. 設定タブの「トークンを貼り付ける」欄に貼り付けて「つないで設定を読み込む」

ボタンが使えないときは、[Fine-grained token の作成画面](https://github.com/settings/personal-access-tokens/new) で同じ内容（Contents と Actions を Read and write）を手で選んでください。トークンはそのスマホのブラウザにだけ保存されます。期限が切れたら作り直して貼り直してください。

---

## 毎日の使い方

- 作成する曜日（初期値は平日）の朝5時ごろに新しい回が作られ、AntennaPod が自動でダウンロードします
- 車では AntennaPod を Bluetooth か Android Auto で再生
- 内容を変えたいときは番組ページの「設定」→「保存」（次の回から反映）。「保存して作成」ならすぐ作り直します
- 番組ページでは、各回の話題の元記事リンクと台本も見られます

## 設定の項目（config.json）

設定タブで変えられる内容は、すべて `config.json` に保存されています。GitHub 上で直接編集してもかまいません。

| 項目 | 内容 |
|---|---|
| `genres[].name` | ジャンル名（番組内で「続いては〇〇の話題です」と読まれます） |
| `genres[].keywords` | Googleニュースで検索する言葉。スペース区切りはすべての語を含む記事 |
| `genres[].feeds` | 追加で読む RSS の URL |
| `genres[].exclude` | 見出しにこの言葉があれば外す |
| `genres[].priority` | 重要度 1〜3（高いほど話題が多く、詳しくなる） |
| `genres[].note` | そのジャンルへの要望（例: 試合結果は詳しく） |
| `genres[].enabled` | false にすると放送しない |
| `genres[].strict` | true（既定）なら、見出しか概要にキーワードが入った記事だけ使う。無関係な記事が混ざるのを防ぐ |
| `program.style` | `dialogue`（2人の掛け合い）／`single`（1人） |
| `program.target_minutes` | 番組の長さの目安（分） |
| `program.max_topics` | 1回の話題の最大数 |
| `program.speech_rate` | 話す速さ（`-10%`〜`+20%` など） |
| `program.days` | 作成する曜日（`mon`〜`sun`） |
| `program.loudness` | 番組の音量（-16=標準 / -14=大きめ / -12=とても大きい） |
| `program.bgm` | BGM（`none` / `morning` 朝のさわやか / `lofi` ローファイ / `ambient` アンビエント / `news` ニュース風 / `custom` = bgm/custom.mp3 に置いた自分の曲） |
| `program.bgm_volume` | BGMの大きさ（`small` / `medium` / `large`）。話している間は自動で小さくなります |
| `program.interest_note` | 話題の選び方・話し方の希望（自由文） |
| `program.hosts` | 出演者の名前と声（`ja-JP-NanamiNeural` 女性／`ja-JP-KeitaNeural` 男性） |
| `tts.engine` | `gemini`（自然なAI音声・既定）／`edge`（軽い機械音声）。Gemini が使えない日は自動で edge になります |
| `program.hosts[].gemini_voice` | 自然な声のときの声（Aoede、Charon など Gemini の声の名前） |
| `tts.direction` | 自然な声の話し方の演出（例: ゆったり落ち着いて） |
| `program.keep_episodes` | 残しておく回数 |
| `program.lookback_hours` | 何時間前までのニュースを対象にするか |
| `weather` | 地点名・緯度・経度、行き／帰りの時間帯（`[7, 9]` は7時〜9時） |
| `gemini.model` | 使う Gemini のモデル（通常は `gemini-flash-latest`） |
| `gemini.use_search` | 見出ししか無い話題を Google 検索で補足するか |

## 作成時刻を変えたい

`.github/workflows/daily.yml` の `cron: "7 20 * * *"` を編集します。UTC（日本時間 − 9時間）で書きます。

- 朝 6:00 に作る → `"0 21 * * *"`
- 朝 4:30 に作る → `"30 19 * * *"`

GitHub が混んでいると数分〜数十分遅れることがあるので、出発の1時間以上前にしておくと安心です。

## うまくいかないとき

| 症状 | 確認すること |
|---|---|
| Actions に赤い × が付く | その実行を開いてログを確認（失敗すると GitHub からメールも届きます） |
| 番組が「簡易版」（見出しの読み上げだけ）になる | `GEMINI_API_KEY` が未登録・入力ミス、または無料枠の上限。Secrets を確認 |
| 番組ページが 404 | 手順5の Pages 設定（初回の実行が終わってから設定） |
| AntennaPod に新しい回が来ない | AntennaPod で番組を開き、下に引っ張って更新。自動ダウンロードの設定も確認 |
| 決まった時間に動かなくなった | 「Actions」タブで止まっていないか確認し、止まっていたら「Enable workflow」 |
| 設定タブで保存できない | トークンの対象リポジトリと、Contents／Actions が Read and write になっているか |

## 注意

- リポジトリと番組ページは公開されます（URL を知っていれば誰でも開けます）。設定に個人情報は書かず、番組の URL は人に広めないでください（個人で聞くための仕組みです）。
- 記事本文は取得せず、見出しと公開情報をもとに AI が要約します。要約には誤りが混じることがあるので、大事な情報は番組ページのリンクから元の記事を確認してください。
- 天気データ: [Open-Meteo.com](https://open-meteo.com/)（CC BY 4.0）／音声: Microsoft Edge の読み上げ（edge-tts）

## PC で試す（任意）

```bash
pip install -r requirements.txt
# Windows PowerShell:  $env:GEMINI_API_KEY="取得したキー"
# Mac / Linux:         export GEMINI_API_KEY="取得したキー"
python commute_radio.py --dry-run                     # 集めた話題と台本を表示するだけ
python commute_radio.py --site-dir site --force --base-url http://localhost:8000
python -m http.server 8000 --directory site           # http://localhost:8000 で番組ページを確認
```

## BGMについて

bgm フォルダの4曲は、tools/make_bgm.py でこのツール用に一から合成したオリジナル曲です。好きな曲を使いたいときは、利用が許可された曲（フリーBGMなど）を custom.mp3 という名前で bgm フォルダに入れ、設定で「自分の曲」を選びます。
