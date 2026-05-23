"""
Indigenous bird names + territory acknowledgment.

Two responsibilities:

1. **Territory lookup** — given lat/lon, query Native Land Digital's public API to
   surface whose traditional territory the listener is standing on. Results
   include nation slugs and Native Land's own descriptive name for each
   territory polygon overlapping the point.

2. **Indigenous name lookup** — given a species (scientific name) and an
   optional set of language codes (filtered by territory), return curated names
   from `data/indigenous_names.json`. Every entry is cited; nothing is invented.

Design principles:
- Names are *gifts from communities*, not licensed property. We surface them with
  reverence and citation, and link out to the source dictionary every time.
- When we don't have a verified name for a species in a language, we return
  nothing for that pairing. We never fabricate or interpolate.
- The dataset is intentionally small + extensible. Quality over quantity.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import httpx

DATA_PATH = Path(__file__).parent / "data" / "indigenous_names.json"
NATIVELAND_API_KEY = os.getenv("NATIVELAND_API_KEY", "").strip()

with DATA_PATH.open("r", encoding="utf-8") as f:
    DATA = json.load(f)


# Native Land Digital → which of our language codes apply.
# Multiple Native Land "language" slugs can map to one of our buckets; we
# also accept territory slugs as a fallback so a listener inside an
# Anishinaabe territory polygon picks up Ojibwe names even if Native Land's
# language layer is sparse there.
NATIVE_LAND_LANGUAGE_MAP = {
    # Anishinaabemowin family
    "anishinaabemowin": "ojibwe",
    "ojibwe": "ojibwe",
    "ojibwemowin": "ojibwe",
    "anishinaabe": "ojibwe",
    "chippewa": "ojibwe",
    "ottawa": "ojibwe",
    "odawa": "ojibwe",
    "potawatomi": "ojibwe",
    "algonquin": "ojibwe",
    "mississauga": "ojibwe",
    "saulteaux": "ojibwe",
    "nipissing": "ojibwe",
    # Haudenosaunee
    "kanienkeha": "mohawk",
    "kanien'kéha": "mohawk",
    "mohawk": "mohawk",
    "kanienkehaka": "mohawk",
    # Cree
    "cree": "cree",
    "nehiyawewin": "cree",
    "nēhiyawēwin": "cree",
    "ililimowin": "cree",
    "swampy-cree": "cree",
    "moose-cree": "cree",
    "mushkegowuk": "cree",
}


def _normalize(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "-").replace("_", "-")


# Coarse regional fallback when Native Land Digital API key is not present.
# Each entry: (lat_min, lat_max, lon_min, lon_max), language_codes, summary.
# This is intentionally conservative — broad regions where we can responsibly
# surface names from our verified dataset. When a real API key is configured
# we get per-point territory polygons instead of these rectangles.
REGIONAL_FALLBACK = [
    {
        # Ontario, broadly. Anishinaabe languages are spoken throughout.
        "name": "Ontario (Anishinaabe homelands, with Haudenosaunee and Mushkegowuk territories in the south and north respectively)",
        "bounds": (41.6, 56.9, -95.2, -74.3),
        "languages": ["ojibwe"],
        "note": "Coarse regional acknowledgment — for per-point territory boundaries, configure NATIVELAND_API_KEY (https://api-docs.native-land.ca).",
    },
]


def _regional_fallback(lat: float, lon: float) -> dict:
    out: dict = {
        "available": False,
        "territories": [],
        "languages_at_location": [],
        "source": "regional fallback (BirdNET-PWA)",
        "source_url": None,
        "note": "No Native Land Digital API key configured — using a coarse regional bucket. Get a key at https://api-docs.native-land.ca and set NATIVELAND_API_KEY for per-point territory boundaries.",
    }
    for region in REGIONAL_FALLBACK:
        lat_min, lat_max, lon_min, lon_max = region["bounds"]
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            out["available"] = True
            out["territories"].append({"name": region["name"], "slug": None, "url": None})
            out["languages_at_location"] = list(region["languages"])
            out["note"] = region.get("note", out["note"])
            return out
    return out


async def lookup_territory(lat: float, lon: float, timeout: float = 8.0) -> dict:
    """Whose territory is this lat/lon on?

    Prefers the Native Land Digital API (per-point territory polygons) when
    NATIVELAND_API_KEY is configured. Otherwise falls back to a coarse
    regional bucket so the feature degrades gracefully — and clearly says so.
    """
    if not NATIVELAND_API_KEY:
        return _regional_fallback(lat, lon)

    base = "https://native-land.ca/api/index.php"
    out: dict = {
        "available": False,
        "territories": [],
        "languages_at_location": [],
        "source": "Native Land Digital",
        "source_url": "https://native-land.ca",
        "note": "Native Land Digital is a community-driven resource and not an authority on territorial boundaries — a starting point for acknowledgment, not a final word.",
    }
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "BirdNET-PWA/0.1 (https://github.com/seanthomasevans/birdnet)"},
        ) as client:
            params_common = {"key": NATIVELAND_API_KEY, "position": f"{lat},{lon}"}
            r_t = await client.get(base, params={**params_common, "maps": "territories"})
            territories_raw = r_t.json() if r_t.status_code == 200 else []
            r_l = await client.get(base, params={**params_common, "maps": "languages"})
            languages_raw = r_l.json() if r_l.status_code == 200 else []
    except Exception as exc:
        # If the API call fails, still fall back to the regional bucket so the
        # user gets *something* respectful rather than silence.
        fallback = _regional_fallback(lat, lon)
        fallback["note"] = f"Native Land API error ({exc}); using regional fallback."
        return fallback

    lang_codes: set[str] = set()

    for poly in territories_raw or []:
        props = poly.get("properties") or {}
        name = props.get("Name") or props.get("name") or "(unnamed territory)"
        slug = props.get("Slug") or props.get("slug") or _normalize(name)
        desc_url = props.get("description") or ""
        out["territories"].append({"name": name, "slug": slug, "url": desc_url or f"https://native-land.ca/maps/territories/{slug}"})
        mapped = NATIVE_LAND_LANGUAGE_MAP.get(_normalize(slug)) or NATIVE_LAND_LANGUAGE_MAP.get(_normalize(name))
        if mapped:
            lang_codes.add(mapped)

    for poly in languages_raw or []:
        props = poly.get("properties") or {}
        name = props.get("Name") or props.get("name") or ""
        slug = props.get("Slug") or props.get("slug") or ""
        mapped = NATIVE_LAND_LANGUAGE_MAP.get(_normalize(slug)) or NATIVE_LAND_LANGUAGE_MAP.get(_normalize(name))
        if mapped:
            lang_codes.add(mapped)

    out["available"] = bool(out["territories"]) or bool(lang_codes)
    out["languages_at_location"] = sorted(lang_codes)
    # If Native Land returned nothing useful at this point, blend in the regional fallback.
    if not out["available"]:
        fb = _regional_fallback(lat, lon)
        if fb["available"]:
            out["territories"] = fb["territories"]
            out["languages_at_location"] = fb["languages_at_location"]
            out["available"] = True
            out["note"] = fb["note"] + " (Native Land returned no polygons for this point.)"
    return out


def lookup_names(scientific_name: Optional[str], language_codes: Optional[list[str]] = None) -> dict:
    """Return Indigenous names for a species, optionally filtered by language.

    If `language_codes` is None, return every language we have an entry for.
    The result includes the language metadata block so the client knows whose
    name it is and where it came from.
    """
    species_block = DATA.get("species", {}).get(scientific_name) if scientific_name else None
    if not species_block:
        return {
            "available": False,
            "scientific_name": scientific_name,
            "names": [],
            "languages": {},
            "acknowledgment": DATA.get("acknowledgment", ""),
        }

    names = species_block.get("names", [])
    if language_codes:
        wanted = {c.lower() for c in language_codes}
        names = [n for n in names if n.get("language", "").lower() in wanted]

    languages_present = sorted({n.get("language") for n in names if n.get("language")})
    languages_meta = {code: DATA["languages"][code] for code in languages_present if code in DATA.get("languages", {})}

    return {
        "available": bool(names),
        "scientific_name": scientific_name,
        "common_name": species_block.get("common_name"),
        "names": names,
        "languages": languages_meta,
        "acknowledgment": DATA.get("acknowledgment", ""),
    }


def dataset_coverage() -> dict:
    """Summary for /healthz and a future /coverage endpoint."""
    species_count = len(DATA.get("species", {}))
    by_lang: dict[str, int] = {}
    for spec in DATA.get("species", {}).values():
        for n in spec.get("names", []):
            code = n.get("language")
            if code:
                by_lang[code] = by_lang.get(code, 0) + 1
    return {
        "species_covered": species_count,
        "names_by_language": by_lang,
        "languages_documented": list(DATA.get("languages", {}).keys()),
        "version": DATA.get("version"),
    }
