"""Convert Claude's GitHub-flavored Markdown into Slack mrkdwn.

Slack `text` uses its own dialect: *bold*, _italic_, ~strike~, <url|label>,
and no headings. Claude replies in standard Markdown, so `**bold**` shows up
raw in the thread. Code spans/blocks are protected and left untouched.
"""
import re

_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]+`")
_PLACEHOLDER = "\x00{}\x00"

_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
# Only `**` — `__x__` is left alone so Python dunders (__init__) survive.
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_ITALIC_STAR = re.compile(r"(?<![\*\w])\*(?=[^\s\*])(.+?)(?<=[^\s\*])\*(?![\*\w])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_IMAGE = re.compile(r"!\[([^\]]*)\]\((\S+?)\)")
_LINK = re.compile(r"\[([^\]]+)\]\((\S+?)\)")
_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)
_HRULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$", re.MULTILINE)
_BOLD_TOKEN = "\x01"
# Slack auto-links digit runs it takes for phone numbers, so "(138.199.60.40,"
# arrives as <tel:(138.199.60.40|...>. Code spans are never auto-linked.
_IPV4 = re.compile(
    r"(?<![A-Za-z0-9.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)"
    r"(?:/\d{1,2})?(?![A-Za-z0-9/]|\.\d)"
)
_SLACK_LINK = re.compile(r"<[^<>\n]+>")


def md_to_mrkdwn(text: str) -> str:
    if not text:
        return text

    protected: list[str] = []

    def _protect(s: str) -> str:
        protected.append(s)
        return _PLACEHOLDER.format(len(protected) - 1)

    # Fenced blocks: drop the language tag (Slack shows it as literal text).
    text = _FENCE.sub(lambda m: _protect("```\n" + m.group(1) + "```"), text)
    text = _INLINE_CODE.sub(lambda m: _protect(m.group(0)), text)

    text = _HRULE.sub("───", text)
    # Mark bold with a sentinel first so the italic pass doesn't eat it.
    text = _HEADING.sub(lambda m: f"{_BOLD_TOKEN}{m.group(1)}{_BOLD_TOKEN}", text)
    text = _BOLD.sub(lambda m: f"{_BOLD_TOKEN}{m.group(1)}{_BOLD_TOKEN}", text)
    text = _BULLET.sub(lambda m: f"{m.group(1)}• ", text)
    text = _ITALIC_STAR.sub(r"_\1_", text)
    text = _STRIKE.sub(r"~\1~", text)
    text = _IMAGE.sub(r"<\2|\1>", text)
    text = _LINK.sub(r"<\2|\1>", text)
    # Nested bold-inside-heading would double up; collapse it.
    text = re.sub(f"{_BOLD_TOKEN}{{2,}}", _BOLD_TOKEN, text)
    text = text.replace(_BOLD_TOKEN, "*")
    # Last, after link conversion so IPs inside <url|label> are protected first.
    text = _SLACK_LINK.sub(lambda m: _protect(m.group(0)), text)
    text = _IPV4.sub(lambda m: _protect(f"`{m.group(0)}`"), text)

    return re.sub(r"\x00(\d+)\x00", lambda m: protected[int(m.group(1))], text)
