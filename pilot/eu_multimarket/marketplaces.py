"""Per-marketplace config for the EU multi-market pilot.

Covers every EU Amazon marketplace except the UK and Ireland (excluded
on shipping-cost grounds when this was scoped): Sweden, Germany, France,
Spain, Netherlands, Belgium, Italy, Poland.

LANGUAGE MODEL — the thing that used to be wrong
------------------------------------------------
track_delivery_multi.py navigates to https://www.<domain>/-/en/dp/<asin>,
where "/-/en/" is Amazon's language-override path segment: the rendered
page text is *English*, not the market's native language. But every
market except "se" used to declare only native month names ("januar",
"février", "gennaio") and only native signal phrases — so on an English
page nothing could ever match, the date-pattern search failed, the
signal search failed, and the result was UNKNOWN. "se" was the sole
market whose `months` dict happened to also contain the English month
names and whose signal phrases were already English, which is exactly
why "se" was the only market returning real dates.

The fix is to stop guessing which language wins. Every market now gets
the union of ENGLISH_* and its native tables:

  * If the "/-/en/" override works (the intended case), the English
    entries match.
  * If Amazon ignores it for some market/session and serves native text
    anyway, the native entries match.

Either way we parse it. `locale` is also pinned to "en-<CC>" for every
market so the Accept-Language header agrees with the URL instead of
fighting it (it used to say de-DE while the URL said English, which is
very likely why some markets were flaky rather than uniformly dead).

Native NO_DATE_SIGNALS / OUT_OF_STOCK_SIGNALS remain best-effort
translations and are still unverified against real pages in those
locales — but they are now a *fallback* behind verified English
phrasing, not the only thing standing between us and UNKNOWN.
"""

# ---------------------------------------------------------------------------
# Shared English tables — applied to EVERY market (see module docstring).
# ---------------------------------------------------------------------------

# Amazon abbreviates month names in delivery promises fairly often
# ("Wednesday, 6 Aug"), so the abbreviations are first-class entries and
# not just a nicety. build_date_pattern() alternates longest-name-first,
# so "aug" can never shadow "august".
ENGLISH_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# NOTE: a bare "release date" is deliberately NOT in this list, even
# though the production .se scraper has it. Practically every Amazon toy
# listing carries a "Release date : 1 August 2026" row in its product
# details table, so a bare match classifies perfectly normal pre-orders
# — including ones that DO have a delivery estimate — as "NO DATE YET".
# track_delivery_multi.py additionally scopes signal matching to the
# buybox/availability region rather than the whole page, which is the
# real defence; dropping the bare phrase is belt-and-braces.
ENGLISH_NO_DATE_SIGNALS = [
    "release date has not been announced",
    "date has not been announced",
    "not yet available",
    "coming soon",
    "this item cannot be shipped",
    "currently unavailable",
    "we don't know when or if this item will be back in stock",
]

ENGLISH_OUT_OF_STOCK_SIGNALS = [
    "temporarily out of stock",
    "we are working hard to be back in stock",
    "in stock soon",
]


_MARKETPLACES = {
    "se": {
        "domain": "amazon.se",
        "country": "Sweden",
        "locale": "en-SE",
        "native_months": {
            "januari": 1, "februari": 2, "mars": 3, "april": 4, "maj": 5, "juni": 6,
            "juli": 7, "augusti": 8, "september": 9, "oktober": 10, "november": 11, "december": 12,
        },
        "native_no_date_signals": [
            "releasedatum", "kommer snart", "inte tillgänglig ännu",
        ],
        "native_out_of_stock_signals": [
            "tillfälligt slut i lager",
        ],
    },
    "de": {
        "domain": "amazon.de",
        "country": "Germany",
        "locale": "en-DE",
        "native_months": {
            "januar": 1, "februar": 2, "märz": 3, "april": 4, "mai": 5, "juni": 6,
            "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12,
        },
        # UNVERIFIED — guessed phrasing, kept as a fallback behind English.
        "native_no_date_signals": [
            "demnächst verfügbar", "derzeit nicht verfügbar",
            "veröffentlichungstermin noch nicht bekannt",
        ],
        "native_out_of_stock_signals": [
            "zurzeit nicht auf lager", "wir arbeiten daran",
        ],
    },
    "fr": {
        "domain": "amazon.fr",
        "country": "France",
        "locale": "en-FR",
        "native_months": {
            "janvier": 1, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
            "juillet": 7, "août": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
        },
        # UNVERIFIED — guessed phrasing, kept as a fallback behind English.
        "native_no_date_signals": [
            "bientôt disponible", "actuellement indisponible",
            "date de sortie non communiquée",
        ],
        "native_out_of_stock_signals": [
            "temporairement en rupture de stock", "nous faisons tout notre possible",
        ],
    },
    "es": {
        "domain": "amazon.es",
        "country": "Spain",
        "locale": "en-ES",
        "native_months": {
            "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
            "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
            "noviembre": 11, "diciembre": 12,
        },
        # UNVERIFIED — guessed phrasing, kept as a fallback behind English.
        "native_no_date_signals": [
            "próximamente disponible", "aún no disponible",
            "fecha de lanzamiento no anunciada",
        ],
        "native_out_of_stock_signals": [
            "temporalmente sin stock", "estamos trabajando para que vuelva a estar disponible",
        ],
    },
    "nl": {
        "domain": "amazon.nl",
        "country": "Netherlands",
        "locale": "en-NL",
        "native_months": {
            "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6,
            "juli": 7, "augustus": 8, "september": 9, "oktober": 10, "november": 11, "december": 12,
        },
        # UNVERIFIED — guessed phrasing, kept as a fallback behind English.
        "native_no_date_signals": [
            "binnenkort beschikbaar", "nog niet beschikbaar",
            "releasedatum nog niet bekend",
        ],
        "native_out_of_stock_signals": [
            "tijdelijk niet op voorraad", "we werken eraan om dit artikel weer op voorraad te krijgen",
        ],
    },
    "be": {
        # Amazon Belgium's actual domain, not amazon.be.
        "domain": "amazon.com.be",
        "country": "Belgium",
        "locale": "en-BE",
        # Belgium's site is bilingual, so both Dutch and French month
        # names are in play if the English override ever fails there.
        "native_months": {
            "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6,
            "juli": 7, "augustus": 8, "september": 9, "oktober": 10, "november": 11, "december": 12,
            "janvier": 1, "février": 2, "mars": 3, "avril": 4, "juin": 6,
            "juillet": 7, "août": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
        },
        # UNVERIFIED — Dutch + French guesses, kept as a fallback behind English.
        "native_no_date_signals": [
            "binnenkort beschikbaar", "nog niet beschikbaar", "releasedatum nog niet bekend",
            "bientôt disponible", "actuellement indisponible",
        ],
        "native_out_of_stock_signals": [
            "tijdelijk niet op voorraad", "we werken eraan om dit artikel weer op voorraad te krijgen",
            "temporairement en rupture de stock",
        ],
    },
    "it": {
        "domain": "amazon.it",
        "country": "Italy",
        "locale": "en-IT",
        "native_months": {
            "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6,
            "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
        },
        # UNVERIFIED — guessed phrasing, kept as a fallback behind English.
        "native_no_date_signals": [
            "disponibile a breve", "non ancora disponibile",
            "data di uscita non ancora annunciata",
        ],
        "native_out_of_stock_signals": [
            "temporaneamente non disponibile", "stiamo lavorando per renderlo di nuovo disponibile",
        ],
    },
    "pl": {
        "domain": "amazon.pl",
        "country": "Poland",
        "locale": "en-PL",
        # Polish dates decline the month into the genitive — a date is
        # written "12 sierpnia", never "12 sierpień". The nominative
        # forms this table used to hold exclusively therefore could not
        # match a single real date. Both forms are listed now.
        "native_months": {
            "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
            "lipca": 7, "sierpnia": 8, "września": 9, "października": 10,
            "listopada": 11, "grudnia": 12,
            "styczeń": 1, "luty": 2, "marzec": 3, "kwiecień": 4, "czerwiec": 6,
            "lipiec": 7, "sierpień": 8, "wrzesień": 9, "październik": 10,
            "listopad": 11, "grudzień": 12,
        },
        # UNVERIFIED — guessed phrasing, kept as a fallback behind English.
        "native_no_date_signals": [
            "wkrótce dostępny", "obecnie niedostępny", "data premiery nieznana",
        ],
        "native_out_of_stock_signals": [
            "chwilowo niedostępny", "pracujemy nad tym aby produkt był ponownie dostępny",
        ],
    },
}


def _merge(config: dict) -> dict:
    """Fold the shared English tables together with a market's native ones.

    English wins on key collisions, but every collision that exists today
    is between two spellings that mean the same month (e.g. Dutch/French
    "mars" == 3 either way), so the merge order is not load-bearing.
    """
    merged = dict(config)
    merged["months"] = {**config["native_months"], **ENGLISH_MONTHS}
    merged["no_date_signals"] = ENGLISH_NO_DATE_SIGNALS + config["native_no_date_signals"]
    merged["out_of_stock_signals"] = (
        ENGLISH_OUT_OF_STOCK_SIGNALS + config["native_out_of_stock_signals"]
    )
    return merged


MARKETPLACES = {code: _merge(config) for code, config in _MARKETPLACES.items()}
