import datetime
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from email.utils import format_datetime
from pathlib import Path
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3

DEFAULT_MODEL = "gemini-2.5-flash"
VOICE = "ja-JP-NanamiNeural"
MAX_HISTORY = 30
PRIVATE_BGM_DIR = Path(os.environ.get("PRIVATE_BGM_DIR", "/tmp/private-bgm"))
GENERAL_BGM_NAME = "bgmusic-general.mp3"
RARE_BGM_NAME = "bgmusic-rare.mp3"
BGM_VOLUME = 0.12


def require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return val


def sanitize_token(token: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", token)
    if len(cleaned) < 16:
        print("RADIO_SECRET is too short or invalid.", file=sys.stderr)
        sys.exit(1)
    return cleaned


def call_gemini(api_key: str, model: str, prompt: str) -> dict:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.8,
        },
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    last_err = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            text = body["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except urllib.error.HTTPError as exc:
            print(f"Gemini HTTPError: {exc.code} {exc.reason}", file=sys.stderr)
            last_err = exc
            time.sleep(2 * attempt)
        except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError, TimeoutError) as exc:
            last_err = exc
            time.sleep(2 * attempt)

    raise RuntimeError(f"Gemini API failed after retries: {type(last_err).__name__}")


def clean_for_speech(script: str) -> str:
    text = re.sub(r"\[[^\]]*\]|\（[^）]*\）", "", script)
    text = re.sub(r"[#*_`]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def synth_mp3(text: str, mp3_path: Path) -> None:
    last_err = None
    for attempt in range(1, 4):
        try:
            subprocess.run(
                [
                    "edge-tts",
                    "--voice",
                    VOICE,
                    "--text",
                    text,
                    "--write-media",
                    str(mp3_path),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
            )
            if mp3_path.exists() and mp3_path.stat().st_size > 1024:
                return
        except (subprocess.SubprocessError, TimeoutError) as exc:
            last_err = exc
            time.sleep(3 * attempt)

    raise RuntimeError(f"edge-tts failed after retries: {type(last_err).__name__}")


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> datetime.date:
    first = datetime.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7 + (nth - 1) * 7
    return first + datetime.timedelta(days=offset)


def _vernal_equinox_day(year: int) -> int:
    return int(20.8431 + 0.242194 * (year - 1980)) - ((year - 1980) // 4)


def _autumnal_equinox_day(year: int) -> int:
    return int(23.2488 + 0.242194 * (year - 1980)) - ((year - 1980) // 4)


def is_jp_holiday_or_weekend(d: datetime.date) -> bool:
    if d.weekday() >= 5:
        return True

    y = d.year
    holidays: set[datetime.date] = {
        datetime.date(y, 1, 1),
        _nth_weekday(y, 1, 0, 2),
        datetime.date(y, 2, 11),
        datetime.date(y, 2, 23),
        datetime.date(y, 3, _vernal_equinox_day(y)),
        datetime.date(y, 4, 29),
        datetime.date(y, 5, 3),
        datetime.date(y, 5, 4),
        datetime.date(y, 5, 5),
        _nth_weekday(y, 7, 0, 3),
        datetime.date(y, 8, 11),
        _nth_weekday(y, 9, 0, 3),
        datetime.date(y, 9, _autumnal_equinox_day(y)),
        _nth_weekday(y, 10, 0, 2),
        datetime.date(y, 11, 3),
        datetime.date(y, 11, 23),
    }

    for h in sorted(list(holidays)):
        if h.weekday() == 6:
            sub = h + datetime.timedelta(days=1)
            while sub in holidays:
                sub += datetime.timedelta(days=1)
            holidays.add(sub)

    return d in holidays


def select_bgm_path(jst_date: datetime.date) -> Path:
    if is_jp_holiday_or_weekend(jst_date):
        rare_path = PRIVATE_BGM_DIR / RARE_BGM_NAME
        if rare_path.exists():
            return rare_path
    general_path = PRIVATE_BGM_DIR / GENERAL_BGM_NAME
    if general_path.exists():
        return general_path
    return Path("assets/bgmusic.mp3")


def mix_with_bgm(voice_path: Path, output_path: Path, bgm_path: Path, volume: float) -> None:
    if not bgm_path.exists():
        print("BGM file not found; using voice-only output.")
        voice_path.replace(output_path)
        return

    filter_complex = (
        f"[1:a]volume={volume},afade=t=in:st=0:d=2[bgm];"
        f"[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=3[out]"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(voice_path),
            "-stream_loop",
            "-1",
            "-i",
            str(bgm_path),
            "-filter_complex",
            filter_complex,
            "-map",
            "[out]",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "4",
            str(output_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=180,
    )
    if voice_path.exists():
        voice_path.unlink()


def strip_tags(mp3_path: Path) -> int:
    try:
        audio = EasyID3(str(mp3_path))
        audio.delete()
        audio.save()
    except Exception:
        pass
    info = MP3(str(mp3_path))
    return int(round(info.info.length))


def build_prompt(jst_date: str, recent_topics: list[str], local_context: str) -> str:
    context_line = (
        f"リスナーの背景メモ（番組内で地名や個人情報を直接言及せず、視点の参考としてのみ使うこと）: {local_context}"
        if local_context
        else "リスナーの背景メモ: なし"
    )
    past_line = ", ".join(recent_topics) if recent_topics else "なし"

    return f"""
あなたは毎朝配信される個人向けラジオ番組「MATT RADIO」の落ち着いた女性DJです。
日付: {jst_date}
過去のテーマ一覧（重複を避けること）: {past_line}
{context_line}

以下の条件で、約5〜6分（日本語で約1,400〜1,800文字）のラジオ台本を作成してください。
- 挨拶、今日のテーマ紹介、知識・背景解説、独自の考察、締めの流れにする。
- 事実が不明な点や推測は断定せず、「〜とされています」「〜という見方もあります」と自然に表現する。
- 出力は必ず次のJSON形式のみとすること:
{{
  "title": "エピソードタイトル",
  "topic": "短いテーマ名（15文字以内）",
  "summary": "番組の短い紹介文（120文字以内）",
  "script": "読み上げ用本文"
}}
""".strip()


def update_rss(
    feed_path: Path,
    channel_title: str,
    feed_url: str,
    image_url: str,
    episode_title: str,
    summary: str,
    mp3_url: str,
    mp3_size: int,
    duration_sec: int,
    pub_dt: datetime.datetime,
) -> None:
    pub_rfc2822 = format_datetime(pub_dt)
    guid = html.escape(mp3_url)
    item_xml = f"""    <item>
      <title>{html.escape(episode_title)}</title>
      <description>{html.escape(summary)}</description>
      <enclosure url="{html.escape(mp3_url)}" length="{mp3_size}" type="audio/mpeg" />
      <guid isPermaLink="true">{guid}</guid>
      <pubDate>{pub_rfc2822}</pubDate>
      <itunes:duration>{duration_sec}</itunes:duration>
    </item>"""

    existing_items: list[str] = []
    if feed_path.exists():
        raw = feed_path.read_text(encoding="utf-8")
        existing_items = re.findall(r"<item>.*?</item>", raw, flags=re.DOTALL)

    filtered = [it for it in existing_items if guid not in it][: MAX_HISTORY - 1]
    all_items = [item_xml] + [f"    {it.strip()}" for it in filtered]

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{html.escape(channel_title)}</title>
    <link>{html.escape(feed_url)}</link>
    <description>Personal daily AI radio feed</description>
    <language>ja</language>
    <itunes:image href="{html.escape(image_url)}" />
    <lastBuildDate>{pub_rfc2822}</lastBuildDate>
{chr(10).join(all_items)}
  </channel>
</rss>
"""
    feed_path.write_text(rss, encoding="utf-8")


def main() -> None:
    api_key = require_env("GEMINI_API_KEY")
    secret = sanitize_token(require_env("RADIO_SECRET"))
    base_url = require_env("PAGES_BASE_URL").rstrip("/")
    local_context = os.environ.get("LOCAL_CONTEXT", "").strip()
    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL

    jst = datetime.timezone(datetime.timedelta(hours=9))
    now_jst = datetime.datetime.now(jst)
    date_str = now_jst.strftime("%Y-%m-%d")

    out_dir = Path("out") / secret
    episodes_dir = out_dir / "episodes"
    episodes_dir.mkdir(parents=True, exist_ok=True)

    history_file = Path("history.json")
    history: list[str] = []
    if history_file.exists():
        try:
            history = json.loads(history_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            history = []

    prompt = build_prompt(date_str, history[-20:], local_context)
    result = call_gemini(api_key, model, prompt)

    topic = str(result.get("topic") or f"{date_str}の話題").strip()
    episode_title = str(result.get("title") or f"{date_str} {topic}").strip()
    summary = str(result.get("summary") or topic).strip()
    raw_script = str(result.get("script") or "").strip()
    spoken_text = clean_for_speech(raw_script)

    if not spoken_text:
        raise RuntimeError("Generated script was empty.")

    mp3_name = f"{date_str.replace('-', '')}.mp3"
    voice_tmp_path = episodes_dir / f"tmp_{mp3_name}"
    mp3_path = episodes_dir / mp3_name

    synth_mp3(spoken_text, voice_tmp_path)
    bgm_path = select_bgm_path(now_jst.date())
    mix_with_bgm(voice_tmp_path, mp3_path, bgm_path, BGM_VOLUME)

    duration_sec = strip_tags(mp3_path)
    mp3_size = mp3_path.stat().st_size

    feed_path = out_dir / "feed.xml"
    feed_url = f"{base_url}/{secret}/feed.xml"
    image_url = f"{base_url}/{secret}/radiologo.jpg"
    mp3_url = f"{base_url}/{secret}/episodes/{mp3_name}"

    update_rss(
        feed_path=feed_path,
        channel_title="MATT RADIO",
        feed_url=feed_url,
        image_url=image_url,
        episode_title=episode_title,
        summary=summary,
        mp3_url=mp3_url,
        mp3_size=mp3_size,
        duration_sec=duration_sec,
        pub_dt=now_jst,
    )

    history.append(topic)
    history_file.write_text(
        json.dumps(history[-MAX_HISTORY:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Generated episode for {date_str} ({duration_sec}s, bgm={bgm_path.name})")


if __name__ == "__main__":
    main()
