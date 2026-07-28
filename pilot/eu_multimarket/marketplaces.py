"""Per-marketplace config for the EU multi-market pilot.

These extend the English/Swedish handling in
scripts/track_delivery_date_playwright.py to German and French. The
month names are correct, but NO_DATE_SIGNALS, OUT_OF_STOCK_SIGNALS, and
BRAND_FILTER for "de" and "fr" are best-effort GUESSES based on how the
Swedish/English site reads — they have NOT been confirmed against real
amazon.de / amazon.fr pages. Validating and correcting these against
real output is the actual point of this pilot; expect early runs to
produce "UNKNOWN" results and debug_*.html dumps until they're tuned.
"""

MARKETPLACES = {
    "se": {
        "domain": "amazon.se",
        "locale": "en-SE",
        "brand_filter": "p_89:Hasbro",
        "months": {
            "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
            "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
            "januari": 1, "februari": 2, "mars": 3, "maj": 5, "juni": 6, "juli": 7,
            "augusti": 8, "oktober": 10,
        },
        "no_date_signals": [
            "release date", "coming soon", "not yet available",
            "date has not been announced", "this item cannot be shipped",
        ],
        "out_of_stock_signals": [
            "temporarily out of stock", "tillfälligt slut i lager",
            "we are working hard to be back in stock",
        ],
    },
    "de": {
        "domain": "amazon.de",
        "locale": "de-DE",
        # UNVERIFIED — confirm this refinement id actually filters to Hasbro
        # on .de; if it 404s or returns unfiltered results, drop the rh=
        # param entirely and filter by title text instead.
        "brand_filter": "p_89:Hasbro",
        "months": {
            "januar": 1, "februar": 2, "märz": 3, "april": 4, "mai": 5, "juni": 6,
            "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12,
        },
        # UNVERIFIED — guessed phrasing, tune from real debug_*.html output.
        "no_date_signals": [
            "erscheinungsdatum", "demnächst verfügbar", "derzeit nicht verfügbar",
            "veröffentlichungstermin noch nicht bekannt",
        ],
        "out_of_stock_signals": [
            "zurzeit nicht auf lager", "wir arbeiten daran",
        ],
    },
    "fr": {
        "domain": "amazon.fr",
        "locale": "fr-FR",
        # UNVERIFIED — see the .de comment above, same caveat applies.
        "brand_filter": "p_89:Hasbro",
        "months": {
            "janvier": 1, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
            "juillet": 7, "août": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
        },
        # UNVERIFIED — guessed phrasing, tune from real debug_*.html output.
        "no_date_signals": [
            "date de publication", "bientôt disponible", "actuellement indisponible",
            "date de sortie non communiquée",
        ],
        "out_of_stock_signals": [
            "temporairement en rupture de stock", "nous faisons tout notre possible",
        ],
    },
}
