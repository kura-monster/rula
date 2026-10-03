"""Translation, summarising, and the quote generator.

Three commands that are all the same shape underneath: take some text, wrap it
in an instruction, hand it to the model, print what comes back. They live
together because that shape is the whole implementation — RulaBot.run_task does
the queueing, the error handling and the formatting, and everything here is the
wording of one prompt.

The prompts are deliberately explicit about what *not* to do. A small model
asked to translate will otherwise explain its translation, apologise for it, or
answer the text instead of translating it, and each of those is a worse failure
than a clumsy translation would be.
"""
import logging
import re

import discord
from discord.ext import commands

import ui
import websearch

log = logging.getLogger("rula.assist")

# How far back R!summary reads, and the ceiling someone can ask for. The limit
# is the model's context, not Discord's: a thousand messages would be truncated
# somewhere in the middle and summarised as though the rest were never said.
SUMMARY_DEFAULT = 50
SUMMARY_MAX = 200
# Per message, so one pasted essay cannot crowd out the rest of the log.
SUMMARY_CHARS_PER_MESSAGE = 300
SUMMARY_TOTAL_CHARS = 6000
# The API rejects anything longer, and trimming here gives a translation of most
# of it rather than an error about all of it.
MAX_SOURCE_CHARS = 3000

# Targets worth naming. The model handles more than these; this list exists so
# `R!tr fr ...` is understood as a language rather than as text to translate.
LANGUAGES = {
    "ja": "日本語", "jp": "日本語", "japanese": "日本語", "日本語": "日本語",
    "en": "英語", "english": "英語", "英語": "英語",
    "ko": "韓国語", "korean": "韓国語", "韓国語": "韓国語",
    "zh": "中国語", "cn": "中国語", "chinese": "中国語", "中国語": "中国語",
    "es": "スペイン語", "spanish": "スペイン語",
    "fr": "フランス語", "french": "フランス語",
    "de": "ドイツ語", "german": "ドイツ語",
    "ru": "ロシア語", "russian": "ロシア語",
    "pt": "ポルトガル語", "it": "イタリア語",
    "th": "タイ語", "vi": "ベトナム語", "id": "インドネシア語",
    "ar": "アラビア語", "hi": "ヒンディー語", "tr": "トルコ語",
}

# Kana and kanji, built from codepoints so the ranges are readable as ranges
# rather than as four characters that happen to sit at the ends of them.
_JAPANESE = re.compile("[{}-{}{}-{}]".format(
    chr(0x3040), chr(0x30FF),      # hiragana and katakana
    chr(0x4E00), chr(0x9FFF)))     # kanji


def _looks_japanese(text: str) -> bool:
    """Whether to translate out of Japanese rather than into it.

    Counted against the letters only. A Japanese sentence quoting an English
    product name is still Japanese, and punctuation and digits say nothing
    either way.
    """
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    return sum(bool(_JAPANESE.match(c)) for c in letters) / len(letters) > 0.2


TRANSLATE_PROMPT = """\
次のテキストを{target}に翻訳してください。

条件:
- 直訳ではなく、{target}として自然な表現にすること
- 口調と丁寧さを保つこと(タメ口はタメ口、敬語は敬語、乱暴な言葉は乱暴なまま)
- ネットスラングや略語は、{target}側の対応する言い方に置き換えること
- 訳文だけを出力すること。前置き、説明、原文の再掲は書かないこと
- 訳しようのない固有名詞はそのまま残すこと
- 複数の解釈があり得る場合のみ、訳文の後に1行だけ「補足:」として書くこと

テキスト:
{text}"""

SUMMARY_PROMPT = """\
次はDiscordのチャットログです。話の要点をまとめてください。

条件:
- 箇条書きで、各行の先頭に「・」を付けること
- {lines}行以内に収めること
- 誰が何を言ったか分かるように、必要なら名前を入れること
- 話題が複数あるなら分けて書くこと
- 挨拶、相槌、スタンプだけの発言は無視すること
- 決まったこと、結論、質問と答えを優先すること
- ログに書かれていないことは書かないこと
- まとめだけを出力し、前置きを書かないこと

ログ:
{log}"""

# The quote styles. Each is one line of instruction and one worked example: a
# small model copies the shape of an example far more reliably than it follows
# a description of it.
STYLES: dict[str, tuple[str, str, str]] = {
    "shinjiro": (
        "小泉進次郎構文",
        "同じ内容を言い換えただけの同語反復にしてください。"
        "真面目で断定的な口調のまま、よく読むと何も言っていない文にすること。",
        "例: 「今のままではいけないと思います。だからこそ、"
        "日本は今のままではいけないんです。」",
    ),
    "ruu": (
        "ルー語",
        "文の一部を、意味のない場所で英単語(カタカナ)に置き換えてください。"
        "置き換えるのは簡単な単語だけにし、文の骨格は日本語のまま残すこと。",
        "例: 「藪からスティック」「寝耳にウォーター」",
    ),
    "kotowaza": (
        "ことわざ風",
        "古くからあることわざのような、短くて言い切りの形にしてください。"
        "実際には存在しないのに、昔からありそうな響きにすること。",
        "例: 「急がば回れ」のような、七五調に近い短い形",
    ),
    "ishiki": (
        "意識高い系",
        "カタカナのビジネス用語を詰め込んで、中身のない自己啓発風にしてください。"
        "コミット、シナジー、リソース、バリューなどを使うこと。",
        "例: 「そのタスク、コミットする前にバリューをリソースとシナジーさせるべき」",
    ),
    "haiku": (
        "俳句",
        "五・七・五の十七音にまとめてください。季語は無くて構いません。",
        "例: 「古池や 蛙飛び込む 水の音」",
    ),
    "edo": (
        "江戸っ子",
        "江戸っ子の啖呵のような、威勢のいい話し言葉にしてください。"
        "「べらぼうめ」「てやんでい」などを使うこと。",
        "例: 「てやんでい、そんなこたぁ知ったこっちゃねえや」",
    ),
    "chuuni": (
        "中二病",
        "大げさな漢字と英語のルビが似合う、仰々しい言い回しにしてください。",
        "例: 「我が右手に宿りし闇が疼く」",
    ),
}

MEIGEN_PROMPT = """\
次のお題を「{name}」の形式に変換してください。

{how}
{example}

条件:
- 変換した文だけを出力すること。解説や前置きは書かないこと
- 3文以内に収めること
- お題の話題からは離れないこと

お題:
{topic}"""


def _style(name: str) -> tuple[str, str, str] | None:
    key = name.strip().lower()
    aliases = {"進次郎": "shinjiro", "しんじろう": "shinjiro", "koizumi": "shinjiro",
               "ルー": "ruu", "るー": "ruu", "lou": "ruu",
               "ことわざ": "kotowaza", "諺": "kotowaza",
               "意識高い": "ishiki", "意識高い系": "ishiki",
               "俳句": "haiku", "はいく": "haiku",
               "江戸": "edo", "江戸っ子": "edo",
               "中二": "chuuni", "中二病": "chuuni"}
    return STYLES.get(aliases.get(key, key))


class Assist(commands.Cog, name="AI"):
    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.CheckFailure("サーバー内でのみ使えます。")
        return True

    def _source_text(self, ctx: commands.Context, given: str) -> str:
        """The text to work on: what was typed, or what was replied to.

        Replying is the useful half. The message worth translating is almost
        always one already on screen, and making people copy it out first is
        friction that stops the feature being used at all.
        """
        text = given.strip()
        if not text and ctx.message.reference:
            replied = ctx.message.reference.resolved
            if isinstance(replied, discord.Message):
                text = (replied.content or "").strip()
        return text[:MAX_SOURCE_CHARS]

    @commands.cooldown(4, 20.0, commands.BucketType.user)
    @commands.command(name="tr", aliases=["translate", "翻訳"])
    async def translate(self, ctx: commands.Context, *, args: str = ""):
        """翻訳します。R!tr <文> / R!tr en <文> / 返信して R!tr"""
        target = ""
        rest = args.strip()
        # A leading word that names a language is the target, not the text.
        # Only when something follows it, so "R!tr en" on a reply still works.
        head, _, tail = rest.partition(" ")
        if head.lower() in LANGUAGES:
            target = LANGUAGES[head.lower()]
            rest = tail.strip()
        elif rest.lower() in LANGUAGES and ctx.message.reference:
            target = LANGUAGES[rest.lower()]
            rest = ""

        text = self._source_text(ctx, rest)
        if not text:
            raise commands.UserInputError(
                "翻訳したい文を入力するか、対象メッセージに返信してください。\n"
                "例: `R!tr Hello` / `R!tr en おはよう` / 返信して `R!tr`")
        if not target:
            target = "英語" if _looks_japanese(text) else "日本語"
        await self.bot.run_task(
            ctx.message, TRANSLATE_PROMPT.format(target=target, text=text),
            title=f"翻訳 ({target})")

    @commands.cooldown(2, 60.0, commands.BucketType.channel)
    @commands.command(name="summary", aliases=["summarize", "要約", "sum"])
    async def summary(self, ctx: commands.Context, count: int = SUMMARY_DEFAULT):
        """直近の会話を要約します。R!summary [件数]"""
        count = max(5, min(count, SUMMARY_MAX))
        try:
            history = [m async for m in ctx.channel.history(limit=count + 1)]
        except discord.Forbidden:
            raise commands.CheckFailure(
                "このチャンネルの履歴を読む権限がありません。") from None
        except ui.SEND_ERRORS as e:
            raise commands.CheckFailure(
                f"履歴を取得できませんでした: {ui.short_error(e)}") from None

        lines, total = [], 0
        for msg in reversed(history):
            if msg.id == ctx.message.id or msg.author.bot:
                continue
            body = (msg.clean_content or "").strip()
            if not body or body.startswith(("R!", "r!", "R！", "r！")):
                continue
            body = ui.clip(body.replace("\n", " "), SUMMARY_CHARS_PER_MESSAGE)
            entry = f"{msg.author.display_name}: {body}"
            total += len(entry)
            if total > SUMMARY_TOTAL_CHARS:
                # Oldest first, so what falls off the end is the newest — which
                # would be wrong. Stop instead and say how far it got.
                break
            lines.append(entry)

        if len(lines) < 3:
            raise commands.UserInputError(
                "要約できるだけの会話が見つかりませんでした。")
        await self.bot.run_task(
            ctx.message,
            SUMMARY_PROMPT.format(lines=min(8, max(3, len(lines) // 6)),
                                  log="\n".join(lines)),
            title=f"要約 (直近 {len(lines)} 件)")

    @commands.cooldown(4, 20.0, commands.BucketType.user)
    @commands.command(name="meigen", aliases=["mg", "迷言", "構文"])
    async def meigen(self, ctx: commands.Context, *, args: str = ""):
        """迷言を生成します。R!meigen [形式] <お題>"""
        rest = args.strip()
        style = None
        head, _, tail = rest.partition(" ")
        if head:
            style = _style(head)
            if style:
                rest = tail.strip()
        if style is None:
            style = STYLES["shinjiro"]     # the one people mean by default

        topic = self._source_text(ctx, rest)
        if not topic:
            names = " / ".join(sorted({v[0] for v in STYLES.values()}))
            raise commands.UserInputError(
                "お題を入力するか、対象メッセージに返信してください。\n"
                "例: `R!meigen 環境問題` / `R!meigen 俳句 今日の天気`\n"
                f"形式: {names}")
        name, how, example = style
        await self.bot.run_task(
            ctx.message,
            MEIGEN_PROMPT.format(name=name, how=how, example=example, topic=topic),
            title=f"迷言 ({name})")

    @commands.cooldown(3, 20.0, commands.BucketType.user)
    @commands.command(name="web", aliases=["google", "検索"])
    async def web(self, ctx: commands.Context, *, query: str = ""):
        """検索結果をそのまま表示します。R!web <検索語>"""
        query = self._source_text(ctx, query)
        if not query:
            raise commands.UserInputError(
                "検索したい言葉を入力してください。例: `R!web 今日の天気 東京`")
        if not websearch.enabled():
            raise commands.CheckFailure("検索機能は無効になっています。")
        async with ui.typing(ctx):
            results = await websearch.search(query, count=5)
        if not results:
            raise commands.UserInputError(
                "検索結果が得られませんでした。時間をおいて試してください。")
        lines = []
        for i, r in enumerate(results, 1):
            title = discord.utils.escape_markdown(ui.clip(r.title, 90))
            lines.append(f"**{i}. {title}**\n{r.url}\n{ui.clip(r.snippet, 180)}")
        await self.bot._say(
            ui.embed("\n\n".join(lines),
                     f"検索: {ui.clip(query, 60)} ({websearch.backend()})",
                     ui.COLOR_ANSWER), ctx.message)

    @commands.command(name="styles", aliases=["形式"])
    async def styles(self, ctx: commands.Context):
        """迷言の形式一覧を表示します。"""
        lines = [f"`{key}` — {name}" for key, (name, _, _) in STYLES.items()]
        lines.append("\n例: `R!meigen haiku 会社の昼休み`")
        await self.bot._say(
            ui.embed("\n".join(lines), "迷言の形式", ui.COLOR_ANSWER), ctx.message)


HELP_LINES = [
    ("R!tr <文>", "翻訳します (返信でも可、`R!tr en` で言語指定)"),
    ("R!summary [件数]", "直近の会話を要約します"),
    ("R!meigen [形式] <お題>", "迷言を生成します"),
    ("R!web <検索語>", "Web検索の結果をそのまま表示します"),
    ("R!styles", "迷言の形式一覧を表示します"),
]


async def setup(bot):
    await bot.add_cog(Assist(bot))
