#!/usr/bin/env python3
"""
Fragments uploader for tsvetoslavshalev.com  (WordPress.com Simple site)

Pipeline:
    open image → read EXIF → reverse-geocode GPS → derive filename →
    compress + resize + strip EXIF → upload media →
    create post in 'Fragments' category (with EXIF footer) →
    suppress subscriber email.

WHY OAuth2, NOT BASIC AUTH
    The public-api.wordpress.com gateway authenticates with OAuth2 bearer tokens.
    Basic Auth with Application Passwords is silently ignored — requests arrive
    anonymous and get a 401 regardless of the account's role.

.env expected:
    WP_SITE              = tsvetoslavshalev.com
    WPCOM_ACCESS_TOKEN   = ...          # preferred: paste a long-lived token
    # OR mint one per-run with the password grant:
    WPCOM_CLIENT_ID      = 12345        # developer.wordpress.com/apps
    WPCOM_CLIENT_SECRET  = ...
    WPCOM_USERNAME       = your_wpcom_username
    WPCOM_APP_PASSWORD   = xxxx xxxx xxxx xxxx xxxx xxxx

Usage:
    python wp_uploader.py "Your blurb" photo.jpg ["optional alt text"] [--dry-run]
"""

import argparse
import html
import io
import json
import os
import sys
import time
from datetime import datetime

import piexif
import requests
from dotenv import load_dotenv
from geopy.geocoders import Nominatim
from PIL import Image, ImageOps
from slugify import slugify

load_dotenv()

WP_SITE       = os.getenv("WP_SITE", "").strip().replace("https://", "").replace("http://", "").rstrip("/")
ACCESS_TOKEN  = os.getenv("WPCOM_ACCESS_TOKEN")
CLIENT_ID     = os.getenv("WPCOM_CLIENT_ID")
CLIENT_SECRET = os.getenv("WPCOM_CLIENT_SECRET")
USERNAME      = os.getenv("WPCOM_USERNAME")
APP_PASSWORD  = os.getenv("WPCOM_APP_PASSWORD")

if not WP_SITE:
    sys.exit("Error: WP_SITE missing from .env")

API       = f"https://public-api.wordpress.com/wp/v2/sites/{WP_SITE}"
TOKEN_URL = "https://public-api.wordpress.com/oauth2/token"

FRAGMENTS_CATEGORY = "Fragments"
MAX_EDGE   = 1600
QUALITY    = 80
GEOCACHE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".geocache.json")
USER_AGENT = "fragments-uploader/1.0 (tsvetoslavshalev.com)"


# ---------- auth -----------------------------------------------------------

def get_token():
    if ACCESS_TOKEN:
        return ACCESS_TOKEN
    if not all([CLIENT_ID, CLIENT_SECRET, USERNAME, APP_PASSWORD]):
        sys.exit("Error: no WPCOM_ACCESS_TOKEN and the password-grant fields are "
                 "incomplete in .env.")
    r = requests.post(TOKEN_URL, data={
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type":    "password",
        "username":      USERNAME,
        "password":      APP_PASSWORD,
    })
    if r.status_code != 200:
        sys.exit(f"Token request failed (HTTP {r.status_code}): {r.text[:300]}")
    return r.json()["access_token"]


def auth_headers(token, extra=None):
    h = {"Authorization": f"Bearer {token}"}
    if extra:
        h.update(extra)
    return h


# ---------- EXIF -----------------------------------------------------------

def _gps_rational_to_float(coord, ref):
    """Convert GPS rational triplet + N/S/E/W reference to a signed decimal degree."""
    if not coord or not ref:
        return None
    try:
        d = coord[0][0] / coord[0][1]
        m = coord[1][0] / coord[1][1]
        s = coord[2][0] / coord[2][1]
        value = d + m / 60 + s / 3600
        ref_str = ref.decode() if isinstance(ref, bytes) else str(ref)
        if ref_str.upper() in ("S", "W"):
            value = -value
        return value
    except (TypeError, ZeroDivisionError, IndexError):
        return None


def _rational(ifd, tag):
    """Return a float from a piexif rational (num, den) tuple, or None."""
    val = ifd.get(tag)
    if val is None:
        return None
    if isinstance(val, tuple) and len(val) == 2 and val[1] != 0:
        return val[0] / val[1]
    if isinstance(val, (int, float)):
        return float(val)
    return None


def read_exif(image_path):
    """
    Read EXIF from the image BEFORE compression strips it.

    Returns a dict with any subset of:
        datetime        datetime object
        datetime_source str — which tag (or 'file mtime') was used
        gps             (lat, lon) floats
        camera          str — make + model, deduplicated
        fnumber         float
        exposure        str — e.g. '1/12310'
        focal_length    float — mm
        iso             int

    All fields are optional; missing fields are simply absent.
    """
    meta = {}
    try:
        img = Image.open(image_path)
        raw = img.info.get("exif")
        if not raw:
            print("  No EXIF data found.", file=sys.stderr)
            return meta
        exif = piexif.load(raw)

        ifd0     = exif.get("0th", {})
        exif_ifd = exif.get("Exif", {})
        gps_ifd  = exif.get("GPS", {})

        # --- datetime (fallback chain) ---
        dto_bytes = exif_ifd.get(piexif.ExifIFD.DateTimeOriginal)
        dt_bytes  = ifd0.get(piexif.ImageIFD.DateTime)

        raw_dt, dt_source = None, None
        if dto_bytes:
            raw_dt, dt_source = dto_bytes.decode(errors="ignore").strip("\x00"), "DateTimeOriginal"
        elif dt_bytes:
            raw_dt, dt_source = dt_bytes.decode(errors="ignore").strip("\x00"), "DateTime"

        if raw_dt:
            try:
                meta["datetime"] = datetime.strptime(raw_dt, "%Y:%m:%d %H:%M:%S")
                meta["datetime_source"] = dt_source
            except ValueError:
                print(f"  Warning: could not parse EXIF datetime '{raw_dt}'.", file=sys.stderr)

        if "datetime" not in meta:
            meta["datetime"] = datetime.fromtimestamp(os.path.getmtime(image_path))
            meta["datetime_source"] = "file mtime"

        # --- GPS ---
        if gps_ifd:
            lat = _gps_rational_to_float(
                gps_ifd.get(piexif.GPSIFD.GPSLatitude),
                gps_ifd.get(piexif.GPSIFD.GPSLatitudeRef),
            )
            lon = _gps_rational_to_float(
                gps_ifd.get(piexif.GPSIFD.GPSLongitude),
                gps_ifd.get(piexif.GPSIFD.GPSLongitudeRef),
            )
            if lat is not None and lon is not None:
                meta["gps"] = (lat, lon)

        # --- camera make + model ---
        def _tag_str(tag):
            val = ifd0.get(tag, b"")
            return (val.decode(errors="ignore") if isinstance(val, bytes) else str(val)).strip().strip("\x00")

        make  = _tag_str(piexif.ImageIFD.Make)
        model = _tag_str(piexif.ImageIFD.Model)
        if make and model:
            # Avoid "Nothing Nothing Phone (2a) Plus" when model already starts with make
            meta["camera"] = model if model.lower().startswith(make.lower()) else f"{make} {model}"
        elif model:
            meta["camera"] = model
        elif make:
            meta["camera"] = make

        # --- optics ---
        fn = _rational(exif_ifd, piexif.ExifIFD.FNumber)
        if fn is not None:
            meta["fnumber"] = fn

        exp = exif_ifd.get(piexif.ExifIFD.ExposureTime)
        if isinstance(exp, tuple) and len(exp) == 2 and exp[1] != 0:
            secs = exp[0] / exp[1]
            # Normalise: Android stores fast speeds as large unsimplified rationals
            meta["exposure"] = f"1/{round(1 / secs)}" if secs < 1 else f"{secs:.1f}"

        fl = _rational(exif_ifd, piexif.ExifIFD.FocalLength)
        if fl is not None:
            meta["focal_length"] = fl

        iso = exif_ifd.get(piexif.ExifIFD.ISOSpeedRatings)
        if iso is not None:
            meta["iso"] = iso[0] if isinstance(iso, (list, tuple)) else iso

    except Exception as e:
        print(f"  Warning: EXIF read failed ({e}); continuing without metadata.", file=sys.stderr)

    return meta


# ---------- geocoding ------------------------------------------------------

def _load_geocache():
    try:
        with open(GEOCACHE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_geocache(cache):
    with open(GEOCACHE, "w") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def reverse_geocode(lat, lon):
    """
    Return (locality, display_name) strings, or (None, None) on any failure.
    Results cached in .geocache.json keyed by coords rounded to 2 dp (~1 km).
    Sleeps 1 s before any live request to respect Nominatim's rate limit.
    """
    key = f"{round(lat, 2)},{round(lon, 2)}"
    cache = _load_geocache()
    if key in cache:
        return cache[key]["locality"], cache[key]["display_name"]

    try:
        time.sleep(1)
        geolocator = Nominatim(user_agent=USER_AGENT)
        location = geolocator.reverse((lat, lon), language="en", timeout=10)
    except Exception as e:
        print(f"  Warning: reverse geocoding failed: {e}", file=sys.stderr)
        return None, None

    if not location:
        print("  Warning: reverse geocoding returned no result.", file=sys.stderr)
        return None, None

    addr = location.raw.get("address", {})
    locality = (
        addr.get("city")
        or addr.get("town")
        or addr.get("village")
        or addr.get("suburb")
        or addr.get("county")
        or addr.get("state")
    )
    country      = addr.get("country", "")
    display_name = (
        f"{locality}, {country}" if locality and country
        else locality or country or location.address
    )

    cache[key] = {"locality": locality, "display_name": display_name}
    _save_geocache(cache)
    return locality, display_name


# ---------- filename -------------------------------------------------------

def make_filename(original_stem, exif_meta, locality):
    """
    {locality_slug}_{YYYYMMDD}_{HHMM}.webp   — with GPS
    {YYYYMMDD}_{HHMM}.webp                   — with datetime but no GPS
    {original_stem}.webp                      — last resort
    """
    dt        = exif_meta.get("datetime")
    date_part = dt.strftime("%Y%m%d_%H%M") if dt else None

    if locality and date_part:
        loc_slug = slugify(locality, separator="_", lowercase=True)
        return f"{loc_slug}_{date_part}.webp"
    if date_part:
        return f"{date_part}.webp"
    return f"{original_stem}.webp"


# ---------- Gutenberg block helpers ----------------------------------------

def _block(name, attrs, inner_html):
    """
    Emit a Gutenberg block comment pair that matches "Copy as HTML" output.
    attrs must be a dict; json.dumps ensures correct serialisation with no
    extra whitespace, matching the format the block editor itself writes.
    """
    if attrs:
        attr_str = json.dumps(attrs, separators=(",", ":"), ensure_ascii=False)
        open_tag = f"<!-- wp:{name} {attr_str} -->"
    else:
        open_tag = f"<!-- wp:{name} -->"
    return f"{open_tag}\n{inner_html}\n<!-- /wp:{name} -->"


# ---------- EXIF footer ----------------------------------------------------

def build_exif_footer(exif_meta, display_name):
    """
    Return a Gutenberg wp:separator + wp:paragraph block string, or '' if
    there is nothing to show. Only present fields are included — 'None' is
    never rendered.
    """
    parts = []

    if display_name:
        parts.append(f"📍 {display_name}")

    dt = exif_meta.get("datetime")
    if dt:
        parts.append(f"{dt.day} {dt.strftime('%B %Y, %H:%M')}")

    if "camera" in exif_meta:
        parts.append(exif_meta["camera"])

    if "fnumber" in exif_meta:
        parts.append(f"f/{exif_meta['fnumber']:g}")

    if "exposure" in exif_meta:
        parts.append(f"{exif_meta['exposure']}s")

    if "focal_length" in exif_meta:
        parts.append(f"{exif_meta['focal_length']:g}mm")

    if "iso" in exif_meta:
        parts.append(f"ISO {exif_meta['iso']}")

    if not parts:
        return ""

    line = html.escape(" · ".join(parts))

    sep  = _block("separator", {}, '<hr class="wp-block-separator has-alpha-channel-opacity"/>')
    para = _block(
        "paragraph",
        {"style": {"typography": {"fontSize": "0.875em", "fontStyle": "italic"}}},
        f'<p style="font-size:0.875em;font-style:italic">{line}</p>',
    )
    return f"\n\n{sep}\n\n{para}"


# ---------- image ----------------------------------------------------------

def compress(input_path):
    """Downscale, convert to WebP, drop EXIF (incl. GPS). Returns BytesIO."""
    img = Image.open(input_path)
    img = ImageOps.exif_transpose(img)  # honour rotation tag BEFORE EXIF is dropped
    img = img.convert("RGB")
    img.thumbnail((MAX_EDGE, MAX_EDGE))
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=QUALITY, method=6)
    buf.seek(0)
    return buf


# ---------- wordpress ------------------------------------------------------

def upload_media(token, buf, filename, alt_text):
    r = requests.post(
        f"{API}/media",
        headers=auth_headers(token, {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "image/webp",
        }),
        data=buf.read(),
    )
    if r.status_code != 201:
        sys.exit(f"Media upload failed (HTTP {r.status_code}): {r.text[:300]}")
    data     = r.json()
    media_id = data["id"]
    full_url = data["source_url"]
    sizes    = data.get("media_details", {}).get("sizes", {})
    medium_url = (
        sizes.get("large", sizes.get("medium", {})).get("source_url") or full_url
    )
    requests.post(f"{API}/media/{media_id}", headers=auth_headers(token),
                  json={"alt_text": alt_text})
    return media_id, full_url, medium_url


def build_figure_block(media_id, medium_url, full_url, alt_text):
    """
    Emit a wp:html block containing the CSS-only lightbox figure.
    Thumbnail links to #frag-{media_id}; the overlay closes via href="#!".
    """
    safe_alt = html.escape(alt_text)
    inner = (
        '<figure class="fragment-figure">\n'
        f'  <a href="#frag-{media_id}" class="fragment-thumb">\n'
        f'    <img src="{medium_url}" alt="{safe_alt}" loading="lazy" />\n'
        '  </a>\n'
        f'  <a href="#!" id="frag-{media_id}" class="fragment-lightbox" aria-hidden="true">\n'
        f'    <img src="{full_url}" alt="" />\n'
        '  </a>\n'
        '</figure>'
    )
    return _block("html", {}, inner)


def get_or_create_category(token, name):
    r = requests.get(f"{API}/categories", headers=auth_headers(token),
                     params={"search": name, "per_page": 100})
    for c in r.json():
        if c.get("name", "").lower() == name.lower():
            return c["id"]
    r = requests.post(f"{API}/categories", headers=auth_headers(token), json={"name": name})
    return r.json()["id"]


def create_fragment(token, media_id, blurb, category_id, figure_block="", exif_footer=""):
    blurb_block = _block("paragraph", {}, f"<p>{html.escape(blurb)}</p>")
    payload = {
        "title":          "",
        "content":        figure_block + "\n\n" + blurb_block + exif_footer,
        "status":         "publish",
        "categories":     [category_id],
        "featured_media": media_id,
        "format":         "image",
        "meta":           {"_jetpack_dont_email_post_to_subs": True},
    }
    r = requests.post(f"{API}/posts", headers=auth_headers(token), json=payload)
    if r.status_code not in (200, 201):
        sys.exit(f"Post creation failed (HTTP {r.status_code}): {r.text[:300]}")
    return r.json()["link"]


# ---------- main -----------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Post a photo fragment to tsvetoslavshalev.com"
    )
    parser.add_argument("blurb",    help="Caption / blurb (appears unchanged at the top)")
    parser.add_argument("image",    help="Path to image file")
    parser.add_argument("alt_text", nargs="?", help="Alt text for the image (defaults to blurb)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse EXIF, print what would be posted, then exit without uploading")
    parser.add_argument("--keep", action="store_true",
                        help="Keep the original image file after a successful upload")
    args = parser.parse_args()

    if not os.path.exists(args.image):
        sys.exit(f"File not found: {args.image}")

    alt_text = args.alt_text or args.blurb
    stem     = os.path.splitext(os.path.basename(args.image))[0]

    # 1. Read EXIF — must happen before compress() strips it
    print("Reading EXIF...")
    exif_meta = read_exif(args.image)
    src = exif_meta.get("datetime_source")
    if src and src != "DateTimeOriginal":
        print(f"  Note: DateTimeOriginal missing; using {src} for timestamp.", file=sys.stderr)

    # 2. Reverse-geocode GPS if present
    locality, display_name = None, None
    if "gps" in exif_meta:
        lat, lon = exif_meta["gps"]
        print(f"  GPS: {lat:.5f}, {lon:.5f}")
        print("Reverse geocoding...")
        locality, display_name = reverse_geocode(lat, lon)
        if display_name:
            print(f"  Place: {display_name}")
        else:
            print("  Could not resolve a place name; continuing without it.", file=sys.stderr)
    else:
        print("  No GPS in EXIF.")

    # 3. Derive filename
    filename = make_filename(stem, exif_meta, locality)
    print(f"  Filename: {filename}")

    # 4. Build EXIF footer Gutenberg block
    exif_footer = build_exif_footer(exif_meta, display_name)

    if args.dry_run:
        blurb_block    = _block("paragraph", {}, f"<p>{html.escape(args.blurb)}</p>")
        figure_preview = build_figure_block("[MEDIA_ID]", "[MEDIUM_URL]", "[FULL_URL]", alt_text)
        print("\n── DRY RUN ──────────────────────────────────────────────")
        print(f"Image:     {args.image}")
        print(f"Filename:  {filename}")
        print(f"Alt text:  {alt_text}")
        print(f"\nPost content:\n{figure_preview}\n\n{blurb_block}{exif_footer}")
        print("\n(Figure URLs are placeholders — real values come from the upload response.)")
        print("─────────────────────────────────────────────────────────")
        return

    # 5. Authenticate
    token = get_token()
    print("Authenticated.")

    # 6. Compress — GPS and all EXIF are stripped here; public file is clean
    print("Compressing + resizing + stripping EXIF...")
    buf = compress(args.image)

    # 7. Upload
    print("Uploading media...")
    media_id, full_url, medium_url = upload_media(token, buf, filename, alt_text)
    print(f"  media id:   {media_id}")
    print(f"  medium url: {medium_url}")

    # 8. Create post
    figure_block = build_figure_block(media_id, medium_url, full_url, alt_text)
    cat_id = get_or_create_category(token, FRAGMENTS_CATEGORY)
    link   = create_fragment(token, media_id, args.blurb, cat_id, figure_block, exif_footer)
    print(f"Fragment published (subscribers not emailed): {link}")

    # 9. Clean up original — only runs if upload + post both succeeded (sys.exit on failure)
    if args.keep:
        print(f"Kept local file: {args.image} (--keep flag)")
    else:
        try:
            os.remove(args.image)
            print(f"Removed local file: {args.image}")
        except OSError as e:
            print(f"Warning: could not remove {args.image}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
