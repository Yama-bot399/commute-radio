#!/usr/bin/env python3
"""通勤ラジオ (commute-radio)

興味のあるジャンルのニュースと天気を集め、Gemini で台本にし、edge-tts で
2人の掛け合い（または1人）の音声にして、自分専用ポッドキャストとして書き出します。
GitHub Actions から毎朝実行する想定です（.github/workflows/daily.yml）。

使い方
  python commute_radio.py --site-dir site          通常実行（「作成する曜日」に従う）
  python commute_radio.py --site-dir site --force  曜日に関係なく作成
  python commute_radio.py --dry-run                収集と台本作成だけ（音声化・保存はしない）

環境変数
  GEMINI_API_KEY  Google AI Studio のAPIキー（無いときは見出しを読み上げる簡易版になります）
  SITE_BASE_URL   公開URL（省略時は GitHub Actions の情報から https://<owner>.github.io/<repo> を組み立て）
  FORCE_RUN       "1" で --force と同じ（手動実行時にワークフローが設定します）
"""
from __future__ import annotations

import argparse
import asyncio
import calendar
import copy
import datetime as dt
import difflib
import html
import json
import os
import re
import shutil
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

JST = dt.timezone(dt.timedelta(hours=9), "JST")
HERE = Path(__file__).resolve().parent
USER_AGENT = "Mozilla/5.0 (compatible; commute-radio/1.0)"
DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_JA = "月火水木金土日"
CHARS_PER_MINUTE = {"gemini": 330, "edge": 300}  # 1分に話す文字数の初期値（実際の回の長さから毎回補正する）
VOICE_SAMPLE_TEXT = "おはようございます。通勤ラジオです。この声で、毎朝のニュースをお届けします。今日も安全運転でいってらっしゃい。"
VOICE_SAMPLES_PER_RUN = 0  # 番組作成のついでに作る「選んでいない声」のサンプル数（番組用の無料枠を守るため0）
LINE_GAP_MS = 300  # 発言と発言のあいだの無音
LEAD_IN_MS = 700  # 冒頭の無音（カーオーディオで頭が切れるのを防ぐ）
TAIL_MS = 1200
FALLBACK_MODEL = "gemini-flash-lite-latest"
PRIORITY_JA = {1: "低", 2: "中", 3: "高"}

DEFAULT_HOSTS = [
    {"name": "ナナミ", "voice": "ja-JP-NanamiNeural", "gemini_voice": "Aoede", "role": "進行役"},
    {"name": "ケイタ", "voice": "ja-JP-KeitaNeural", "gemini_voice": "Charon", "role": "解説役"},
]
# Gemini TTS の声（名前はGoogleの公式一覧より）
GEMINI_VOICES = [
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede", "Callirrhoe", "Autonoe",
    "Enceladus", "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia",
    "Achernar", "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi", "Vindemiatrix",
    "Sadachbia", "Sadaltager", "Sulafat",
]
BGM_CHOICES = ["none", "morning", "lofi", "ambient", "news", "custom"]
BGM_GAIN = {"small": 0.45, "medium": 0.7, "large": 1.0}  # 声の後ろで流すときのBGMの大きさ
GEMINI_TTS_FALLBACKS = ["gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]
TTS_CHUNK_CHARS = 2400  # 1回の音声合成に渡す台本の文字数の目安（約7分）。無料枠は1日10回ほどなので回数を減らす

DEFAULT_CONFIG: dict = {
    "program": {
        "title": "通勤ラジオ",
        "description": "毎朝届く、自分専用のニュース番組",
        "style": "dialogue",  # dialogue = 2人の掛け合い / single = 1人
        "target_minutes": 10,
        "max_topics": 8,
        "speech_rate": "+10%",
        "loudness": -14,  # 音量の目安（LUFS）。-16=標準 / -14=大きめ / -12=とても大きい
        "hosts": DEFAULT_HOSTS,
        "interest_note": "",
        "days": ["mon", "tue", "wed", "thu", "fri"],
        "keep_episodes": 7,
        "lookback_hours": 36,
        "bgm": "morning",  # none / morning / lofi / ambient / news / custom（bgm/custom.mp3）
        "bgm_volume": "medium",  # small / medium / large
    },
    "genres": [],
    "weather": {
        "enabled": True,
        "place": "仙台市",
        "latitude": 38.2682,
        "longitude": 140.8694,
        "go_hours": [7, 9],
        "back_hours": [17, 20],
    },
    "gemini": {"model": "gemini-flash-latest", "use_search": True, "temperature": 0.7},
    # engine: "gemini" = 自然な声（Gemini TTS）/ "edge" = 軽い機械音声（Microsoft Edge）
    "tts": {"engine": "gemini", "model": "gemini-3.1-flash-tts-preview", "direction": ""},
    "site_base_url": "",
}

WARNINGS: list[str] = []


# ---------------------------------------------------------------- 共通
def log(msg: str) -> None:
    print(f"[{dt.datetime.now(JST):%H:%M:%S}] {msg}", flush=True)


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    log("⚠ " + msg)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def num(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def to_list(v) -> list[str]:
    if isinstance(v, str):
        v = re.split(r"[\n,、]", v)
    if not isinstance(v, list):
        return []
    return [str(s).strip() for s in v if str(s).strip()]


def deep_merge(base, over):
    if isinstance(base, dict) and isinstance(over, dict):
        out = {k: (deep_merge(base[k], over[k]) if k in over else copy.deepcopy(base[k])) for k in base}
        for k in over:
            if k not in base:
                out[k] = copy.deepcopy(over[k])
        return out
    return copy.deepcopy(over)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def http_get(url: str, timeout: int = 20, retries: int = 2) -> bytes:
    last: Exception | None = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.5"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries:
                time.sleep(2 * (i + 1))
    raise last  # type: ignore[misc]


TAG_RE = re.compile(r"<[^>]+>")
URL_RE = re.compile(r"https?://\S+")


def clean_text(s: str) -> str:
    s = html.unescape(TAG_RE.sub(" ", s or ""))
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_title(s: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", s).lower())


def is_similar(a: str, b: str, th: float = 0.72) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 12 and short in long_:
        return True
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sm.real_quick_ratio() >= th and sm.quick_ratio() >= th and sm.ratio() >= th


def jp_date(d: dt.datetime) -> str:
    return f"{d.month}月{d.day}日（{WEEKDAY_JA[d.weekday()]}）"


# ---------------------------------------------------------------- 設定
def load_config(path: Path) -> dict:
    user = read_json(path, None)
    if user is None:
        if path.exists():
            raise SystemExit(f"設定ファイル {path} が読めません（JSONの書式を確認してください）")
        warn(f"設定ファイル {path} が無いので初期設定で動かします")
        user = {}
    cfg = deep_merge(DEFAULT_CONFIG, user)
    p = cfg["program"]
    p["style"] = "single" if p.get("style") == "single" else "dialogue"
    p["target_minutes"] = clamp(num(p.get("target_minutes"), 10), 2, 40)
    p["max_topics"] = int(clamp(num(p.get("max_topics"), 8), 1, 20))
    p["keep_episodes"] = int(clamp(num(p.get("keep_episodes"), 7), 1, 60))
    p["lookback_hours"] = clamp(num(p.get("lookback_hours"), 36), 6, 24 * 7)
    if not re.fullmatch(r"[+-]\d{1,3}%", str(p.get("speech_rate", ""))):
        p["speech_rate"] = "+0%"
    p["loudness"] = clamp(num(p.get("loudness"), -14), -20, -10)
    hosts = [h for h in p.get("hosts") or [] if isinstance(h, dict)]
    for i in range(2):
        base = copy.deepcopy(DEFAULT_HOSTS[i])
        if i < len(hosts):
            base.update({k: str(v).strip() for k, v in hosts[i].items() if str(v).strip()})
        if i < len(hosts):
            hosts[i] = base
        else:
            hosts.append(base)
    p["hosts"] = hosts[:2]
    p["days"] = [d for d in to_list(p.get("days")) if d in DAY_KEYS]
    p["title"] = str(p.get("title") or "通勤ラジオ").strip()
    p["interest_note"] = str(p.get("interest_note") or "").strip()
    p["bgm"] = p.get("bgm") if p.get("bgm") in BGM_CHOICES else "none"
    p["bgm_volume"] = p.get("bgm_volume") if p.get("bgm_volume") in BGM_GAIN else "medium"

    genres = []
    for i, g in enumerate(cfg.get("genres") or []):
        if not isinstance(g, dict):
            continue
        genres.append(
            {
                "name": str(g.get("name") or f"ジャンル{i + 1}").strip(),
                "enabled": g.get("enabled", True) is not False,
                "priority": int(clamp(num(g.get("priority"), 2), 1, 3)),
                "keywords": to_list(g.get("keywords")),
                "feeds": to_list(g.get("feeds")),
                "exclude": to_list(g.get("exclude")),
                "note": str(g.get("note") or "").strip(),
                # true: 見出し（か概要）にキーワードが入っている記事だけ使う
                "strict": g.get("strict", True) is not False,
            }
        )
    cfg["genres"] = genres

    w = cfg["weather"]
    w["enabled"] = w.get("enabled", True) is not False
    for key, default in (("go_hours", [7, 9]), ("back_hours", [17, 20])):
        hrs = w.get(key)
        try:
            h0, h1 = int(hrs[0]), int(hrs[1])
        except Exception:  # noqa: BLE001
            h0, h1 = default
        h0, h1 = clamp(h0, 0, 23), clamp(h1, 1, 24)
        w[key] = [h0, h1 if h1 > h0 else h0 + 1]
    g = cfg["gemini"]
    g["model"] = str(g.get("model") or "gemini-flash-latest").strip()
    g["use_search"] = g.get("use_search", True) is not False
    g["temperature"] = clamp(num(g.get("temperature"), 0.7), 0.0, 2.0)
    t = cfg["tts"]
    t["engine"] = "edge" if t.get("engine") == "edge" else "gemini"
    t["model"] = str(t.get("model") or GEMINI_TTS_FALLBACKS[0]).strip()
    t["direction"] = str(t.get("direction") or "").strip()
    for i, h in enumerate(p["hosts"]):
        if h.get("gemini_voice") not in GEMINI_VOICES:
            h["gemini_voice"] = DEFAULT_HOSTS[i]["gemini_voice"]
    return cfg


# ---------------------------------------------------------------- ニュース収集
def _terms(keyword: str) -> list[str]:
    """キーワードを検索語に分ける（"…" はひとかたまり、-語 と OR は照合に使わない）"""
    terms = re.findall(r'"([^"]+)"|(\S+)', unicodedata.normalize("NFKC", keyword))
    out = []
    for quoted, word in terms:
        t = quoted or word
        if not t or t.upper() == "OR" or t.startswith("-"):
            continue
        out.append(t)
    return out


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).lower())


def matches_keyword(text: str, keyword: str) -> bool:
    """キーワードの語がすべて text に含まれるか（大文字小文字・全角半角・空白の違いは無視）"""
    t = _squash(text)
    terms = _terms(keyword)
    return bool(terms) and all(_squash(w) in t for w in terms)


def google_news_url(query: str, lookback_hours: float) -> str:
    days = max(1, int(-(-lookback_hours // 24)))
    if '"' not in query and " " not in query.strip():
        query = f'"{query.strip()}"'  # 1語だけのキーワードは完全一致で検索（あいまい検索で無関係な記事が混ざるのを防ぐ）
    q = f"{query} when:{days}d"
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "ja", "gl": "JP", "ceid": "JP:ja"}
    )


def parse_feed(data: bytes, is_google: bool) -> list[dict]:
    import feedparser

    fp = feedparser.parse(data)
    feed_title = clean_text(fp.feed.get("title", "")) if not is_google else ""
    items = []
    for e in fp.entries:
        title = clean_text(e.get("title", ""))
        if not title:
            continue
        source = ""
        src = e.get("source")
        if isinstance(src, dict):
            source = clean_text(src.get("title", ""))
        if is_google:
            if source and title.endswith(" - " + source):
                title = title[: -(len(source) + 3)].strip()
            elif " - " in title:
                head, tail = title.rsplit(" - ", 1)
                if len(tail) <= 25:
                    title, source = head.strip(), source or tail.strip()
            summary = ""
        else:
            source = source or feed_title
            summary = clean_text(e.get("summary", ""))
            if summary.startswith(title[:15]):
                summary = summary[len(title):].strip()
            summary = summary[:300]
        published = None
        for key in ("published_parsed", "updated_parsed"):
            t = e.get(key)
            if t:
                published = dt.datetime.fromtimestamp(calendar.timegm(t), dt.timezone.utc)
                break
        items.append(
            {"title": title, "source": source, "link": e.get("link", ""), "summary": summary, "published": published}
        )
    return items


def collect_news(cfg: dict, now: dt.datetime, recent_titles: list[str]) -> list[dict]:
    p = cfg["program"]
    since = now - dt.timedelta(hours=p["lookback_hours"])
    history_norms = [norm_title(t) for t in recent_titles]
    accepted: list[str] = []
    result: list[dict] = []
    enabled = [g for g in cfg["genres"] if g["enabled"]]
    if not enabled:
        warn("有効なジャンルがありません（設定画面でジャンルをONにしてください）")
    for gi, genre in enumerate(enabled):
        sources = [(google_news_url(k, p["lookback_hours"]), True, k) for k in genre["keywords"]]
        sources += [(u, False, None) for u in genre["feeds"]]
        if not sources:
            warn(f"「{genre['name']}」にキーワードもRSSもありません")
            continue
        raw: list[dict] = []
        dropped = 0
        for url, is_google, kw in sources:
            try:
                got = parse_feed(http_get(url), is_google)
            except Exception as e:  # noqa: BLE001
                warn(f"「{genre['name']}」の取得に失敗: {url[:80]} ({e})")
                continue
            got = [it for it in got if not it["published"] or it["published"] >= since]
            if genre["strict"]:
                # 見出し・概要にキーワードが無い記事は、ジャンルと無関係とみなして外す
                kws = [kw] if kw else genre["keywords"]
                if kws:
                    keep = [it for it in got if any(matches_keyword(it["title"] + " " + it["summary"], k) for k in kws)]
                    dropped += len(got) - len(keep)
                    got = keep
            got.sort(key=lambda it: it["published"] or since, reverse=True)
            raw += got[:30]
        raw.sort(key=lambda it: it["published"] or since, reverse=True)
        if dropped:
            log(f"「{genre['name']}」: キーワードを含まない{dropped}件を除外")
        fresh = []
        for it in raw:
            if any(w in it["title"] for w in genre["exclude"]):
                continue
            n = norm_title(it["title"])
            if len(n) < 6:
                continue
            if any(is_similar(n, h) for h in history_norms) or any(is_similar(n, a) for a in accepted):
                continue
            accepted.append(n)
            fresh.append(it)
        fresh = fresh[: 4 + 4 * genre["priority"]]
        for j, it in enumerate(fresh):
            it.update({"id": f"g{gi + 1}-{j + 1}", "genre": genre["name"], "genre_index": gi})
        log(f"「{genre['name']}」: {len(raw)}件取得 → 候補{len(fresh)}件")
        result += fresh
    return result


# ---------------------------------------------------------------- 天気（Open-Meteo）
WMO_JA = {
    0: "快晴", 1: "晴れ", 2: "晴れ時々くもり", 3: "くもり", 45: "霧", 48: "霧",
    51: "弱い霧雨", 53: "霧雨", 55: "強い霧雨", 56: "着氷性の霧雨", 57: "着氷性の霧雨",
    61: "弱い雨", 63: "雨", 65: "強い雨", 66: "着氷性の雨", 67: "着氷性の雨",
    71: "弱い雪", 73: "雪", 75: "強い雪", 77: "霧雪", 80: "にわか雨", 81: "にわか雨", 82: "激しいにわか雨",
    85: "にわか雪", 86: "強いにわか雪", 95: "雷雨", 96: "ひょうを伴う雷雨", 99: "ひょうを伴う激しい雷雨",
}
SNOW, ICE, FOG, THUNDER = {71, 73, 75, 77, 85, 86}, {56, 57, 66, 67}, {45, 48}, {95, 96, 99}


def _deg(t) -> str:
    n = round(float(t))
    return f"氷点下{-n}" if n < 0 else str(n)


def fetch_weather(w: dict, now: dt.datetime) -> dict | None:
    params = {
        "latitude": w["latitude"],
        "longitude": w["longitude"],
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "hourly": "precipitation_probability,weather_code,temperature_2m",
        "timezone": "Asia/Tokyo",
        "forecast_days": 2,
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    try:
        data = json.loads(http_get(url))
        return summarize_weather(data, w, now)
    except Exception as e:  # noqa: BLE001
        warn(f"天気の取得に失敗: {e}")
        return None


def summarize_weather(data: dict, w: dict, now: dt.datetime) -> dict:
    today = now.strftime("%Y-%m-%d")
    daily, hourly = data["daily"], data["hourly"]
    i = daily["time"].index(today) if today in daily["time"] else 0
    code = daily["weather_code"][i]
    code = int(code) if code is not None else None
    tmax, tmin = daily["temperature_2m_max"][i], daily["temperature_2m_min"][i]

    def window(h0: int, h1: int):
        probs, codes, temps = [], set(), []
        for t, pr, c, tt in zip(
            hourly["time"], hourly["precipitation_probability"], hourly["weather_code"], hourly["temperature_2m"]
        ):
            if t.startswith(today) and h0 <= int(t[11:13]) < h1:
                if pr is not None:
                    probs.append(pr)
                if c is not None:
                    codes.add(int(c))
                if tt is not None:
                    temps.append(tt)
        return (max(probs) if probs else None), codes, (min(temps) if temps else None)

    parts = [f"{w['place']}の今日の天気は{WMO_JA.get(code, 'はっきりしない空模様') if code is not None else 'はっきりしない空模様'}"]
    if tmax is not None and tmin is not None:
        parts.append(f"最高気温{_deg(tmax)}度、最低気温{_deg(tmin)}度")
    probs, cautions = [], []
    for label, (h0, h1) in (("行き", w["go_hours"]), ("帰り", w["back_hours"])):
        pr, codes, tlow = window(h0, h1)
        if pr is not None:
            probs.append(f"{label}の時間帯（{h0}時から{h1}時）が{pr}パーセント")
        if codes & SNOW:
            cautions.append(f"{label}の時間帯に雪の予報")
        if codes & ICE:
            cautions.append(f"{label}の時間帯に路面凍結のおそれ")
        if codes & FOG:
            cautions.append(f"{label}の時間帯に霧の予報")
        if codes & THUNDER:
            cautions.append(f"{label}の時間帯に雷雨の予報")
        if tlow is not None and tlow <= 1 and not codes & SNOW:
            cautions.append(f"{label}の時間帯は{_deg(tlow)}度前後で路面の凍結に注意")
    if probs:
        parts.append("降水確率は" + "、".join(probs))
    text = "。".join(parts) + "。"
    if cautions:
        text += "運転の注意: " + "、".join(cautions) + "。"
    return {"text": text, "code": code, "cautions": cautions}


# ---------------------------------------------------------------- Gemini
def make_client():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        warn("GEMINI_API_KEY が未設定のため、AIを使わない簡易版（見出しの読み上げ）で作成します")
        return None
    try:
        from google import genai

        return genai.Client(api_key=key)
    except Exception as e:  # noqa: BLE001
        warn(f"Gemini の準備に失敗: {e}")
        return None


_GEMINI_DISABLED: list[str] = []  # APIキーが無効と分かったら以降は呼ばない


def ask_gemini(client, cfg: dict, prompt: str, *, json_mode: bool = False, use_search: bool = False) -> str:
    from google.genai import types

    if _GEMINI_DISABLED:
        raise RuntimeError(_GEMINI_DISABLED[0])
    g = cfg["gemini"]
    models = [g["model"]] + ([FALLBACK_MODEL] if g["model"] != FALLBACK_MODEL else [])
    last: Exception | None = None
    for model in models:
        for search in ([True, False] if use_search else [False]):
            for attempt in range(2):
                try:
                    kw: dict = {"temperature": g["temperature"]}
                    if json_mode:
                        kw["response_mime_type"] = "application/json"
                    if search:
                        kw["tools"] = [types.Tool(google_search=types.GoogleSearch())]
                    resp = client.models.generate_content(
                        model=model, contents=prompt, config=types.GenerateContentConfig(**kw)
                    )
                    text = (resp.text or "").strip()
                    if not text:
                        raise RuntimeError("空の応答でした")
                    if model != g["model"]:
                        warn(f"{g['model']} が使えなかったため {model} で作成しました")
                    return text
                except Exception as e:  # noqa: BLE001
                    last = e
                    msg = str(e)
                    log(f"Gemini エラー（{model}{'・検索あり' if search else ''}・{attempt + 1}回目）: {msg[:300]}")
                    if "API key not valid" in msg or "API_KEY_INVALID" in msg:
                        _GEMINI_DISABLED.append("GeminiのAPIキーが無効です（GitHubのSecretsの GEMINI_API_KEY を確認）")
                        raise RuntimeError(_GEMINI_DISABLED[0]) from e
                    if any(s in msg for s in ("INVALID_ARGUMENT", "NOT_FOUND", "PERMISSION_DENIED", " 400", " 403", " 404")):
                        break  # この組み合わせは再試行しても無駄なので次へ
                    if attempt == 0:
                        time.sleep(15)
    raise RuntimeError(f"Gemini を呼び出せませんでした: {last}")


def parse_json_loose(text: str):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def _fmt_time(d: dt.datetime | None) -> str:
    return d.astimezone(JST).strftime("%m/%d %H:%M") if d else "日時不明"


def genre_lines(cfg: dict) -> str:
    rows = []
    for g in cfg["genres"]:
        if g["enabled"]:
            note = f" / 方針: {g['note']}" if g["note"] else ""
            rows.append(f"- {g['name']}（重要度: {PRIORITY_JA[g['priority']]}{note}）")
    return "\n".join(rows) or "（なし）"


def select_topics(client, cfg: dict, items: list[dict]) -> tuple[list[dict], bool]:
    n = cfg["program"]["max_topics"]
    if not items:
        return [], False
    if client is not None:
        cands = "\n".join(
            f"{it['id']} | {it['genre']} | {_fmt_time(it['published'])} | {it['source'] or '-'} | {it['title']}"
            + (f" | {it['summary'][:120]}" if it["summary"] else "")
            for it in items
        )
        prompt = f"""あなたは朝の通勤ラジオ番組のディレクターです。
次のニュース候補から、リスナーが朝の運転中に聞きたい話題を最大{n}本選んでください。

# リスナーの希望
{cfg['program']['interest_note'] or '（特になし）'}

# ジャンルと方針
{genre_lines(cfg)}

# 選び方
- 重要度「高」のジャンルを多めに、「低」は少なめに。候補があるジャンルからは原則1本以上選ぶ
- 候補は「ジャンル」の話題として集めたものだが、検索の都合で無関係な記事も混ざる。見出しがそのジャンルの話題だとはっきり分かるものだけ選び、少しでも怪しいものは選ばない（例: ジャンル「ベガルタ仙台」に将棋や他チームだけの話題）。合う候補が無いジャンルからは選ばなくてよい
- 同じ出来事を扱う候補は1本だけ選ぶ
- 新しく、具体的な事実（結果・数字・発表内容）があるものを優先する
- 広告・まとめ記事・中身のない見出しは選ばない

# 候補（id | ジャンル | 日時 | 媒体 | 見出し | 概要）
{cands}

# 出力
次の形のJSONだけを出力してください。
{{"topics": [{{"id": "候補のid", "point": "この話題で伝えたいポイントを1文で"}}]}}"""
        try:
            data = parse_json_loose(ask_gemini(client, cfg, prompt, json_mode=True))
            by_id = {it["id"]: it for it in items}
            chosen, seen = [], set()
            for t in data.get("topics", []):
                it = by_id.get(str(t.get("id", "")).strip())
                if it and it["id"] not in seen:
                    seen.add(it["id"])
                    chosen.append({**it, "point": str(t.get("point", "")).strip()})
            if chosen:
                chosen = chosen[:n]
                chosen.sort(key=lambda it: it["genre_index"])  # 設定のジャンル順に並べる
                return chosen, True
            warn("AIの話題選びが空だったため、新しい順に選びました")
        except Exception as e:  # noqa: BLE001
            warn(f"AIの話題選びに失敗したため、新しい順に選びました: {e}")
    return fallback_select(cfg, items, n), False


def fallback_select(cfg: dict, items: list[dict], n: int) -> list[dict]:
    groups: dict[int, list[dict]] = {}
    for it in items:
        groups.setdefault(it["genre_index"], []).append(it)
    enabled = [g for g in cfg["genres"] if g["enabled"]]
    weights = {gi: enabled[gi]["priority"] for gi in groups}
    total = sum(weights.values()) or 1
    quota = {gi: max(1, round(n * w / total)) for gi, w in weights.items()}
    chosen = []
    for gi in sorted(groups):
        chosen += [{**it, "point": ""} for it in groups[gi][: quota[gi]]]
    return chosen[:n]


STAGE_RE = re.compile(r"[（(][^）)]{0,10}(笑|拍手|BGM|SE|ジングル|間|効果音)[^）)]{0,10}[）)]")
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")


def parse_script(text: str, style: str, names: tuple[str, str] = ("", "")) -> list[tuple[int, str]]:
    """「A: 〜」「B: 〜」（出演者名でも可）の行を (話者番号, 文) にする"""
    labels = ["A", "a", "Ａ", "B", "b", "Ｂ"] + [n for n in names if n]
    alt = "|".join(sorted((re.escape(s) for s in labels), key=len, reverse=True))
    speaker_re = re.compile(rf"^\s*[*\-・>\s]*\**\s*({alt})\s*\**\s*[:：]\s*(.+?)\s*$")
    lines = []
    for raw in text.splitlines():
        m = speaker_re.match(raw)
        if not m:
            continue
        who = m.group(1)
        spk = 0 if who in ("A", "a", "Ａ") or (names[0] and who == names[0]) else 1
        if style == "single":
            spk = 0
        t = m.group(2)
        t = URL_RE.sub("", t)
        t = STAGE_RE.sub("", t)
        t = EMOJI_RE.sub("", t)
        t = re.sub(r"\s{2,}", " ", t.replace("**", "").replace("#", "")).strip()
        if t:
            lines.append((spk, t))
    return lines


def estimate_cpm(state: dict, cfg: dict) -> float:
    """1分あたりに話す文字数。過去の回の「台本の文字数 ÷ 実際の長さ」から求める（話す速さの設定も考慮）"""
    engine = cfg["tts"]["engine"]
    factor = _rate_factor(cfg["program"]["speech_rate"])
    vals = []
    for e in state["episodes"][:8]:
        if e.get("engine") == engine and e.get("chars") and e.get("speech_minutes", 0) > 1:
            vals.append(e["chars"] / e["speech_minutes"] / e.get("rate_factor", 1.0))
        if len(vals) >= 3:
            break
    base = sum(vals) / len(vals) if vals else CHARS_PER_MINUTE[engine] / (1.0 if engine == "gemini" else 1.1)
    return clamp(base * factor, 200, 500)


def _budget_text(topics: list[dict], cfg: dict, body: int) -> str:
    weights = []
    for t in topics:
        g = next((g for g in cfg["genres"] if g["name"] == t["genre"]), None)
        weights.append({1: 0.7, 2: 1.0, 3: 1.5}[g["priority"] if g else 2])
    total = sum(weights) or 1
    per = [int(body * w / total) for w in weights]
    return "\n".join(f"- {t['title'][:40]}: 約{n}文字" for t, n in zip(topics, per))


def _expand_script(client, cfg: dict, lines: list, target: int, fmt: str, names: tuple) -> list:
    """台本が目標より短いときに、事実を増やさずに話を厚くして書き直してもらう"""
    p = cfg["program"]
    for _ in range(2):
        chars = sum(len(t) for _, t in lines)
        if chars >= target * 0.88:
            break
        label = lambda s: "A" if s == 0 else "B"
        current = "\n".join(f"{label(s)}: {t}" for s, t in lines)
        prompt = f"""次のラジオ台本は約{chars}文字で、目標の約{target}文字より短すぎます。約{target}文字（{round(p['target_minutes'])}分）になるように書き直してください。

# 膨らませ方
- 各話題で、背景・これまでの経緯・数字の意味・今後の予定・リスナーの暮らしや仕事への影響を足す（Google検索で確認できたことだけ。確認できないことは言わない）
- 2人の掛け合いを増やす（質問と答え、感想、身近な例への置き換え）。ただし同じことの繰り返しや水増しの相づちは入れない
- 話題の順番・オープニング・エンディングの流れは変えない。新しいニュースは足さない

# 出力
台本の本文だけ。{fmt}。見出し・ト書き・説明・空行は出力しない

# 今の台本
{current}"""
        try:
            text = ask_gemini(client, cfg, prompt, use_search=cfg["gemini"]["use_search"])
            new = parse_script(text, p["style"], names)
            if sum(len(t) for _, t in new) > chars:
                lines = new
                log(f"台本を膨らませました: {chars} → {sum(len(t) for _, t in lines)}文字")
            else:
                break
        except Exception as e:  # noqa: BLE001
            warn(f"台本を目標の長さまで膨らませられませんでした: {str(e)[:150]}")
            break
    return lines


def write_script(client, cfg: dict, topics: list[dict], weather: dict | None, now: dt.datetime,
                 cpm: float | None = None) -> tuple[list, bool]:
    p = cfg["program"]
    a, b = p["hosts"]
    if client is not None:
        cpm = cpm or CHARS_PER_MINUTE[cfg["tts"]["engine"]]
        target = int(p["target_minutes"] * cpm)
        if p["style"] == "dialogue":
            cast = (
                f"A: {a['name']}（{a.get('role') or '進行役'}。明るく話題を振る）\n"
                f"B: {b['name']}（{b.get('role') or '解説役'}。落ち着いて要点や背景を補足する）"
            )
            fmt = "各行は「A: 」または「B: 」で始める。2人の自然な掛け合いにし、相づちは入れすぎない"
        else:
            cast = f"A: {a['name']}（1人で進行するアナウンサー）"
            fmt = "各行は「A: 」で始める（1人で話す）。1行は1〜3文で区切る"
        blocks, current = [], None
        for t in topics:
            if t["genre"] != current:
                current = t["genre"]
                g = next((g for g in cfg["genres"] if g["name"] == current), None)
                pr = PRIORITY_JA[g["priority"]] if g else "中"
                note = f" / 方針: {g['note']}" if g and g["note"] else ""
                blocks.append(f"\n## {current}（重要度: {pr}{note}）")
            blocks.append(
                f"- 見出し: {t['title']}\n  媒体: {t['source'] or '不明'} / 日時: {_fmt_time(t['published'])}"
                + (f"\n  概要: {t['summary']}" if t["summary"] else "")
                + (f"\n  伝えるポイント: {t['point']}" if t.get("point") else "")
            )
        news = "\n".join(blocks) if blocks else "（今日はニュースを取得できませんでした。天気と、良い一日をという挨拶だけで短くまとめる）"
        search_rule = (
            "- 見出ししか無い話題は、Google検索で事実を確認して短く補足してよい（確認できない内容は言わない）\n"
            if cfg["gemini"]["use_search"]
            else ""
        )
        prompt = f"""あなたは人気ラジオ番組の構成作家です。車で通勤中のリスナーが聞く朝の番組「{p['title']}」の台本を書いてください。

# 放送日
{now.year}年{jp_date(now)}

# 出演者
{cast}

# 長さ（大事）
全体でおよそ{target}文字（約{round(p['target_minutes'])}分）。短すぎないように、各話題の目安の文字数を守る
- オープニングとエンディング: 合わせて約{min(600, target // 6)}文字
{_budget_text(topics, cfg, max(0, target - min(600, target // 6))) if topics else ''}
- 見出しだけで材料が足りない話題は、Google検索で確認した背景・経緯・今後の予定・数字の意味で厚くし、2人のやり取り（質問と答え、感想、身近な例）で深める

# 構成
1. オープニング: あいさつ、日付と曜日、今日の天気（下の天気情報を使う。通勤時間帯に雨・雪・霧・凍結の心配があれば運転の注意をひとこと）
2. ニュース: 下の順番どおりに伝える。ジャンルが変わるところで「続いては〇〇の話題です」と区切る。重要度「高」のジャンルは厚めに
3. エンディング: 今日の話題をひとことで振り返り、「運転気をつけて、いってらっしゃい」のように送り出す

# リスナーの希望
{p['interest_note'] or '（特になし）'}

# 守ること
- 話題は下のニュースだけ。分からないことを推測で補わない。下に無い別のニュースを検索で見つけて付け足さない（下の話題の背景を検索で確かめて補足するのはよい）
- 見出しの内容がそのジャンルと関係ないと分かったものは、その話題ごと飛ばす
{search_rule}- 記事の文章をそのまま読まず、話し言葉で短く言い換える。媒体名は「河北新報によると」のように必要なときだけ
- 耳で聞いて分かる文章にする。1回の発言は1〜3文、1文は短く。URL・記号・括弧・箇条書き・絵文字は使わない
- 数字は聞き取りやすく言う（例: 2-1 は「2対1」、10/26 は「10月26日」）
- 出力は台本の本文だけ。{fmt}。見出し・ト書き・説明・空行は出力しない

# 天気
{weather['text'] if weather else '（天気情報なし。天気には触れない）'}

# ニュース（放送順）
{news}"""
        try:
            text = ask_gemini(client, cfg, prompt, use_search=cfg["gemini"]["use_search"])
            lines = parse_script(text, p["style"], (a["name"], b["name"]))
            if len(lines) >= 3:
                log(f"台本: {len(lines)}行・{sum(len(t) for _, t in lines)}文字（目標 {target}文字・1分あたり{cpm:.0f}文字で計算）")
                if topics:
                    lines = _expand_script(client, cfg, lines, target, fmt, (a["name"], b["name"]))
                return lines, True
            warn("AIの台本が短すぎたため、簡易版で作成しました")
        except Exception as e:  # noqa: BLE001
            warn(f"AIの台本作成に失敗したため、簡易版で作成しました: {e}")
    return fallback_script(cfg, topics, weather, now, client is not None), False


def fallback_script(cfg: dict, topics: list[dict], weather: dict | None, now: dt.datetime, ai_tried: bool) -> list:
    p = cfg["program"]
    b = 1 if p["style"] == "dialogue" else 0
    lines = [(0, f"おはようございます。{jp_date(now)}の{p['title']}です。")]
    if weather:
        lines.append((b, weather["text"].replace("運転の注意: ", "運転に注意が必要です。")))
    reason = "AIによる要約がうまく動かなかったため" if ai_tried else "AIの設定がまだのため"
    if topics:
        lines.append((0, f"今日は{reason}、ニュースの見出しだけをお伝えします。"))
        current = None
        for t in topics:
            if t["genre"] != current:
                current = t["genre"]
                lines.append((0, f"続いては、{current}の話題です。"))
            src = f"{t['source']}の記事です。" if t["source"] else ""
            lines.append((b, f"{t['title']}。{src}"))
    else:
        lines.append((0, "今日はニュースを取得できませんでした。設定のキーワードを確認してみてください。"))
    lines.append((0, f"以上、{p['title']}でした。運転気をつけて、いってらっしゃい。"))
    return lines


# ---------------------------------------------------------------- 音声（edge-tts）と MP3 処理
_BITRATES = {
    True: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],  # MPEG-1 Layer III
    False: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],  # MPEG-2/2.5 Layer III
}
_SAMPLE_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def mp3_frame(b: bytes, i: int) -> dict | None:
    if i + 4 > len(b) or b[i] != 0xFF or (b[i + 1] & 0xE0) != 0xE0:
        return None
    h = int.from_bytes(b[i : i + 4], "big")
    ver, layer = (h >> 19) & 3, (h >> 17) & 3
    br_idx, sr_idx = (h >> 12) & 0xF, (h >> 10) & 3
    if ver == 1 or layer != 1 or br_idx in (0, 15) or sr_idx == 3:
        return None
    mpeg1 = ver == 3
    br, sr = _BITRATES[mpeg1][br_idx] * 1000, _SAMPLE_RATES[ver][sr_idx]
    mono = ((h >> 6) & 3) == 3
    return {
        "header": h,
        "mpeg1": mpeg1,
        "bitrate": br,
        "sr": sr,
        "samples": 1152 if mpeg1 else 576,
        "length": (144 if mpeg1 else 72) * br // sr + ((h >> 9) & 1),
        "side": (17 if mono else 32) if mpeg1 else (9 if mono else 17),
        "crc": 0 if (h >> 16) & 1 else 2,
    }


def clean_mp3(data: bytes) -> bytes:
    """ID3タグとXing/Info/VBRIヘッダーを取り除き、MPEGフレームだけを返す（連結用）"""
    i, end = 0, len(data)
    if data[:3] == b"ID3" and len(data) > 10:
        size = (data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | (data[9] & 0x7F)
        i = 10 + size + (10 if data[5] & 0x10 else 0)
    if end - i > 128 and data[end - 128 : end - 125] == b"TAG":
        end -= 128
    out, first = bytearray(), True
    while i < end:
        f = mp3_frame(data, i)
        if not f or i + f["length"] > end:
            i += 1
            continue
        frame = data[i : i + f["length"]]
        i += f["length"]
        if first:
            first = False
            off = 4 + f["crc"] + f["side"]
            if frame[off : off + 4] in (b"Xing", b"Info") or frame[36:40] == b"VBRI":
                continue
        out += frame
    return bytes(out)


def silent_frames(template: dict, ms: int) -> bytes:
    h = (template["header"] | (1 << 16)) & ~(1 << 9) & ~0b11  # CRCなし・パディングなし・強調なし
    length = (144 if template["mpeg1"] else 72) * template["bitrate"] // template["sr"]
    frame = h.to_bytes(4, "big") + bytes(length - 4)  # サイド情報もデータも0 → 無音
    n = max(1, round(ms / 1000 * template["sr"] / template["samples"]))
    return frame * n


def mp3_duration(data: bytes) -> float:
    i, total = 0, 0.0
    while i < len(data):
        f = mp3_frame(data, i)
        if not f:
            i += 1
            continue
        total += f["samples"] / f["sr"]
        i += f["length"]
    return total


def join_mp3(clips: list[bytes], gap_ms: int = LINE_GAP_MS) -> tuple[bytes, float]:
    out, template = bytearray(), None
    for c in clips:
        frames = clean_mp3(c)
        if not frames:
            continue
        if template is None:
            template = mp3_frame(frames, 0)
            out += silent_frames(template, LEAD_IN_MS)
        else:
            out += silent_frames(template, gap_ms)
        out += frames
    if template is None:
        raise RuntimeError("音声データがありません")
    out += silent_frames(template, TAIL_MS)
    return bytes(out), mp3_duration(bytes(out))


async def _tts_all(lines: list[tuple[int, str]], cfg: dict) -> list[bytes | None]:
    import edge_tts

    hosts, rate = cfg["program"]["hosts"], cfg["program"]["speech_rate"]
    sem = asyncio.Semaphore(4)

    async def one(idx: int, spk: int, text: str) -> bytes | None:
        err: Exception | None = None
        async with sem:
            for attempt in range(4):
                try:
                    comm = edge_tts.Communicate(text, hosts[spk]["voice"], rate=rate)
                    buf = bytearray()
                    async for chunk in comm.stream():
                        if chunk.get("type") == "audio" and chunk.get("data"):
                            buf.extend(chunk["data"])
                    if buf:
                        return bytes(buf)
                    raise RuntimeError("音声が空でした")
                except Exception as e:  # noqa: BLE001
                    err = e
                    await asyncio.sleep(2 * (attempt + 1))
        warn(f"{idx + 1}行目の音声化に失敗: {err}")
        return None

    return await asyncio.gather(*(one(i, s, t) for i, (s, t) in enumerate(lines)))


# ---- Gemini TTS（自然な声）
def ffmpeg_exe() -> str | None:
    """音声変換ツール ffmpeg の場所。入っていなければ pip の imageio-ffmpeg に同梱のものを使う（apt は使わない）"""
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


def _rate_factor(rate: str) -> float:
    try:
        return clamp(1 + int(rate.rstrip("%")) / 100, 0.8, 1.3)
    except ValueError:
        return 1.0


def _pcm_from_response(data: bytes) -> bytes:
    """Gemini の音声データ（WAV か 生のPCM）から 24kHz・モノラル・16bit のPCMを取り出す"""
    if data[:4] == b"RIFF":
        i = 12
        while i + 8 <= len(data):
            cid, size = data[i:i + 4], int.from_bytes(data[i + 4:i + 8], "little")
            if cid == b"data":
                return data[i + 8:i + 8 + size]
            i += 8 + size + (size & 1)
        raise RuntimeError("WAVの中に音声データがありません")
    return data


def _chunks(lines: list[tuple[int, str]], limit: int = TTS_CHUNK_CHARS) -> list[list[tuple[int, str]]]:
    out, cur, n = [], [], 0
    for ln in lines:
        if cur and n + len(ln[1]) > limit:
            out.append(cur)
            cur, n = [], 0
        cur.append(ln)
        n += len(ln[1])
    if cur:
        out.append(cur)
    return out


def _direction(cfg: dict) -> str:
    p, t = cfg["program"], cfg["tts"]
    a, b = p["hosts"]
    if p["style"] == "dialogue":
        who = (f"Host1（{a['name']}）は{a.get('role') or '進行役'}で、明るく親しみやすい話し方。"
               f"Host2（{b['name']}）は{b.get('role') or '解説役'}で、落ち着いた聞き取りやすい話し方。"
               "2人は気心の知れた仲で、相手の話に自然に反応しながら会話する。")
    else:
        who = f"Host1（{a['name']}）は1人で番組を進める、明るく聞き取りやすいパーソナリティ。"
    extra = f"演出の希望: {t['direction']}\n" if t["direction"] else ""
    return ("朝の通勤時間に車で聞く、日本語のラジオ番組です。"
            "原稿を読み上げる調子ではなく、生放送のラジオのように自然な抑揚と間で話してください。"
            f"{who}\n{extra}以下の台本を、そのとおりに話してください。\n\n")


def _gemini_tts_chunk(client, cfg: dict, chunk: list[tuple[int, str]], model: str) -> bytes:
    from google.genai import types

    p = cfg["program"]
    hosts = p["hosts"]
    if p["style"] == "dialogue":
        script = "\n".join(f"Host{spk + 1}: {text}" for spk, text in chunk)
        speech = types.SpeechConfig(multi_speaker_voice_config=types.MultiSpeakerVoiceConfig(
            speaker_voice_configs=[
                types.SpeakerVoiceConfig(speaker=f"Host{i + 1}", voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=hosts[i]["gemini_voice"])))
                for i in range(2)]))
    else:
        script = "\n".join(text for _, text in chunk)
        speech = types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=hosts[0]["gemini_voice"])))
    resp = client.models.generate_content(
        model=model, contents=_direction(cfg) + script,
        config=types.GenerateContentConfig(response_modalities=["AUDIO"], speech_config=speech))
    for part in resp.candidates[0].content.parts:
        if part.inline_data and part.inline_data.data:
            return _pcm_from_response(part.inline_data.data)
    raise RuntimeError("音声が返ってきませんでした")


def synthesize_gemini(client, lines: list[tuple[int, str]], cfg: dict) -> tuple[bytes, float]:
    chunks = _chunks(lines)
    models = [cfg["tts"]["model"]] + [m for m in GEMINI_TTS_FALLBACKS if m != cfg["tts"]["model"]]
    log(f"音声化（Gemini・自然な声）: {len(lines)}行を{len(chunks)}回に分けて作成")
    pcm = bytearray()
    silence = lambda ms: bytes(int(24000 * ms / 1000) * 2)  # 24kHz・16bit の無音
    pcm += silence(LEAD_IN_MS)
    for ci, chunk in enumerate(chunks):
        last: Exception | None = None
        for model in list(models):
            for attempt in range(3):
                try:
                    pcm += _gemini_tts_chunk(client, cfg, chunk, model)
                    pcm += silence(500)
                    last = None
                    break
                except Exception as e:  # noqa: BLE001
                    last = e
                    msg = str(e)
                    log(f"Gemini TTS エラー（{model}・{ci + 1}/{len(chunks)}・{attempt + 1}回目）: {msg[:300]}")
                    if any(s in msg for s in ("NOT_FOUND", "INVALID_ARGUMENT", " 404", " 400", "PERMISSION_DENIED")):
                        break  # このモデルは使えない → 次のモデルへ
                    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                        if "PerDay" in msg or "per day" in msg.lower() or "_per_day" in msg:
                            warn(f"{model} の1日の無料回数を使い切ったため、別のモデルで続けます")
                            break  # 1日の上限 → 待っても無駄なので、別モデル（別の無料枠）へ
                        time.sleep(65)  # 1分あたりの上限 → 1分待ってやり直す
                    else:
                        time.sleep(8)
            if last is None:
                if model != models[0]:
                    models.remove(model)
                    models.insert(0, model)  # 使えたモデルを次から先に使う
                break
            if model in models and len(models) > 1:
                models.remove(model)
        if last is not None:
            raise RuntimeError(f"Gemini で音声を作れませんでした: {last}")
        if ci + 1 < len(chunks):
            time.sleep(25)  # 無料枠は1分に数回までなので間をあける
    cfg["_tts_model_used"] = models[0]
    pcm += silence(TAIL_MS)
    factor = _rate_factor(cfg["program"]["speech_rate"])
    import subprocess

    af = ["-af", f"atempo={factor:.2f}"] if abs(factor - 1) > 0.01 else []
    mp3 = subprocess.run(
        [ffmpeg_exe(), "-v", "error", "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "pipe:0", *af,
         "-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", "pipe:1"],
        input=bytes(pcm), capture_output=True, check=True).stdout
    frames = clean_mp3(mp3)
    return frames, mp3_duration(frames)


def make_voice_samples(client, cfg: dict, site: Path, limit: int = VOICE_SAMPLES_PER_RUN,
                       only: list[str] | None = None) -> None:
    """設定画面の「試聴」用に、まだ無い声のサンプルを少しずつ作って voices/ に置く"""
    import subprocess

    vdir = site / "voices"
    vdir.mkdir(exist_ok=True)
    have = {f.stem for f in vdir.glob("*.mp3")}
    exe = ffmpeg_exe()

    def save(name: str, pcm_or_mp3: bytes, raw_pcm: bool) -> None:
        src = ["-f", "s16le", "-ar", "24000", "-ac", "1"] if raw_pcm else ["-f", "mp3"]
        out = subprocess.run([exe, "-v", "error", *src, "-i", "pipe:0", "-af", "loudnorm=I=-16:TP=-1.5",
                              "-ar", "24000", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", "pipe:1"],
                             input=pcm_or_mp3, capture_output=True, check=True).stdout
        (vdir / f"{name}.mp3").write_bytes(out)
        have.add(name)

    if exe:
        for v in ("ja-JP-NanamiNeural", "ja-JP-KeitaNeural"):  # 機械音声のサンプル（無料・回数制限なし）
            if v not in have:
                try:
                    async def _edge(voice=v):
                        import edge_tts

                        buf = bytearray()
                        async for ch in edge_tts.Communicate(VOICE_SAMPLE_TEXT, voice).stream():
                            if ch.get("type") == "audio" and ch.get("data"):
                                buf.extend(ch["data"])
                        return bytes(buf)

                    save(v, asyncio.run(_edge()), raw_pcm=False)
                except Exception as e:  # noqa: BLE001
                    log(f"声のサンプル（{v}）を作れませんでした: {str(e)[:120]}")
    made = 0
    if client is not None and exe:
        chosen = [h["gemini_voice"] for h in cfg["program"]["hosts"]]
        first = cfg.get("_tts_model_used") or cfg["tts"]["model"]
        models = [first] + [m for m in [cfg["tts"]["model"], *GEMINI_TTS_FALLBACKS] if m != first]
        models = list(dict.fromkeys(models))
        if only is not None:  # 設定画面の「見本を作る」から頼まれた声だけ（作り直しも含む）
            order, limit = [v for v in only if v in GEMINI_VOICES], len(only)
            have -= set(order)
        else:  # 番組作成のついで: 選んでいる2つの声 + limit 個
            order = chosen + [v for v in GEMINI_VOICES if v not in chosen]
            limit = limit + sum(1 for v in chosen if v not in have)
        for v in order:
            if v in have:
                continue
            if made >= limit:
                break
            c = copy.deepcopy(cfg)
            c["program"]["style"] = "single"
            c["program"]["hosts"][0]["gemini_voice"] = v
            c["tts"]["direction"] = ""
            pcm = None
            for model in list(models):
                try:
                    pcm = _gemini_tts_chunk(client, c, [(0, VOICE_SAMPLE_TEXT)], model)
                    break
                except Exception as e:  # noqa: BLE001
                    msg = str(e)
                    log(f"声のサンプル（{v}・{model}）を作れませんでした: {msg[:160]}")
                    if len(models) > 1:
                        models.remove(model)  # 1日の上限や使えないモデル → 別のモデル（別の無料枠）で
            if pcm is None:
                warn(f"声の見本（{v}）を作れませんでした。無料枠は毎日回復するので、時間をおいて試してください")
                break
            save(v, pcm, raw_pcm=True)
            made += 1
            time.sleep(20)  # 無料枠の回数制限に引っかかりにくくする
    prev = read_json(vdir / "index.json", {})
    write_json(vdir / "index.json", {"voices": sorted(have), "handled_at": prev.get("handled_at", "")})
    rest = len([v for v in GEMINI_VOICES if v not in have])
    log(f"声のサンプル: 今回{made}個作成（自然な声は残り{rest}個）")


def post_process(mp3: bytes, cfg: dict) -> tuple[bytes, float]:
    """音量をそろえ（ラウドネス正規化）、選んだBGMを声の後ろに小さく敷く"""
    import subprocess
    import tempfile

    if ffmpeg_exe() is None:
        warn("ffmpeg が見つからないため、音量の調整とBGMを省きました")
        return mp3, mp3_duration(mp3)
    p = cfg["program"]
    bgm_file = HERE / "bgm" / f"{p['bgm']}.mp3"
    use_bgm = p["bgm"] != "none"
    if use_bgm and not bgm_file.exists():
        warn(f"BGMのファイル bgm/{p['bgm']}.mp3 が見つからないため、BGMなしで作りました")
        use_bgm = False
    with tempfile.TemporaryDirectory() as td:
        src, out = Path(td) / "voice.mp3", Path(td) / "out.mp3"
        src.write_bytes(mp3)
        # 声を軽く圧縮して小さい所を持ち上げ、番組全体を設定の音量（loudness）にそろえる
        voice = ("[0:a]aresample=24000,acompressor=threshold=-24dB:ratio=3:attack=5:release=120:makeup=2,"
                 f"loudnorm=I={p['loudness']}:TP=-1.5:LRA=7,aresample=24000")
        if use_bgm:
            g = BGM_GAIN[p["bgm_volume"]]
            # 冒頭2.5秒はBGMだけ → 声が入ると自動で小さく（ダッキング）→ 最後はフェードアウト
            fc = (f"{voice},adelay=2500:all=1,apad=pad_dur=3,asplit=2[v][sc];"
                  f"[1:a]aresample=24000,volume={g},afade=t=in:d=1.5[b];"
                  "[b][sc]sidechaincompress=threshold=0.03:ratio=4:attack=40:release=900:makeup=1[bd];"
                  "[v][bd]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.9[mix]")
            args = ["-i", str(src), "-stream_loop", "-1", "-i", str(bgm_file)]
        else:
            fc = f"{voice},alimiter=limit=0.9[mix]"
            args = ["-i", str(src)]
        subprocess.run([ffmpeg_exe(), "-y", "-v", "error", *args, "-filter_complex", fc, "-map", "[mix]",
                        "-ac", "1", "-ar", "24000", "-c:a", "libmp3lame", "-b:a", "64k", str(out)], check=True)
        data = out.read_bytes()
        if use_bgm:  # 最後の3秒をフェードアウト
            dur = mp3_duration(clean_mp3(data))
            out2 = Path(td) / "out2.mp3"
            subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-i", str(out), "-af", f"afade=t=out:st={max(0, dur - 3):.2f}:d=3",
                            "-c:a", "libmp3lame", "-b:a", "64k", str(out2)], check=True)
            data = out2.read_bytes()
    frames = clean_mp3(data)
    log(f"音量を調整しました（目安 {p['loudness']} LUFS）{'・BGM: ' + p['bgm'] if use_bgm else ''}")
    return frames, mp3_duration(frames)


def synthesize(lines: list[tuple[int, str]], cfg: dict, client=None) -> tuple[bytes, float]:
    if cfg["tts"]["engine"] == "gemini":
        if client is None:
            warn("Gemini が使えないため、軽い機械音声（Edge）で作成しました")
        elif ffmpeg_exe() is None:
            warn("ffmpeg が見つからないため、軽い機械音声（Edge）で作成しました")
        else:
            try:
                cfg["_engine_used"] = "gemini"
                return synthesize_gemini(client, lines, cfg)
            except Exception as e:  # noqa: BLE001
                warn(f"自然な声（Gemini）で作れなかったため、軽い機械音声（Edge）で作成しました: {str(e)[:200]}")
    cfg["_engine_used"] = "edge"
    log(f"音声化（Edge）: {len(lines)}行")
    clips = asyncio.run(_tts_all(lines, cfg))
    ok = [c for c in clips if c]
    if len(ok) < max(1, int(len(lines) * 0.7)):
        raise RuntimeError(f"音声化に失敗した行が多すぎます（成功 {len(ok)}/{len(lines)}）")
    return join_mp3(ok)


def write_notes_page(site: Path, cfg: dict, ep: dict, lines: list, now: dt.datetime) -> None:
    """NotebookLM などに読み込ませる「今日のまとめ」ページ（today.html）を書く"""
    p = cfg["program"]
    rows = []
    for t in ep["topics"]:
        rows.append(f"<li><strong>【{html.escape(t['genre'])}】{html.escape(t['title'])}</strong>"
                    + (f"（{html.escape(t['source'])}）" if t.get("source") else "")
                    + f"<br><a href=\"{html.escape(t['link'])}\">{html.escape(t['link'])}</a></li>")
    script = "".join(f"<p>{html.escape(p['hosts'][s_]['name'])}: {html.escape(t)}</p>" for s_, t in lines)
    doc = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><title>{html.escape(p['title'])} 今日のまとめ {now:%Y-%m-%d}</title>
<style>body{{font-family:sans-serif;max-width:720px;margin:0 auto;padding:16px;line-height:1.8}}li{{margin-bottom:12px}}a{{word-break:break-all}}</style></head>
<body><h1>{html.escape(p['title'])} 今日のまとめ（{jp_date(now)}）</h1>
<p>このページは、今朝の番組で取り上げたニュースのまとめです。</p>
<h2>天気</h2><p>{html.escape(ep.get('weather') or '（なし）')}</p>
<h2>ニュース</h2><ul>{''.join(rows) or '<li>（なし）</li>'}</ul>
<h2>番組の台本</h2>{script}
</body></html>"""
    (site / "today.html").write_text(doc, encoding="utf-8")


# ---------------------------------------------------------------- サイト（ポッドキャストfeed）
def resolve_base_url(arg: str | None, cfg: dict) -> str:
    url = arg or os.environ.get("SITE_BASE_URL") or cfg.get("site_base_url") or ""
    if not url:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if "/" in repo:
            owner, name = repo.split("/", 1)
            host = f"{owner.lower()}.github.io"
            url = f"https://{host}" if name.lower() == host else f"https://{host}/{name}"
    if not url:
        url = "http://localhost:8000"
        log("公開URLが分からないため http://localhost:8000 として出力します（--base-url で指定可）")
    return url.rstrip("/")


def x(s) -> str:
    return _xml_escape(str(s), {'"': "&quot;"})


def episode_html(ep: dict) -> str:
    rows = []
    if ep.get("weather"):
        rows.append(f"<p>{html.escape(ep['weather'])}</p>")
    if ep.get("topics"):
        lis = "".join(
            f"<li>【{html.escape(t['genre'])}】<a href=\"{html.escape(t['link'])}\">{html.escape(t['title'])}</a>"
            + (f"（{html.escape(t['source'])}）" if t.get("source") else "")
            + "</li>"
            for t in ep["topics"]
        )
        rows.append(f"<ul>{lis}</ul>")
    if ep.get("mode") == "fallback":
        rows.append("<p>※AIを使わない簡易版（見出しの読み上げ）です。</p>")
    rows.append("<p>天気データ: Open-Meteo.com（CC BY 4.0）／音声: Microsoft Edge 読み上げ</p>")
    return "".join(rows)


def build_feed(cfg: dict, episodes: list[dict], base_url: str, now: dt.datetime, has_cover: bool) -> str:
    p = cfg["program"]
    items = []
    for ep in episodes:
        pub = dt.datetime.fromisoformat(ep["date"])
        items.append(
            f"""    <item>
      <title>{x(ep['title'])}</title>
      <description>{x(episode_html(ep))}</description>
      <enclosure url="{x(base_url + '/' + ep['file'])}" length="{int(ep['bytes'])}" type="audio/mpeg"/>
      <guid isPermaLink="false">commute-radio-{x(ep['id'])}</guid>
      <pubDate>{format_datetime(pub)}</pubDate>
      <itunes:duration>{int(ep['duration'])}</itunes:duration>
      <itunes:episodeType>full</itunes:episodeType>
    </item>"""
        )
    cover = (
        f'    <itunes:image href="{x(base_url)}/cover.png"/>\n'
        f"    <image><url>{x(base_url)}/cover.png</url><title>{x(p['title'])}</title><link>{x(base_url)}/</link></image>\n"
        if has_cover
        else ""
    )
    body = "\n".join(items)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{x(p['title'])}</title>
    <link>{x(base_url)}/</link>
    <atom:link href="{x(base_url)}/feed.xml" rel="self" type="application/rss+xml"/>
    <description>{x(p['description'] or p['title'])}</description>
    <language>ja</language>
    <lastBuildDate>{format_datetime(now)}</lastBuildDate>
    <itunes:author>{x(p['title'])}</itunes:author>
    <itunes:explicit>false</itunes:explicit>
    <itunes:block>yes</itunes:block>
    <itunes:category text="News"/>
{cover}{body}
  </channel>
</rss>
"""


def copy_web_assets(site: Path) -> None:
    web = HERE / "web"
    if web.is_dir():
        for f in web.iterdir():
            if f.is_file():
                shutil.copy2(f, site / f.name)
    bgm = HERE / "bgm"
    if bgm.is_dir():  # 設定画面でBGMを試聴できるように公開する
        (site / "bgm").mkdir(exist_ok=True)
        for f in bgm.glob("*.mp3"):
            shutil.copy2(f, site / "bgm" / f.name)
    (site / ".nojekyll").write_text("", encoding="utf-8")


def load_state(site: Path) -> dict:
    ep = read_json(site / "episodes.json", {})
    hist = read_json(site / "history.json", {})
    return {
        "episodes": ep.get("episodes", []) if isinstance(ep, dict) else [],
        "last_run": ep.get("last_run") if isinstance(ep, dict) else None,
        "history": hist.get("days", {}) if isinstance(hist, dict) else {},
    }


def recent_titles(history: dict, now: dt.datetime, days: int = 3) -> list[str]:
    today = now.date()
    out = []
    for day, titles in history.items():
        try:
            d = dt.date.fromisoformat(day)
        except ValueError:
            continue
        if 0 < (today - d).days <= days:  # 当日分は含めない（作り直したときに同じ話題を使えるように）
            out += titles
    return out


def write_site(site: Path, cfg: dict, state: dict, base_url: str, now: dt.datetime) -> None:
    keep = cfg["program"]["keep_episodes"]
    episodes = sorted(state["episodes"], key=lambda e: e["date"], reverse=True)[:keep]
    ep_dir = site / "episodes"
    ep_dir.mkdir(exist_ok=True)
    keep_names = {Path(e["file"]).name for e in episodes} | {Path(e.get("script", "")).name for e in episodes}
    for f in ep_dir.iterdir():
        if f.is_file() and f.name not in keep_names:
            f.unlink()
    today = now.date()
    history = {
        d: t for d, t in state["history"].items() if _days_ago(d, today) is not None and _days_ago(d, today) <= 4
    }
    write_json(site / "history.json", {"days": history})
    write_json(
        site / "episodes.json",
        {
            "title": cfg["program"]["title"],
            "description": cfg["program"]["description"],
            "feed": base_url + "/feed.xml",
            "updated": now.isoformat(timespec="seconds"),
            "last_run": state.get("last_run"),
            "episodes": episodes,
        },
    )
    has_cover = (site / "cover.png").exists()
    (site / "feed.xml").write_text(build_feed(cfg, episodes, base_url, now, has_cover), encoding="utf-8")


def _days_ago(day: str, today: dt.date) -> int | None:
    try:
        return (today - dt.date.fromisoformat(day)).days
    except ValueError:
        return None


def write_step_summary(md: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(md + "\n")


# ---------------------------------------------------------------- メイン
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="通勤ラジオ: 興味のあるニュースを毎朝ポッドキャストにする")
    ap.add_argument("--config", default=str(HERE / "config.json"), help="設定ファイル（既定: config.json）")
    ap.add_argument("--site-dir", default="site", help="出力先フォルダ（GitHub Pages で公開する中身）")
    ap.add_argument("--base-url", default=None, help="公開URL（例: https://name.github.io/commute-radio）")
    ap.add_argument("--force", action="store_true", help="作成する曜日の設定を無視して作成する")
    ap.add_argument("--dry-run", action="store_true", help="収集と台本作成だけ行い、音声化・保存はしない")
    ap.add_argument("--voice-sample", default=os.environ.get("VOICE_SAMPLE", ""),
                    help="番組は作らず、指定した声（カンマ区切り）の試聴用サンプルだけ作る")
    args = ap.parse_args(argv)

    cfg = load_config(Path(args.config))
    now = dt.datetime.now(JST)
    site = Path(args.site_dir)
    site.mkdir(parents=True, exist_ok=True)
    base_url = resolve_base_url(args.base_url, cfg)
    state = load_state(site)
    force = args.force or os.environ.get("FORCE_RUN") == "1"
    p = cfg["program"]

    if not args.dry_run:
        copy_web_assets(site)

    # 試聴サンプルの依頼: 引数／環境変数、または設定画面が config.json に書いた依頼（まだ処理していないもの）
    req = cfg["tts"].get("sample_request") or {}
    vindex = read_json(site / "voices" / "index.json", {})
    pending = isinstance(req, dict) and req.get("voice") and str(req.get("at", "")) > str(vindex.get("handled_at", ""))
    if args.voice_sample.strip() or pending:
        voices = [v.strip() for v in (args.voice_sample or req.get("voice", "")).split(",") if v.strip()]
        log(f"声のサンプルを作ります（番組は作りません）: {', '.join(voices)}")
        if not args.dry_run:
            make_voice_samples(make_client(), cfg, site, only=voices)
            if pending:
                vi = read_json(site / "voices" / "index.json", {})
                vi["handled_at"] = str(req.get("at", ""))
                write_json(site / "voices" / "index.json", vi)
            write_site(site, cfg, state, base_url, now)
        return 0

    if not force and DAY_KEYS[now.weekday()] not in p["days"]:
        log(f"今日（{WEEKDAY_JA[now.weekday()]}曜日）は番組を作らない設定です")
        if not args.dry_run:
            state["last_run"] = {"time": now.isoformat(timespec="seconds"), "result": "skipped",
                                 "message": "作成しない曜日のためお休み"}
            write_site(site, cfg, state, base_url, now)
        return 0

    log(f"{jp_date(now)}の番組を作ります（{'2人の掛け合い' if p['style'] == 'dialogue' else '1人'}・約{round(p['target_minutes'])}分）")
    items = collect_news(cfg, now, recent_titles(state["history"], now))
    weather = fetch_weather(cfg["weather"], now) if cfg["weather"]["enabled"] else None
    if weather:
        log("天気: " + weather["text"])
    if not items and not weather and not args.dry_run:
        raise SystemExit("ニュースも天気も取得できませんでした。ネットワークか設定を確認してください。")

    client = make_client()
    topics, select_ai = select_topics(client, cfg, items)
    cpm = estimate_cpm(state, cfg)
    lines, script_ai = write_script(client, cfg, topics, weather, now, cpm)
    mode = "ai" if script_ai else "fallback"

    if args.dry_run:
        print("\n===== 選んだ話題 =====")
        for t in topics:
            print(f"[{t['genre']}] {t['title']}（{t['source']}）")
        print("\n===== 台本 =====")
        for spk, t in lines:
            print(f"{p['hosts'][spk]['name']}: {t}")
        return 0

    audio, duration = synthesize(lines, cfg, client)
    try:
        audio, duration = post_process(audio, cfg)
    except Exception as e:  # noqa: BLE001
        warn(f"音量の調整・BGMの追加に失敗したため、元の音声のまま公開しました: {str(e)[:200]}")
    ep_id = now.strftime("%Y%m%d-%H%M")
    ids = {e["id"] for e in state["episodes"]}
    k = 2
    while ep_id in ids:
        ep_id = now.strftime("%Y%m%d-%H%M") + f"-{k}"
        k += 1
    (site / "episodes").mkdir(exist_ok=True)
    mp3_name, txt_name = f"episodes/{ep_id}.mp3", f"episodes/{ep_id}.txt"
    (site / mp3_name).write_bytes(audio)
    (site / txt_name).write_text(
        "\n".join(f"{p['hosts'][s]['name']}: {t}" for s, t in lines) + "\n", encoding="utf-8"
    )
    same_day = any(e["date"][:10] == now.date().isoformat() for e in state["episodes"])
    title = f"{jp_date(now)}の{p['title']}" + (f"（{now:%H:%M}版）" if same_day else "")
    episode = {
        "id": ep_id,
        "title": title,
        "date": now.isoformat(timespec="seconds"),
        "file": mp3_name,
        "script": txt_name,
        "bytes": len(audio),
        "duration": round(duration, 1),
        "mode": mode,
        # 次回の長さの見積もりに使う（台本の文字数と、話している部分の長さ）
        "chars": sum(len(t) for _, t in lines),
        "engine": cfg.get("_engine_used", cfg["tts"]["engine"]),
        "rate_factor": _rate_factor(p["speech_rate"]),
        "speech_minutes": round(max(0.1, duration - (8 if p["bgm"] != "none" else 3)) / 60, 2),
        "weather": weather["text"] if weather else "",
        "topics": [{"genre": t["genre"], "title": t["title"], "source": t["source"], "link": t["link"]} for t in topics],
    }
    state["episodes"].insert(0, episode)
    day = now.date().isoformat()
    state["history"].setdefault(day, [])
    state["history"][day] += [t["title"] for t in topics]
    minutes = f"{int(duration // 60)}分{int(duration % 60):02d}秒"
    state["last_run"] = {
        "time": now.isoformat(timespec="seconds"),
        "result": "ok" if mode == "ai" else "fallback",
        "message": f"{minutes}、話題{len(topics)}本（候補{len(items)}件から選択）",
        "warnings": WARNINGS[-10:],
    }
    try:
        write_notes_page(site, cfg, episode, lines, now)
    except Exception as e:  # noqa: BLE001
        warn(f"今日のまとめページを作れませんでした: {e}")
    if cfg.get("_engine_used") == "gemini" and client is not None:
        try:  # 選んでいる声の試聴用サンプルがまだ無ければ作る（最大2回）
            make_voice_samples(client, cfg, site)
        except Exception as e:  # noqa: BLE001
            log(f"声のサンプル作成をスキップ: {e}")
    write_site(site, cfg, state, base_url, now)
    log(f"完成: {mp3_name}（{minutes}・{len(audio) / 1e6:.1f}MB）")

    md = [f"## {title}", f"- 長さ: {minutes} / 話題 {len(topics)}本（候補 {len(items)}件）/ {'AI台本' if mode == 'ai' else '簡易版'}"]
    md += [f"- 【{t['genre']}】{t['title']}" for t in topics]
    if WARNINGS:
        md += ["", "### 注意"] + [f"- {w}" for w in WARNINGS]
    write_step_summary("\n".join(md))
    return 0


if __name__ == "__main__":
    sys.exit(main())
