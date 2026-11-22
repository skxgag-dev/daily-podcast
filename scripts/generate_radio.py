import os
import re
import glob
import asyncio
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
import xml.etree.ElementTree as ET
from xml.dom import minidom

from google import genai
from google.genai import types
import edge_tts
from mutagen.mp3 import MP3

# 定数定義
JST = timezone(timedelta(hours=9))
DEFAULT_MODEL = "gemini-2.5-flash"
VOICE_NAME = "ja-JP-NanamiNeural"
DIST_ROOT = "dist"
EPISODES_DIR_NAME = "episodes"
FEED_FILE_NAME = "feed.xml"
MAX_KEEP_EPISODES = 14
PODCAST_TITLE = "Daily AI Morning Radio"
PODCAST_DESCRIPTION = "毎朝自動生成されるパーソナルAIラジオ番組"
PODCAST_AUTHOR = "AI Radio Host"


def validate_secret(secret: str) -> str:
    if not secret or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", secret):
        raise ValueError("RADIO_SECRET の形式が不正です（16文字以上の半角英数字・ハイフン・アンダースコアのみ）。")
    return secret


def generate_script(api_key: str, model_name: str, local_context: str, now_jst: datetime) -> str:
    client = genai.Client(api_key=api_key)
    date_str = now_jst.strftime("%Y年%m月%d日（%a）")

    context_instruction = ""
    if local_context.strip():
        context_instruction = (
            f"\n【パーソナライズ用背景メモ（絶対に出力へ固有名詞・地名を含めないこと）】\n"
            f"{local_context.strip()}\n"
        )

    prompt = f"""
あなたは親しみやすく知的な朝のラジオパーソナリティです。
今日（{date_str}）の朝に聴く、約3〜4分（800〜1100文字程度）の日本語ラジオトーク台本を作成してください。

【構成ルール】
1. オープニングの挨拶と日付の案内
2. 今日の豆知識・雑学・思考のヒント（1〜2トピック、深くわかりやすく）
3. 今日も良い一日にするための短いクロージング

【厳守事項】
- 音声読み上げ（TTS）にそのまま渡すため、ト書き（「（笑）」「（BGM）」）、Markdown記号（`#`、`*`、`-`）、URLは一切書かないこと。
- 特定の都道府県名・市区町村名・個人名などのプライバシー情報は絶対に出力に含めないこと。
- 自然な話し言葉と句読点のみで構成すること。
{context_instruction}
"""

    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.7,
        ),
    )
    script_text = (response.text or "").strip()
    if not script_text:
        raise RuntimeError("Gemini API から台本テキストが返されませんでした。")
    return script_text


async def synthesize_speech(text: str, output_path: str, voice: str) -> None:
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(output_path)


def cleanup_old_episodes(episodes_dir: str, max_keep: int) -> None:
    mp3_files = sorted(glob.glob(os.path.join(episodes_dir, "*.mp3")))
    if len(mp3_files) > max_keep:
        for old_file in mp3_files[:-max_keep]:
            os.remove(old_file)


def build_rss_feed(episodes_dir: str, feed_path: str, base_url: str, secret: str) -> None:
    rss = ET.Element("rss", {
        "version": "2.0",
        "xmlns:itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    })
    channel = ET.SubElement(rss, "channel")

    public_prefix = f"{base_url.rstrip('/')}/{secret}"
    ET.SubElement(channel, "title").text = PODCAST_TITLE
    ET.SubElement(channel, "link").text = public_prefix
    ET.SubElement(channel, "description").text = PODCAST_DESCRIPTION
    ET.SubElement(channel, "language").text = "ja"
    ET.SubElement(channel, "itunes:author").text = PODCAST_AUTHOR
    ET.SubElement(channel, "itunes:explicit").text = "false"

    mp3_files = sorted(glob.glob(os.path.join(episodes_dir, "*.mp3")), reverse=True)

    for mp3_path in mp3_files:
        filename = os.path.basename(mp3_path)
        stem = os.path.splitext(filename)[0]
        try:
            ep_dt = datetime.strptime(stem, "%Y%m%d_%H%M%S").replace(tzinfo=JST)
        except ValueError:
            ep_dt = datetime.now(JST)

        file_size = os.path.getsize(mp3_path)
        audio_info = MP3(mp3_path)
        duration_sec = int(audio_info.info.length)
        mp3_url = f"{public_prefix}/{EPISODES_DIR_NAME}/{filename}"

        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = f"{ep_dt.strftime('%Y/%m/%d')} 朝のAIラジオ"
        ET.SubElement(item, "description").text = f"{ep_dt.strftime('%Y年%m月%d日')} 配信エピソード"
        ET.SubElement(item, "pubDate").text = format_datetime(ep_dt)
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = stem
        ET.SubElement(item, "enclosure", {
            "url": mp3_url,
            "length": str(file_size),
            "type": "audio/mpeg",
        })
        ET.SubElement(item, "itunes:duration").text = str(duration_sec)

    raw_xml = ET.tostring(rss, encoding="utf-8")
    pretty_xml = minidom.parseString(raw_xml).toprettyxml(indent="  ", encoding="utf-8")
    with open(feed_path, "wb") as f:
        f.write(pretty_xml)


def main() -> None:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("GEMINI_API_KEY が設定されていません。")

    secret = validate_secret(os.environ.get("RADIO_SECRET", "").strip())
    base_url = os.environ.get("PAGES_BASE_URL", "").strip()
    if not base_url.startswith("https://"):
        raise ValueError("PAGES_BASE_URL は https:// から始まるURLを設定してください。")

    local_context = os.environ.get("LOCAL_CONTEXT", "")
    model_name = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL

    now_jst = datetime.now(JST)
    timestamp_id = now_jst.strftime("%Y%m%d_%H%M%S")

    secret_dir = os.path.join(DIST_ROOT, secret)
    episodes_dir = os.path.join(secret_dir, EPISODES_DIR_NAME)
    os.makedirs(episodes_dir, exist_ok=True)

    print("1/3: 台本を生成中...")
    script_text = generate_script(api_key, model_name, local_context, now_jst)

    mp3_path = os.path.join(episodes_dir, f"{timestamp_id}.mp3")
    print("2/3: 音声を合成中...")
    asyncio.run(synthesize_speech(script_text, mp3_path, VOICE_NAME))

    cleanup_old_episodes(episodes_dir, MAX_KEEP_EPISODES)

    feed_path = os.path.join(secret_dir, FEED_FILE_NAME)
    print("3/3: RSSフィードを更新中...")
    build_rss_feed(episodes_dir, feed_path, base_url, secret)
    print("完了しました。")


if __name__ == "__main__":
    main()
