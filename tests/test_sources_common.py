"""Shared acquisition helpers (``far_aim.sources.common``).

The CDN neutralisation helpers were added after the first scheduled
``upstream-sync`` run (2026-09-05) failed its AIM re-verification: every
FAA HTML page carried an Akamai bot-manager script pair whose values differed
between downloads, and figure downloads returned a mix of stale and fresh
edge-cache copies after a silent FAA re-upload.
"""

from __future__ import annotations

from far_aim.sources.common import (
    CACHE_BUST_PARAM,
    cache_busted,
    download_nonce,
    strip_cdn_injection,
)

# Verbatim shapes of the injection as served by www.faa.gov (values vary).
INJECTION = (
    b'<script >bazadebezolkohpepadr="1577209189"</script>'
    b'<script type="text/javascript" src="https://www.faa.gov/akam/13/5e024c0a"  defer></script>'
)
PIXEL = (
    b'<noscript><img src="https://www.faa.gov/akam/13/pixel_5e024d88?a=dD1lZmEyZmQzNTU4MTUxMGU0"'
    b' style="visibility: hidden; position: absolute; left: -999px; top: -999px;" /></noscript>'
)
PAGE = (
    b'<html lang="en-us"><head><meta charset="UTF-8"><title>T</title>'
    b'<link rel="stylesheet" href="./commonltr.css">' + INJECTION + b"</head>"
    b'<body><script src="./atc-nav.js"></script><main><p class="paragraph-title">4-1-1.</p>'
    b'<noscript><img src="./images/f.png" alt="figure"></noscript></main>'
    + PIXEL
    + b"</body></html>"
)
CLEAN = PAGE.replace(INJECTION, b"").replace(PIXEL, b"")


def test_strip_removes_injected_elements_and_nothing_else():
    stripped = strip_cdn_injection(PAGE)
    assert b"bazadebezolkohpepadr" not in stripped
    assert b"/akam/" not in stripped
    assert stripped == CLEAN
    # The site's own scripts and noscript content are page content as served.
    assert b'<script src="./atc-nav.js"></script>' in stripped
    assert b'<noscript><img src="./images/f.png" alt="figure"></noscript>' in stripped


def test_strip_is_value_independent_and_idempotent():
    other = (
        PAGE.replace(b"1577209189", b"1974812660")
        .replace(b"5e024c0a", b"75b53f27")
        .replace(b"pixel_5e024d88?a=dD1lZmEyZmQzNTU4MTUxMGU0", b"pixel_5ecd932a?a=dD01MTk2MDZlNDZm")
    )
    assert strip_cdn_injection(other) == CLEAN
    assert strip_cdn_injection(CLEAN) == CLEAN


def test_cache_busted_appends_one_off_parameter():
    url = "https://www.faa.gov/air_traffic/publications/atpubs/aim_html/images/a.png"
    assert cache_busted(url, "abcd") == f"{url}?{CACHE_BUST_PARAM}=abcd"
    assert cache_busted(url + "?x=1", "abcd") == f"{url}?x=1&{CACHE_BUST_PARAM}=abcd"


def test_download_nonce_is_fresh_per_download():
    nonces = {download_nonce() for _ in range(16)}
    assert len(nonces) == 16
    assert all(len(n) == 16 and set(n) <= set("0123456789abcdef") for n in nonces)
