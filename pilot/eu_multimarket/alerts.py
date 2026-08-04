"""Discord message formatting for the EU multi-market pilot.

Kept separate from the scrapers so that test_discord.py exercises the
*real* formatters rather than a copy of them — a test that renders its
own idea of the message would happily pass while the thing you actually
receive is broken.

The delivery alert is deliberately **per ASIN, not per market**: if two
markets improve in the same run you get one message showing both, rather
than two messages you have to mentally join. Every market's current
state is listed even when it didn't change, so the message answers "where
should I buy this?" on its own without cross-referencing an earlier one.
"""

MAX_NAME_LENGTH = 60


def truncate(text: str, limit: int = MAX_NAME_LENGTH) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def product_url(domain: str, asin: str) -> str:
    return f"https://www.{domain}/dp/{asin}"


def short_state(state: str) -> str:
    """Drop the explanatory parenthetical from a non-date outcome.

    Stored states read "OUT OF STOCK (temporarily unavailable, no
    delivery date yet)" — useful in a log, far too long in a message
    that lists four markets. Real dates never contain a bracket, so
    they're untouched.
    """
    state = (state or "").strip()
    head, bracket, _ = state.partition(" (")
    return head if bracket and head.isupper() else state


def format_delivery_alert(asin: str, name: str, rows: list[dict]) -> str:
    """Build the per-ASIN "moved earlier" message.

    rows: one dict per market, each with
        market   market code ("fr")
        flag     country flag emoji
        domain   "amazon.fr"
        previous previous stored value, or None if we'd never seen it
        current  this run's value
        improved True if the date moved closer (including gaining a date
                 where there was none) — these get the NEW! tag and have
                 their link stacked at the bottom
        price    price string, or "" if the listing has no offer
        sort_key date for ordering, or None

    Markets that improved come first, then the rest by date, so the
    lines you can act on are at the top.
    """
    ordered = sorted(rows, key=lambda r: (not r["improved"], r["sort_key"] is None, r["sort_key"]))

    lines = [f"📦 **Delivery date moved earlier for:** `{asin}` — {truncate(name)}"]
    for row in ordered:
        # Only show an arrow where something actually changed; repeating
        # "X → X" for untouched markets is noise, and the point of
        # listing them at all is context, not history.
        previous, current = short_state(row["previous"]), short_state(row["current"])
        if row["improved"] and previous and previous != current:
            state = f"{previous} → **{current}**"
        elif row["improved"]:
            state = f"**{current}**"
        else:
            state = current
        tag = "  🆕 **NEW!**" if row["improved"] else ""
        price = f"  ·  {row['price']}" if row["price"] else ""
        lines.append(f"{row['flag']} `{row['market']}`  {state}{tag}{price}")

    # One link per improved market, in the same order as the lines, so
    # you can go straight to whichever one you want to buy from.
    for row in ordered:
        if row["improved"]:
            lines.append(product_url(row["domain"], asin))

    return "\n".join(lines)


def format_new_product_alert(market: str, config: dict, product: dict) -> str:
    """Build the "new product found" message for one discovery."""
    price = f"  ·  {product['price']}" if product.get("price") else ""
    return (
        f"🆕 **New Beyblade X product on {config['domain']}:** "
        f"`{product['asin']}` — {truncate(product['title'])}\n"
        f"{config['flag']} `{market}`{price}\n"
        f"{product_url(config['domain'], product['asin'])}"
    )
