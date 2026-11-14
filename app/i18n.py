"""Lokalizace webu, API a e-mailů (katalogy v ``configs/i18n/<jazyk>.json``).

Čeština je zdroj, angličtina základ pro ostatní jazyky. Fallback: jazyk → en → cs → klíč.
Volba jazyka pro požadavek: ``?lang=`` → cookie ``lang`` → výchozí jazyk domény (``SITES``)
→ ``Accept-Language`` → ``DEFAULT_LANG``. Bez ``SITES`` a ``LANGS`` se chová jako dřív (cs).

Doména (``Host``) určuje jen výchozí jazyk a základ odkazů; hodnota se bere
výhradně ze seznamu ``SITES`` (allowlist), nikdy z hlavičky přímo.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from html import escape as html_escape
from pathlib import Path
from typing import Any, Mapping

CATALOG_DIR = Path(__file__).resolve().parent.parent / "configs" / "i18n"
SOURCE_LANG = "cs"
BASE_LANG = "en"
LANG_COOKIE = "lang"
LANG_COOKIE_MAX_AGE = 365 * 24 * 3600
# Prefixy klíčů, které server vloží do stránky pro app.js.
WEB_PREFIXES = ("web.", "step.", "status.")
# Texty stránky vykreslené serverem (``{{h:…}}``) do app.js nejdou.
SERVER_ONLY_PREFIXES = ("web.page.",)

_LANG_RE = re.compile(r"^[a-z]{2}$")
_PLACEHOLDER_RE = re.compile(r"\{\{(t|h):([a-zA-Z0-9_.\-]+)\}\}")
_LOCALES = {"cs": "cs-CZ", "en": "en-GB", "de": "de-DE", "pl": "pl-PL", "fr": "fr-FR", "sk": "sk-SK"}


@dataclass(frozen=True)
class Site:
    host: str
    lang: str
    base_url: str
    # free = dnešní bezplatná verze (pevný jazyk, bez přepínače), paid = placená mezinárodní verze.
    # Režim jen ovlivňuje zobrazení; oprávnění (země, placené limity) se nesmí opírat o doménu.
    mode: str = "free"
    countries: tuple[str, ...] = ("CZ",)

    @property
    def fixed_lang(self) -> bool:
        return self.mode == "free"


def available_langs() -> tuple[str, ...]:
    """Jazyky, pro které existuje katalog (cs vždy první)."""
    found = sorted(p.stem for p in CATALOG_DIR.glob("*.json") if _LANG_RE.match(p.stem))
    rest = [code for code in found if code != SOURCE_LANG]
    return (SOURCE_LANG, *rest)


def _env_list(name: str) -> list[str]:
    return [part.strip().lower() for part in os.environ.get(name, "").split(",") if part.strip()]


def default_lang() -> str:
    code = os.environ.get("DEFAULT_LANG", SOURCE_LANG).strip().lower()
    return code if code in available_langs() else SOURCE_LANG


def offered_langs(site: Site | None = None) -> tuple[str, ...]:
    """Jazyky v přepínači (env ``LANGS``, výchozí jen cs → přepínač se neukáže)."""
    if site is not None and site.fixed_lang:
        return (site.lang,)
    have = available_langs()
    wanted = [code for code in _env_list("LANGS") if code in have]
    if not wanted:
        return (default_lang(),)
    return tuple(dict.fromkeys(wanted))


SITE_MODES = ("free", "paid")


def sites() -> dict[str, Site]:
    """``SITES=host=jazyk[:režim[:země]][,…]``; základ odkazů ``https://<host>``.

    Režim ``free`` (výchozí) / ``paid``, země jako ISO kódy oddělené ``+`` nebo ``*`` (všechny),
    např. ``podkladarna.kibos.link=cs:free:CZ,example.eu=en:paid:*``.
    """
    out: dict[str, Site] = {}
    for item in os.environ.get("SITES", "").split(","):
        if "=" not in item:
            continue
        host, spec = (part.strip() for part in item.split("=", 1))
        host = host.lower().removeprefix("https://").removeprefix("http://").rstrip("/")
        lang, mode, countries = ([*spec.split(":"), "", ""][:3])
        lang = lang.strip().lower()
        mode = mode.strip().lower() or "free"
        codes = tuple(c.strip().upper() for c in (countries or "CZ").split("+") if c.strip()) or ("CZ",)
        if not host or lang not in available_langs() or mode not in SITE_MODES:
            continue
        out[host] = Site(host=host, lang=lang, base_url=f"https://{host}", mode=mode, countries=codes)
    return out


def site_base_url(site_key: str | None) -> str | None:
    """Základ odkazů pro uloženou doménu jobu (jen ze seznamu SITES)."""
    site = sites().get((site_key or "").strip().lower())
    return site.base_url if site else None


def _request_host(request: Any) -> str:
    host = (request.headers.get("host") or "").strip().lower()
    return host.split(":", 1)[0]


def site_for_request(request: Any) -> Site | None:
    return sites().get(_request_host(request))


@lru_cache(maxsize=None)
def _catalog_cached(lang: str, mtime: float) -> dict[str, str]:
    path = CATALOG_DIR / f"{lang}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in data.items() if not str(k).startswith("_")}


def catalog(lang: str) -> dict[str, str]:
    path = CATALOG_DIR / f"{lang}.json"
    if not _LANG_RE.match(lang or "") or not path.is_file():
        return {}
    return _catalog_cached(lang, path.stat().st_mtime)


def _chain(lang: str) -> list[str]:
    return list(dict.fromkeys([lang, BASE_LANG, SOURCE_LANG]))


def t(key: str, lang: str | None = None, **params: Any) -> str:
    """Text pro klíč v jazyce; chybí-li, en, pak cs, nakonec klíč."""
    for code in _chain(lang or default_lang()):
        text = catalog(code).get(key)
        if text is not None:
            return text.format_map(_SafeDict(params)) if params else text
    return key


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def merged_catalog(lang: str, prefixes: tuple[str, ...] = WEB_PREFIXES) -> dict[str, str]:
    """Klíče s prefixem v jazyce s doplněním z en a cs (pro app.js)."""
    out: dict[str, str] = {}
    for code in reversed(_chain(lang)):
        out.update(
            {
                k: v
                for k, v in catalog(code).items()
                if k.startswith(prefixes) and not k.startswith(SERVER_ONLY_PREFIXES)
            }
        )
    return out


def locale_for(lang: str) -> str:
    return _LOCALES.get(lang, lang)


def _accept_language(header: str, allowed: tuple[str, ...]) -> str | None:
    best: tuple[float, str] | None = None
    for part in (header or "").split(","):
        bits = part.strip().split(";")
        code = bits[0].strip().lower().split("-", 1)[0]
        if code not in allowed:
            continue
        q = 1.0
        for bit in bits[1:]:
            if bit.strip().startswith("q="):
                try:
                    q = float(bit.strip()[2:])
                except ValueError:
                    q = 0.0
        if best is None or q > best[0]:
            best = (q, code)
    return best[1] if best and best[0] > 0 else None


def resolve_lang(request: Any) -> tuple[str, bool]:
    """(jazyk, nastavit cookie). Cookie jen při platném ``?lang=``."""
    site = site_for_request(request)
    if site is not None and site.fixed_lang:
        return site.lang, False
    allowed = available_langs()
    asked = (request.query_params.get("lang") or "").strip().lower()
    if asked in allowed:
        return asked, True
    cookie = (request.cookies.get(LANG_COOKIE) or "").strip().lower()
    if cookie in allowed:
        return cookie, False
    if site is not None:
        return site.lang, False
    from_header = _accept_language(request.headers.get("accept-language", ""), offered_langs())
    if from_header and len(offered_langs()) > 1:
        return from_header, False
    return default_lang(), False


def set_lang_cookie(response: Any, lang: str, *, secure: bool) -> None:
    response.set_cookie(
        LANG_COOKIE,
        lang,
        max_age=LANG_COOKIE_MAX_AGE,
        path="/",
        samesite="lax",
        secure=secure,
        httponly=False,
    )


def render_placeholders(html: str, lang: str) -> str:
    """``{{t:klic}}`` v HTML → escapovaný text, ``{{h:klic}}`` → HTML z katalogu beze změny.

    ``h`` jen pro texty stránek (katalog je obsah repozitáře, ne vstup uživatele).
    """

    def repl(m: re.Match[str]) -> str:
        text = t(m.group(2), lang)
        return text if m.group(1) == "h" else html_escape(text, quote=True)

    return _PLACEHOLDER_RE.sub(repl, html)


def lang_switcher_html(lang: str, site: Site | None = None) -> str:
    """Odkazy ``?lang=`` na nabízené jazyky; při jediném jazyku (i bezplatná doména) prázdné."""
    langs = offered_langs(site)
    if len(langs) < 2:
        return ""
    items = []
    for code in langs:
        label = html_escape(t("web.lang.name", code))
        if code == lang:
            items.append(f'<strong lang="{code}" aria-current="true">{label}</strong>')
        else:
            items.append(f'<a href="?lang={code}" lang="{code}" hreflang="{code}">{label}</a>')
    title = html_escape(t("web.lang.switch", lang), quote=True)
    return f'<nav class="lang-switch" aria-label="{title}">' + " · ".join(items) + "</nav>"


def alternate_links_html(request: Any) -> str:
    """``hreflang`` alternativy podle ``SITES`` (jedna doména = jeden jazyk)."""
    entries = sites()
    if len(entries) < 2:
        return ""
    path = request.url.path or "/"
    lines = [
        f'<link rel="alternate" hreflang="{s.lang}" href="{html_escape(s.base_url + path, quote=True)}" />'
        for s in entries.values()
    ]
    site = site_for_request(request)
    if site is not None:
        lines.append(f'<link rel="canonical" href="{html_escape(site.base_url + path, quote=True)}" />')
    return "\n  ".join(lines)


def localize_page(html: str, request: Any, lang: str, *, web_catalog: bool = False) -> str:
    """Jazyk stránky: ``<html lang>``, zástupky, přepínač, hreflang, případně katalog pro app.js."""
    html = re.sub(r'<html lang="[a-z]{2}"', f'<html lang="{lang}"', html, count=1)
    html = html.replace("<!-- PODKLADARNA_LANG_SWITCH -->", lang_switcher_html(lang, site_for_request(request)))
    head_extra = alternate_links_html(request)
    if web_catalog:
        payload = {"lang": lang, "locale": locale_for(lang), "messages": merged_catalog(lang)}
        data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
        script = f'<script data-cfasync="false">window.I18N = {data};</script>'
        head_extra = f"{head_extra}\n  {script}" if head_extra else script
    if head_extra:
        html = html.replace("</head>", f"  {head_extra}\n</head>", 1)
    return render_placeholders(html, lang)


def placeholders_in(text: str) -> set[str]:
    return set(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", text))


def missing_keys(reference: Mapping[str, str], other: Mapping[str, str]) -> list[str]:
    return sorted(k for k in reference if k not in other)
