"""Safe, consistent rich replies using Telegram's native rich-message API."""

import html
import re
from html.parser import HTMLParser

from telegram import Message
from telegram.error import BadRequest, InvalidToken


def message_text(message):
    """PTB 22.8 retains newer rich message fields in api_kwargs."""
    if message.text or message.caption:
        return message.text or message.caption
    rich = (getattr(message, "api_kwargs", None) or {}).get("rich_message", {})
    parts = []

    class TextParser(HTMLParser):
        def handle_data(self, data):
            parts.append(data)

        def handle_endtag(self, tag):
            if tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6"}:
                parts.append("\n")

        def handle_starttag(self, tag, attrs):
            if tag == "br":
                parts.append("\n")

    def visit(value):
        if isinstance(value, dict):
            if isinstance(value.get("html"), str):
                TextParser().feed(value["html"])
            elif isinstance(value.get("text"), str):
                parts.append(value["text"] + "\n")
            elif isinstance(value.get("markdown"), str):
                parts.append(value["markdown"])
            else:
                for child in value.values():
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(rich)
    return "".join(parts).strip()


def inline(text):
    escaped = html.escape(str(text), quote=False)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)
    return escaped


def markup(text, native=True):
    parts = str(text).split("\n\n")
    rendered = []
    for i, part in enumerate(parts):
        lines = part.splitlines()
        if i == 0 and lines and len(lines[0]) <= 130:
            heading = re.sub(r"^[#\s]+|\*\*", "", lines.pop(0))
            rendered.append(
                f"<h3>{html.escape(heading)}</h3>" if native else f"<b>{html.escape(heading)}</b>"
            )
        if lines:
            body = "\n".join(inline(line) for line in lines)
            rendered.append(f"<p>{body}</p>" if native else body)
    return "\n".join(rendered) if native else "\n\n".join(rendered)


class RichSender:
    def __init__(self, mode="native"):
        self.mode = mode

    async def send(self, bot, chat, text, **kwargs):
        if self.mode == "native" and callable(getattr(bot, "do_api_request", None)):
            data = {"chat_id": chat, "rich_message": {"html": markup(text)}, **kwargs}
            for key in ("reply_markup", "reply_parameters"):
                if hasattr(data.get(key), "to_dict"):
                    data[key] = data[key].to_dict()
            try:
                return await bot.do_api_request("sendRichMessage", api_kwargs=data, return_type=Message)
            except (BadRequest, InvalidToken) as exc:
                # PTB also maps HTTP 404 from an unknown endpoint to InvalidToken.
                if not isinstance(exc, InvalidToken) and not any(
                    term in str(exc).lower() for term in ("not found", "unknown method", "not supported")
                ):
                    raise
                # Older self-hosted Bot API servers can still receive formatted HTML.
                self.mode = "html"
        return await bot.send_message(chat, markup(text, native=False), parse_mode="HTML", **kwargs)
