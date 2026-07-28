"""Per-marketplace config for the EU multi-market pilot.

Covers every EU Amazon marketplace except the UK and Ireland (excluded
on shipping-cost grounds when this was scoped): Sweden, Germany, France,
Spain, Netherlands, Belgium, Italy, Poland.

These extend the English/Swedish handling in
scripts/track_delivery_date_playwright.py to the other markets' month
names and phrasing. Month names are correct, but NO_DATE_SIGNALS and
OUT_OF_STOCK_SIGNALS for every market except "se" are best-effort
GUESSES translated from the English phrasing — they have NOT been
confirmed against real Amazon pages in those locales. Validating and
correcting these against real output is the actual point of this pilot;
expect early runs to produce "UNKNOWN" results and debug_*.html dumps
until they're tuned.

The catalog search itself (scrape_catalog_multi.py) uses a single brand
id (p_123:219753, Hasbro) shared across all markets — see that file —
so there's no per-market brand_filter here to worry about.
"""

MARKETPLACES = {
    "se": {
        "domain": "amazon.se",
        "locale": "en-SE",
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
    "es": {
        "domain": "amazon.es",
        "locale": "es-ES",
        "months": {
            "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
            "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
        },
        # UNVERIFIED — guessed phrasing, tune from real debug_*.html output.
        "no_date_signals": [
            "fecha de lanzamiento", "próximamente disponible", "aún no disponible",
            "fecha de lanzamiento no anunciada",
        ],
        "out_of_stock_signals": [
            "temporalmente sin stock", "estamos trabajando para que vuelva a estar disponible",
        ],
    },
    "nl": {
        "domain": "amazon.nl",
        "locale": "nl-NL",
        "months": {
            "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6,
            "juli": 7, "augustus": 8, "september": 9, "oktober": 10, "november": 11, "december": 12,
        },
        # UNVERIFIED — guessed phrasing, tune from real debug_*.html output.
        "no_date_signals": [
            "releasedatum", "binnenkort beschikbaar", "nog niet beschikbaar",
            "releasedatum nog niet bekend",
        ],
        "out_of_stock_signals": [
            "tijdelijk niet op voorraad", "we werken eraan om dit artikel weer op voorraad te krijgen",
        ],
    },
    "be": {
        # Amazon Belgium's actual domain, not amazon.be.
        "domain": "amazon.com.be",
        "locale": "nl-BE",
        "months": {
            "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6,
            "juli": 7, "augustus": 8, "september": 9, "oktober": 10, "november": 11, "december": 12,
        },
        # UNVERIFIED — same guessed Dutch phrasing as "nl"; Belgium's site is
        # bilingual (Dutch/French) so this may need French phrases added too
        # once real output is seen.
        "no_date_signals": [
            "releasedatum", "binnenkort beschikbaar", "nog niet beschikbaar",
            "releasedatum nog niet bekend",
        ],
        "out_of_stock_signals": [
            "tijdelijk niet op voorraad", "we werken eraan om dit artikel weer op voorraad te krijgen",
        ],
    },
    "it": {
        "domain": "amazon.it",
        "locale": "it-IT",
        "months": {
            "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6,
            "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
        },
        # UNVERIFIED — guessed phrasing, tune from real debug_*.html output.
        "no_date_signals": [
            "data di uscita", "disponibile a breve", "non ancora disponibile",
            "data di uscita non ancora annunciata",
        ],
        "out_of_stock_signals": [
            "temporaneamente non disponibile", "stiamo lavorando per renderlo di nuovo disponibile",
        ],
    },
    "pl": {
        "domain": "amazon.pl",
        "locale": "pl-PL",
        "months": {
            "styczeń": 1, "luty": 2, "marzec": 3, "kwiecień": 4, "maj": 5, "czerwiec": 6,
            "lipiec": 7, "sierpień": 8, "wrzesień": 9, "październik": 10, "listopad": 11, "grudzień": 12,
        },
        # UNVERIFIED — guessed phrasing, tune from real debug_*.html output.
        "no_date_signals": [
            "data premiery", "wkrótce dostępny", "obecnie niedostępny",
            "data premiery nieznana",
        ],
        "out_of_stock_signals": [
            "chwilowo niedostępny", "pracujemy nad tym aby produkt był ponownie dostępny",
        ],
    },
}
