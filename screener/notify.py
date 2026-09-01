"""Optional buy-signal notifications.

Three transports, all independent and all optional. Every one of them is
skipped silently when its configuration is absent, so a laptop run just prints
and CI sends whatever it has been given:

* `send_push` -- ntfy, for phones. Several people, one topic, no app to build.
* `send_webhook` -- Slack or Discord, via SCREENER_WEBHOOK_URL.
* `send_github_issue` -- an issue, which GitHub emails to whoever watches the
  repository. The only one that needs no credential of ours at all.

Unset all three and everything still lands in SQLite, the CSVs, and stdout.

What gets sent, and the once-only rule that governs it, live in
`cli._notify_new_strong_buys` and `screener.notified`.
"""

from __future__ import annotations

import os

import requests

from .signals import SELL
from .storage import Signal


def format_signal(sig: Signal, rule_description: str, horizon=None) -> str:
    # Announce the side it actually is. This read "BUY SIGNAL" for everything,
    # which was harmless while only buys existed and became a lie the day the
    # sell direction was added -- and sells outnumber buys most runs.
    side = "SELL SIGNAL" if sig.direction == SELL else "BUY SIGNAL"
    head = f"{side} — {sig.symbol}"
    if horizon is not None:
        head += f" [{horizon.label} chart]"
    lines = [
        head,
        f"  RSI crossed up on {sig.up1_date}, dipped {sig.down_date}, crossed up again {sig.up2_date}",
    ]
    if sig.price is not None and sig.fair_value is not None:
        lines.append(f"  Price {sig.price:,.2f} vs fair value {sig.fair_value:,.2f}")
    if sig.earnings_growth is not None:
        lines.append(f"  YoY EPS growth: {sig.earnings_growth:+.1f}%")
    lines.append(f"  Valuation gate: {rule_description}")
    if horizon is not None:
        lines.append(
            f"  Needs {horizon.margin_pct} headroom to fair value on this timeframe; "
            f"suggested leverage {horizon.leverage}x"
        )
    return "\n".join(lines)


def send_webhook(message: str) -> bool:
    """Post to the configured webhook. Returns False if none is configured."""
    url = os.environ.get("SCREENER_WEBHOOK_URL")
    if not url:
        return False
    try:
        response = requests.post(url, json={"text": message, "content": message}, timeout=15)
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        print(f"  ! webhook failed: {exc}")
        return False


def format_strong_buy(symbol: str, discount,  price, fair_value, currency: str,
                horizon, threshold: float, url: str) -> str:
    """A newly fired strong buy, as a line worth interrupting someone for.

    Kept short on purpose. This is a push notification, not the dashboard --
    it needs to say which stock, how cheap, and where to look.
    """
    money = f"{price:,.2f}" if price is not None else "?"
    fair = f"{fair_value:,.2f}" if fair_value is not None else "?"
    ccy = "" if currency == "USD" else f" {currency}"
    return "\n".join([
        f"STRONG BUY 🚀 — {symbol}"
        + (f"  ({discount * 100:.0f}% below fair value)" if discount is not None else ""),
        f"  Second cross of {threshold:g} within the last {horizon.fresh_label}"
        f" on the {horizon.label} chart",
        f"  {money}{ccy} against a {fair}{ccy} fair value"
        f" · {horizon.leverage}x suggested",
        f"  {url}",
    ])


def format_pattern_buy(symbol: str, price, currency: str, horizon,
                       threshold: float, url: str) -> str:
    """A fired pattern on something with no valuation to confirm it.

    Crypto. It has to read as a *weaker* claim than a strong buy, because it
    is: the same double cross, with nothing independent agreeing. Rendering it
    with the rocket and the word "strong" would erase in the notification the
    distinction the dashboard is careful to make -- and the phone is where the
    distinction matters most, since a push is read in three seconds and acted
    on without opening anything.

    So: a different marker, the word "pattern", and a line saying outright that
    no valuation exists rather than leaving a suspicious blank where the fair
    value normally sits.
    """
    money = f"{price:,.2f}" if price is not None else "?"
    ccy = "" if currency == "USD" else f" {currency}"
    return "\n".join([
        f"PATTERN 📈 — {symbol}",
        f"  Second cross of {threshold:g} within the last {horizon.fresh_label}"
        f" on the {horizon.label} chart",
        f"  {money}{ccy} · no fair value exists for this, so the pattern is"
        f" the only evidence",
        f"  {url}",
    ])


def issue_title(symbol: str, discount, horizon) -> str:
    """One line, because this is what lands in the email subject."""
    gap = f" — {discount * 100:.0f}% below fair value" if discount is not None else ""
    return f"🚀 {symbol} strong buy on the {horizon.label} chart{gap}"


def pattern_issue_title(symbol: str, horizon) -> str:
    """The same distinction in the email subject, where it is read fastest."""
    return f"📈 {symbol} RSI pattern on the {horizon.label} chart — no valuation"


NTFY_SERVER = "https://ntfy.sh"


def send_push(title: str, message: str, url: str = "") -> bool:
    """Push to every phone subscribed to the ntfy topic.

    The scrappy answer to "can several people get this on their phone without
    us building an app": everyone installs ntfy, subscribes to one topic, and
    a plain HTTP POST from CI reaches all of them. No account, no device
    registry, no server of ours.

    The topic name *is* the credential -- ntfy has no other access control, so
    anyone who knows it can read the alerts (and post to them). It lives in
    `SCREENER_NTFY_TOPIC`, and on this public repository an Actions secret is
    readable by anyone who can push a workflow, exactly like the webhook URL.
    Worth knowing rather than worrying about: the worst a leak buys is reading
    which stocks the screener likes, and rotating it is a settings edit plus a
    re-subscribe.

    Returns False when no topic is configured, which is the normal case on a
    laptop.
    """
    topic = os.environ.get("SCREENER_NTFY_TOPIC")
    if not topic:
        return False
    # Headers rather than a JSON body: the plain-text form is ntfy's simplest
    # API, and it keeps the message readable in the notification shade exactly
    # as composed. Non-ASCII has to go, though -- headers are latin-1 on the
    # wire, and the title is where the rocket would land.
    headers = {"Title": _ascii(title), "Tags": "rocket", "Priority": "high"}
    if url:
        headers["Click"] = url
    try:
        response = requests.post(
            f"{NTFY_SERVER}/{topic}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        print(f"  ! push failed: {exc}")
        return False


# Punctuation worth keeping in some form rather than deleting. The titles here
# come from `issue_title`, which sets its clauses off with an em dash; dropping
# it outright leaves two spaces where the pause should be.
_TRANSLITERATE = str.maketrans({"—": "-", "–": "-", "’": "'", "“": '"', "”": '"'})


def _ascii(text: str) -> str:
    """Reduce a title to what an HTTP header can actually carry.

    Headers are latin-1 on the wire, so the rocket emoji `issue_title` opens
    with raises UnicodeEncodeError inside requests and takes the whole
    notification down with it. Emoji are dropped; punctuation that carries
    meaning is transliterated first.
    """
    plain = text.translate(_TRANSLITERATE).encode("ascii", "ignore").decode("ascii")
    return " ".join(plain.split()) or "Strong buy"


def issue_marker(key: str) -> str:
    """A machine-readable fingerprint of the signal, hidden in the issue body.

    An HTML comment: invisible both on github.com and in the notification
    email, but readable back out of the API. Matching on this rather than on
    the title matters because the title quotes a discount that moves with the
    price -- the same signal an hour later would read "48%" instead of "49%"
    and a title match would miss it.
    """
    return f"<!-- screener-signal: {key} -->"


def _issue_exists(repo: str, token: str, marker: str) -> bool:
    """Has this exact signal already been filed?

    The ledger in `screener.notified` is the first line of defence; this is the
    second, and it is the authoritative one, because the thing being deduped is
    the issue itself. If the ledger is ever lost -- a run that dies before its
    commit, a rebase that drops the file -- GitHub still remembers, and the
    friend still gets one email instead of two.

    Deliberately the *list* endpoint rather than the search API: search is
    served from an index that lags issue creation by minutes, and runs land
    half an hour apart. `state=all` so a closed issue is not reopened as a
    duplicate; the list is newest-first, and 100 covers far more than the
    handful of days a pattern stays fresh.
    """
    response = requests.get(
        f"https://api.github.com/repos/{repo}/issues",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        params={"state": "all", "per_page": 100},
        timeout=15,
    )
    response.raise_for_status()
    return any(marker in (issue.get("body") or "") for issue in response.json())


def send_github_issue(title: str, body: str, key: str | None = None) -> bool:
    """Open an issue, so GitHub emails whoever watches the repository.

    The one notification path that costs no credential of ours. Actions
    injects GITHUB_TOKEN itself, scoped to this repository and expiring with
    the run, so nothing has to be stored anywhere. That matters here: the
    repository is public, and an Actions secret is readable by anyone who can
    push a workflow -- which includes any collaborator.

    With a `key`, the issue carries a hidden marker and an existing issue with
    the same marker suppresses a second one: the same signal never lands in the
    inbox twice.

    Needs `issues: write` on the job (and `read` for the duplicate check, which
    the same scope covers). Returns False when the environment does not supply
    a token, which is the normal case on a laptop, and when a duplicate was
    suppressed -- in both cases nothing was sent.
    """
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        return False
    if key is not None:
        marker = issue_marker(key)
        body = f"{body}\n\n{marker}"
        try:
            if _issue_exists(repo, token, marker):
                print("  (already filed as an issue — not sending again)")
                return False
        except requests.RequestException as exc:
            # Can't confirm, so err towards telling him. A duplicate email is
            # an annoyance; a missed strong buy is the whole point of this.
            print(f"  ! could not check for a duplicate issue ({exc}) — filing anyway")
    try:
        response = requests.post(
            f"https://api.github.com/repos/{repo}/issues",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"title": title, "body": body},
            timeout=15,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        print(f"  ! issue failed: {exc}")
        return False
