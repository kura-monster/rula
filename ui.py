"""Shared formatting for everything the bot says.

One module rather than a helper in each file, so the no-emoji rule has a single
choke point: every outgoing message is built by embed() below, and anything that
carries a pictograph — the bot's own labels, a song title from YouTube, the
model's answer — loses it there instead of at each call site.
"""
import asyncio
import contextlib
import re

import aiohttp
import discord

# "Discord could not be reached", as opposed to "Discord said no". These are
# raised by aiohttp underneath discord.py, so they are not HTTPException and slip
# straight through every handler written for it — which is how a failed DNS
# lookup became an unhandled traceback instead of a message. socket.gaierror,
# the one seen in the wild, is an OSError.
NETWORK_ERRORS = (aiohttp.ClientError, OSError, asyncio.TimeoutError)
# Anything that means the message did not get sent, for whichever reason.
SEND_ERRORS = (discord.HTTPException, *NETWORK_ERRORS)


def short_error(error: BaseException) -> str:
    """One line, for a log that should not carry a page of traceback."""
    return f"{type(error).__name__}: {clip(str(error), 160)}"


@contextlib.asynccontextmanager
async def typing(ctx):
    """ctx.typing(), downgraded to nothing when it cannot be started.

    The indicator is decoration; the command behind it is not. Entering it makes
    an HTTP request, and when Discord is unreachable that request failed before
    the command had begun — so a cosmetic flourish took down the work it was
    decorating. Only entering and leaving are guarded here, so a genuine failure
    inside the body still propagates.
    """
    indicator = ctx.typing()
    try:
        await indicator.__aenter__()
    except SEND_ERRORS:
        indicator = None
    try:
        yield
    finally:
        if indicator is not None:
            with contextlib.suppress(*SEND_ERRORS):
                await indicator.__aexit__(None, None, None)

# Left edge of the embed. Colour is the fastest way to tell at a glance what the
# bot is doing, and it is the only decoration left now that emoji are out.
COLOR_ANSWER = 0xE58FB1   # the reply
COLOR_WORKING = 0x9AA0A6  # thinking, or waiting for a turn
COLOR_ERROR = 0xE05252    # something went wrong
# Playback has its own colour and its own formatting helpers, and both live in
# music.py. Nothing to do with audio belongs here: this module is shared with
# the build that has no music.py at all, and a utility only one build can use is
# dead weight in the other.

# A description holds 4096 characters against a plain message's 2000.
MAX_EMBED_CHARS = 4096

# The table below is written as numbers and assembled at import. Spelling it with
# the characters themselves would put emoji in the one file whose job is to
# remove them, and three of these codepoints — the variation selector, the joiner
# and the combining keycap — are invisible: a line containing them looks blank,
# survives no editor that rewrites the encoding, and would take the filter down
# with it silently, which fails open and lets everything through.
_VS16 = 0xFE0F      # "render the character before me as an emoji"
_ZWJ = 0x200D       # binds several pictographs into one glyph
_KEYCAP = 0x20E3    # the box drawn around the digit of a keycap
_SKIN = (0x1F3FB, 0x1F3FF)
_ASTRAL = (0x1F000, 0x1FAFF)   # pictographs, emoticons, transport, flags
_TAGS = (0xE0020, 0xE007F)     # spell out the subdivision of a flag sequence

# Codepoints Discord renders as a colour glyph on their own, without being asked.
# Narrow runs rather than whole blocks, because the blocks these sit in also hold
# characters that are ordinary text: this leaves the star, the musical note, the
# check mark and the arrows alone while taking the hourglass and the warning sign.
_BMP_RANGES = (
    (0x231A, 0x231B), (0x23E9, 0x23EC), (0x23F0, 0x23F0), (0x23F3, 0x23F3),
    (0x25FD, 0x25FE), (0x2614, 0x2615), (0x2648, 0x2653), (0x267F, 0x267F),
    (0x2693, 0x2693), (0x26A1, 0x26A1), (0x26AA, 0x26AB), (0x26BD, 0x26BE),
    (0x26C4, 0x26C5), (0x26CE, 0x26CE), (0x26D4, 0x26D4), (0x26EA, 0x26EA),
    (0x26F2, 0x26F3), (0x26F5, 0x26F5), (0x26FA, 0x26FA), (0x26FD, 0x26FD),
    (0x2705, 0x2705), (0x270A, 0x270B), (0x2728, 0x2728), (0x274C, 0x274C),
    (0x274E, 0x274E), (0x2753, 0x2755), (0x2757, 0x2757), (0x2795, 0x2797),
    (0x27B0, 0x27B0), (0x27BF, 0x27BF), (0x2B1B, 0x2B1C), (0x2B50, 0x2B50),
    (0x2B55, 0x2B55), (0x3297, 0x3297), (0x3299, 0x3299),
)


def _cls(*ranges: tuple[int, int]) -> str:
    """Codepoint pairs as the body of a regex character class.

    Left unescaped on purpose: every codepoint used here is above U+00A0, so
    none of them can turn out to be a bracket, a caret or a backslash.
    """
    return "".join(chr(lo) if lo == hi else f"{chr(lo)}-{chr(hi)}"
                   for lo, hi in ranges)


# One emoji, however many codepoints it is spelt with. Ordered widest-first so a
# sequence is consumed whole: taking the base character and leaving the modifier
# behind would turn a removal into mojibake.
_EMOJI_ATOM = "|".join((
    r"<a?:\w{2,32}:\d{15,25}>",                # Discord's own <:name:id> markup
    f"[0-9#*]{chr(_VS16)}?{chr(_KEYCAP)}",     # keycaps: three codepoints, one glyph
    f"[{_cls(_ASTRAL)}][{_cls(_SKIN)}]?",      # with its skin tone attached
    f"[{_cls(*_BMP_RANGES)}]{chr(_VS16)}?",
    f"[{_cls((0xA0, 0x33FF))}]{chr(_VS16)}",   # anything asked to render as emoji
    f"[{_cls(_TAGS)}]",
    chr(_ZWJ),                                 # joiner left between removed halves
))
# The surrounding space is part of the match, so removing an emoji from mid
# sentence does not leave a double space where it used to be.
_EMOJI_RUN = re.compile(rf"[ \t]?(?:{_EMOJI_ATOM})+[ \t]?")


def strip_emoji(text: str) -> str:
    """Remove every emoji, keeping the spacing around it sane.

    Applied to outgoing text as a whole rather than to the bot's own wording
    alone: the model writes emoji unprompted and so do the people who title
    songs, and a ban that covered only the hard-coded strings would be a ban in
    name only.
    """
    if not text:
        return text
    # A run between two words collapses to one space; a run at either end goes
    # entirely, taking the space that was holding it there with it.
    return _EMOJI_RUN.sub(
        lambda m: " " if m.group(0)[:1] == " " and m.group(0)[-1:] == " " else "",
        text)


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def embed(description: str = "", title: str | None = None,
          color: int = COLOR_ANSWER) -> discord.Embed:
    """One place where an Embed is built, so every message looks the same.

    The description is clipped rather than trusted: Discord rejects the whole
    message when it runs over, which would lose the answer instead of trimming it.
    """
    return discord.Embed(
        title=clip(strip_emoji(title), 256) if title else None,
        color=color,
        description=clip(strip_emoji(description), MAX_EMBED_CHARS) or None)


def as_text(source: discord.Embed) -> str:
    """The same content as a plain message, for channels that refuse embeds."""
    parts = [p for p in (source.title, source.description) if p]
    return clip("\n".join(parts), 2000) or "(空の応答)"


def can_embed(channel) -> bool:
    """Whether this channel will accept an embed.

    Checked rather than attempted. Without Embed Links every send comes back 403
    and the bot goes silent with the reason visible only in the log — which is
    exactly how it looks when it is simply broken.
    """
    try:
        return channel.permissions_for(channel.guild.me).embed_links
    except AttributeError:
        return True  # no guild context to judge by; assume it is fine


def payload(channel, source: discord.Embed) -> dict:
    """`embed=` where allowed, `content=` where not.

    Both keys are always set, so an edit cannot leave the other one behind.
    """
    if can_embed(channel):
        return {"content": None, "embed": source}
    return {"content": as_text(source), "embed": None}


