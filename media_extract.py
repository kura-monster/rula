"""Extract audio (MP3) or video (MP4) from URLs using yt-dlp and ffmpeg.

Commands:
  R!mp3 <URL>           — Extract audio as MP3
  R!mp4 <URL>           — Extract video as MP4
  R!extract <mp3|mp4> <URL> — Extract media in the specified format
"""
import html
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.request

import aiohttp
import discord
from discord.ext import commands

import ui

log = logging.getLogger("rula.extract")

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

_URL_PATTERN = re.compile(r"https?://\S+")

# Default Discord upload limit: 25 MB
DEFAULT_MAX_BYTES = 25 * 1024 * 1024
TIMEOUT_SECONDS = 120


def find_ffmpeg() -> str | None:
    """Find ffmpeg binary via explicit path, PATH, or imageio-ffmpeg."""
    explicit = os.environ.get("FFMPEG_PATH", "").strip().strip('"')
    if explicit and os.path.isfile(explicit):
        return explicit
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _format_size(num_bytes: int) -> str:
    """Format bytes into a human-readable string."""
    for unit in ["B", "KB", "MB", "GB"]:
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def _format_duration(seconds: float | int | None) -> str:
    """Format duration in seconds into MM:SS or HH:MM:SS."""
    if seconds is None or seconds <= 0:
        return "--:--"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, sec = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes}:{sec:02d}"


def _sanitize_filename(name: str) -> str:
    """Sanitize filename to prevent invalid characters."""
    sanitized = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    return sanitized or "media"


def _download_sync(url: str, mode: str, output_dir: str, ffmpeg_exe: str, max_bytes: int) -> dict:
    """Synchronous download function to run in a thread pool."""
    if yt_dlp is None:
        raise RuntimeError("yt-dlp がインストールされていません。")

    out_template = os.path.join(output_dir, "%(title).100s.%(ext)s")

    common_opts = {
        "outtmpl": out_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ffmpeg_location": ffmpeg_exe,
        "max_filesize": max_bytes,
    }

    if mode == "mp3":
        ydl_opts = {
            **common_opts,
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }],
        }
    else:  # mp4
        ydl_opts = {
            **common_opts,
            "format": (
                "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/"
                "best[height<=720][ext=mp4]/"
                "best[ext=mp4]/"
                "bestvideo[height<=720]+bestaudio/"
                "best"
            ),
            "postprocessors": [{
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }],
        }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info:
            raise RuntimeError("動画または音声の情報を取得できませんでした。")

    # Find the downloaded file in output_dir
    files = [os.path.join(output_dir, f) for f in os.listdir(output_dir) if os.path.isfile(os.path.join(output_dir, f))]
    if not files:
        raise RuntimeError("ファイルのダウンロードに失敗しました。")

    # Pick the most recently created or modified file with desired extension
    target_ext = f".{mode}"
    matched_files = [f for f in files if f.lower().endswith(target_ext)]
    result_file = matched_files[0] if matched_files else files[0]

    return {
        "path": result_file,
        "title": info.get("title") or "不明なタイトル",
        "duration": info.get("duration"),
        "uploader": info.get("uploader") or info.get("channel") or "不明",
        "size": os.path.getsize(result_file),
    }


class MediaExtract(commands.Cog, name="メディア抽出"):
    """URLから音声(MP3)や動画(MP4)をダウンロードして送信する機能。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _extract_url(self, ctx: commands.Context, text: str) -> str:
        """Find a URL in given text or the referenced message."""
        text = text.strip()
        match = _URL_PATTERN.search(text)
        if match:
            return match.group(0)

        if ctx.message.reference:
            replied = ctx.message.reference.resolved
            if isinstance(replied, discord.Message) and replied.content:
                ref_match = _URL_PATTERN.search(replied.content)
                if ref_match:
                    return ref_match.group(0)

        return ""

    async def _handle_extract(self, ctx: commands.Context, mode: str, raw_arg: str):
        url = self._extract_url(ctx, raw_arg)
        if not url:
            mode_upper = mode.upper()
            raise commands.UserInputError(
                f"URLを指定するか、URLを含むメッセージに返信して実行してください。\n"
                f"例: `R!{mode} https://www.youtube.com/watch?v=...`"
            )

        ffmpeg_exe = find_ffmpeg()
        if not ffmpeg_exe:
            raise commands.CheckFailure(
                "ffmpeg が見つからないため、メディアの変換ができません。\n"
                "`imageio-ffmpeg` をインストールするか、PATHに ffmpeg を追加してください。"
            )

        max_bytes = ctx.guild.filesize_limit if ctx.guild else DEFAULT_MAX_BYTES
        mode_label = "音声 (MP3)" if mode == "mp3" else "動画 (MP4)"

        status_msg = await ctx.reply(
            embed=ui.embed(f"`{url}`\nから {mode_label} を抽出・変換しています…\nしばらくお待ちください。",
                           "抽出処理中…", ui.COLOR_WORKING),
            mention_author=False
        )

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                try:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(
                            _download_sync, url, mode, temp_dir, ffmpeg_exe, max_bytes
                        ),
                        timeout=TIMEOUT_SECONDS
                    )
                except asyncio.TimeoutError:
                    raise RuntimeError(f"処理がタイムアウトしました ({TIMEOUT_SECONDS}秒以内)。")
                except Exception as e:
                    err_text = str(e)
                    if "File is larger than max_filesize" in err_text or "larger than" in err_text.lower():
                        limit_str = _format_size(max_bytes)
                        raise RuntimeError(f"ファイルサイズがDiscordの上限（{limit_str}）を超えています。")
                    raise RuntimeError(f"抽出に失敗しました: {err_text}")

                file_path = result["path"]
                file_size = result["size"]

                if file_size > max_bytes:
                    limit_str = _format_size(max_bytes)
                    size_str = _format_size(file_size)
                    raise RuntimeError(
                        f"ファイルサイズ（{size_str}）がDiscordの上限（{limit_str}）を超えているため送信できませんでした。"
                    )

                ext = os.path.splitext(file_path)[1]
                safe_name = _sanitize_filename(result["title"]) + ext
                d_file = discord.File(file_path, filename=safe_name)

                desc_lines = [
                    f"**タイトル:** {result['title']}",
                    f"**投稿者:** {result['uploader']}",
                    f"**長さ:** {_format_duration(result['duration'])}",
                    f"**サイズ:** {_format_size(file_size)}",
                ]

                result_embed = ui.embed("\n".join(desc_lines), f"{mode_label} 抽出完了", ui.COLOR_ANSWER)

                # Send file and embed
                await ctx.reply(embed=result_embed, file=d_file, mention_author=False)

                # Delete or update status message
                try:
                    await status_msg.delete()
                except Exception:
                    pass

        except Exception as e:
            err_embed = ui.embed(str(e), "抽出エラー", ui.COLOR_ERROR)
            try:
                await status_msg.edit(embed=err_embed)
            except Exception:
                await ctx.reply(embed=err_embed, mention_author=False)

    @commands.cooldown(2, 20.0, commands.BucketType.user)
    @commands.command(name="mp3", aliases=["audio", "音楽抽出", "音声抽出"])
    async def extract_mp3(self, ctx: commands.Context, *, url: str = ""):
        """URLから音声を抽出し、MP3ファイルとして送信します。R!mp3 <URL>"""
        await self._handle_extract(ctx, "mp3", url)

    @commands.cooldown(2, 20.0, commands.BucketType.user)
    @commands.command(name="mp4", aliases=["video", "動画抽出"])
    async def extract_mp4(self, ctx: commands.Context, *, url: str = ""):
        """URLから動画を抽出し、MP4ファイルとして送信します。R!mp4 <URL>"""
        await self._handle_extract(ctx, "mp4", url)

    @commands.cooldown(2, 20.0, commands.BucketType.user)
    @commands.command(name="extract", aliases=["dl", "download"])
    async def extract(self, ctx: commands.Context, mode: str = "", *, url: str = ""):
        """指定した形式(mp3/mp4)でメディアを抽出します。R!extract <mp3|mp4> <URL>"""
        mode_clean = mode.strip().lower()
        if mode_clean in ("mp3", "audio", "音声"):
            target_mode = "mp3"
            target_url = url
        elif mode_clean in ("mp4", "video", "動画"):
            target_mode = "mp4"
            target_url = url
        elif _URL_PATTERN.search(mode_clean):
            # If the user typed `R!dl <URL>`, default to mp4
            target_mode = "mp4"
            target_url = f"{mode} {url}".strip()
        else:
            raise commands.UserInputError(
                "形式とURLを指定してください。\n"
                "例: `R!extract mp3 <URL>` または `R!extract mp4 <URL>`\n"
                "短縮コマンド: `R!mp3 <URL>` / `R!mp4 <URL>`"
            )

        await self._handle_extract(ctx, target_mode, target_url)


EXTRACT_HELP_LINES = [
    ("R!mp3 <URL>", "URLから音声を抽出してMP3で送信します (返信でも可)"),
    ("R!mp4 <URL>", "URLから動画を抽出してMP4で送信します (返信でも可)"),
    ("R!extract <mp3|mp4> <URL>", "指定した形式でメディアを抽出します"),
]

SUMMARY_HELP_LINES = [
    ("R!ytsum <URL>", "YouTube動画やWeb記事の内容をAIで要約します (返信でも可)"),
]

HELP_LINES = EXTRACT_HELP_LINES + SUMMARY_HELP_LINES

# --------------------------------------------------------------------------- #
# URL & YouTube Summarizer (Integrated from url_summary)
# --------------------------------------------------------------------------- #

_TAG_PATTERN = re.compile(r"<[^>]+>")

SUMMARY_PROMPT = """\
次は「{title}」に関する内容（字幕、トランスクリプト、または記事本文）です。
この内容の要点を分かりやすくまとめてください。

条件:
- 最初に全体の概要を1〜2文で説明すること
- その後、「主なポイント:」として箇条書き（・）で3〜5項目にまとめること
- 重要な結論、数字、新情報があれば優先して含めること
- 読みやすく自然な日本語でまとめること

テキスト:
{text}"""


def _clean_html(raw_html: str) -> str:
    no_tags = _TAG_PATTERN.sub(" ", raw_html)
    return html.unescape(no_tags).strip()


def _parse_vtt(vtt_text: str) -> str:
    lines = []
    seen = set()
    for line in vtt_text.splitlines():
        line = line.strip()
        if not line or "-->" in line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        clean = _clean_html(line)
        if clean and clean not in seen:
            seen.add(clean)
            lines.append(clean)
    return " ".join(lines)


def _extract_yt_content_sync(url: str) -> dict:
    if yt_dlp is None:
        raise RuntimeError("yt-dlp が利用できません。")

    ydl_opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["ja", "en", "all"],
        "quiet": True,
        "no_warnings": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if not info:
            raise RuntimeError("動画の情報を取得できませんでした。")

    title = info.get("title") or "YouTube動画"
    uploader = info.get("uploader") or info.get("channel") or "不明"
    description = (info.get("description") or "").strip()

    subs = info.get("subtitles") or {}
    auto_subs = info.get("automatic_captions") or {}

    transcript_text = ""
    sub_url = ""

    for lang in ["ja", "en"]:
        if lang in subs and subs[lang]:
            sub_url = subs[lang][0].get("url", "")
            break
        if lang in auto_subs and auto_subs[lang]:
            sub_url = auto_subs[lang][0].get("url", "")
            break

    if sub_url:
        try:
            req = urllib.request.Request(sub_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                vtt_content = resp.read().decode("utf-8", "ignore")
                transcript_text = _parse_vtt(vtt_content)
        except Exception as e:
            log.warning("字幕のダウンロードに失敗しました: %s", e)

    content = transcript_text if len(transcript_text) > 100 else description
    if not content:
        content = "（動画の説明や字幕を取得できませんでした）"

    return {
        "type": "youtube",
        "title": title,
        "uploader": uploader,
        "content": content[:5000],
        "has_transcript": bool(transcript_text and len(transcript_text) > 100),
    }


def _make_http_session() -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    return aiohttp.ClientSession(connector=connector)


async def _extract_article_content(session: aiohttp.ClientSession, url: str) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    timeout = aiohttp.ClientTimeout(total=15.0)
    async with session.get(url, headers=headers, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"ページを取得できませんでした (HTTP {resp.status})")
        html_text = await resp.text(errors="ignore")

    title_match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
    title = html.unescape(title_match.group(1)).strip() if title_match else url

    cleaned_html = re.sub(r"<(script|style|header|footer|nav|noscript)[^>]*>.*?</\1>", " ", html_text, flags=re.IGNORECASE | re.DOTALL)
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", cleaned_html, re.IGNORECASE | re.DOTALL)
    text_blocks = [_clean_html(p) for p in paragraphs if len(_clean_html(p)) > 20]

    article_text = "\n".join(text_blocks)
    if not article_text:
        article_text = _clean_html(cleaned_html)

    article_text = re.sub(r"\s+", " ", article_text).strip()

    return {
        "type": "article",
        "title": title,
        "uploader": "",
        "content": article_text[:5000],
        "has_transcript": False,
    }


class UrlSummary(commands.Cog, name="URL要約"):
    """YouTube動画やWeb記事をAIで要約する機能。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _get_url(self, ctx: commands.Context, given: str) -> str:
        given = given.strip()
        match = _URL_PATTERN.search(given)
        if match:
            return match.group(0)
        if ctx.message.reference:
            replied = ctx.message.reference.resolved
            if isinstance(replied, discord.Message) and replied.content:
                ref_match = _URL_PATTERN.search(replied.content)
                if ref_match:
                    return ref_match.group(0)
        return ""

    @commands.cooldown(3, 30.0, commands.BucketType.user)
    @commands.command(name="ytsum", aliases=["urlsum", "ytsummary", "sumurl", "動画要約", "記事要約"])
    async def ytsum(self, ctx: commands.Context, *, arg: str = ""):
        """YouTube動画や記事のURLをAIで要約します。R!ytsum <URL>"""
        url = self._get_url(ctx, arg)
        if not url:
            raise commands.UserInputError(
                "要約したいYouTube動画や記事のURLを入力するか、URLを含むメッセージに返信してください。\n"
                "例: `R!ytsum https://www.youtube.com/watch?v=...`"
            )

        if not self.bot.rula.configured:
            raise commands.CheckFailure("AI会話機能が無効なため要約できません（RULA_API_KEY未設定）。")

        is_youtube = any(k in url.lower() for k in ("youtube.com", "youtu.be"))
        label = "YouTube動画" if is_youtube else "Web記事"

        status_msg = await ctx.reply(
            embed=ui.embed(f"`{url}`\nの{label}情報を読み込んでいます…", f"{label}要約", ui.COLOR_WORKING),
            mention_author=False
        )

        try:
            if is_youtube:
                data = await asyncio.to_thread(_extract_yt_content_sync, url)
            else:
                async with _make_http_session() as session:
                    data = await _extract_article_content(session, url)

            title = data["title"]
            content = data["content"]

            if not content or len(content) < 30:
                raise RuntimeError("要約に十分なテキスト（字幕や本文）を取得できませんでした。")

            source_info = "（字幕から要約）" if data.get("has_transcript") else "（説明/本文から要約）"
            prompt = SUMMARY_PROMPT.format(title=title, text=content)

            await status_msg.edit(embed=ui.embed(f"「{title}」\nをAIが要約中…", f"{label}要約", ui.COLOR_WORKING))

            summary = await self.bot.rula.ask(prompt)
            if not summary:
                raise RuntimeError("AIによる要約の生成に失敗しました。")

            result_embed = ui.embed(
                f"**[{title}]({url})**\n{source_info}\n\n{summary}",
                f"{label}の要約",
                ui.COLOR_ANSWER
            )
            await status_msg.edit(embed=result_embed)

        except Exception as e:
            log.exception("ytsum failed: %s", e)
            await status_msg.edit(embed=ui.embed(f"要約に失敗しました: {e}", "エラー", ui.COLOR_ERROR))


# 後方互換性
sys.modules["url_summary"] = sys.modules[__name__]


async def setup(bot: commands.Bot):
    await bot.add_cog(MediaExtract(bot))
    await bot.add_cog(UrlSummary(bot))

