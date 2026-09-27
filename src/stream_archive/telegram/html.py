"""HTML replies for the Telegram bot (parse mode HTML).

Secrets render as one-tap code spans. A group chat never sees a secret:
it gets a pointer to the admin's private chat instead.
"""

import html


def escape(text: str) -> str:
    """Plain text for an HTML reply."""
    return html.escape(text, quote=False)


def code_span(secret: str) -> str:
    """A secret as an HTML code span. One tap on it copies the secret."""
    return f"<code>{html.escape(secret, quote=False)}</code>"


def reveal(label: str, secret: str, chat_id: int | None, admin_id: int, elsewhere: str) -> str:
    """A secret under ``label`` for the admin's private chat, or a pointer.

    The reply is HTML and the secret is a code span, so one tap on it
    copies the secret. A group chat keeps the secret out of the message:
    the whole group would otherwise read it.
    """
    if chat_id is not None and chat_id != admin_id:
        return escape(elsewhere)
    return f"{escape(label)}\n{code_span(secret)}"
