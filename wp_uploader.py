#!/usr/bin/env python3
"""
Fragments uploader for tsvetoslavshalev.com  (WordPress.com Simple site)

Pipeline:
    compress + resize + strip EXIF  ->  upload media  ->
    create a post in the 'Fragments' category  ->  suppress the subscriber email.

WHY THE OLD SCRIPT 401'd
    The public-api.wordpress.com gateway authenticates with OAuth2 *bearer tokens*,
    not Application-Password Basic Auth. Basic Auth there is ignored, so the request
    arrived anonymous -> "not allowed to create posts" regardless of your role.
    This version uses a bearer token instead.

.env expected:
    WP_SITE              = tsvetoslavshalev.com      # domain or numeric blog id
    # Either supply a ready token...
    WPCOM_ACCESS_TOKEN   = ...                        # preferred: store just this
    # ...or the password-grant inputs so the script mints one:
    WPCOM_CLIENT_ID      = 12345                      # developer.wordpress.com/apps
    WPCOM_CLIENT_SECRET  = ...
    WPCOM_USERNAME       = your_wpcom_username        # user_login, NOT your email
    WPCOM_APP_PASSWORD   = xxxx xxxx xxxx xxxx xxxx xxxx

Usage:
    python wp_uploader.py "Your blurb here" photo.jpg ["optional alt text"]
"""

import io
import os
import sys
import requests
from PIL import Image, ImageOps
from dotenv import load_dotenv

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
MAX_EDGE = 1600     # longest side in px; bump to 2048 for crisper click-through
QUALITY  = 80       # WebP quality


# ---------- auth -----------------------------------------------------------
def get_token():
    if ACCESS_TOKEN:
        return ACCESS_TOKEN
    if not all([CLIENT_ID, CLIENT_SECRET, USERNAME, APP_PASSWORD]):
        sys.exit("Error: no WPCOM_ACCESS_TOKEN and the password-grant fields are "
                 "incomplete in .env.")
    r = requests.post(TOKEN_URL, data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "password",
        "username": USERNAME,
        "password": APP_PASSWORD,
    })
    if r.status_code != 200:
        sys.exit(f"Token request failed (HTTP {r.status_code}): {r.text[:300]}")
    return r.json()["access_token"]


def headers(token, extra=None):
    h = {"Authorization": f"Bearer {token}"}
    if extra:
        h.update(extra)
    return h


# ---------- image ----------------------------------------------------------
def compress(input_path):
    """Downscale, convert to WebP, drop EXIF (incl. GPS). Returns BytesIO."""
    img = Image.open(input_path)
    img = ImageOps.exif_transpose(img)      # apply rotation BEFORE EXIF is dropped
    img = img.convert("RGB")
    img.thumbnail((MAX_EDGE, MAX_EDGE))     # keeps aspect ratio
    buf = io.BytesIO()
    # PIL does not copy EXIF unless told to, so GPS/camera data is left behind.
    img.save(buf, "WEBP", quality=QUALITY, method=6)
    buf.seek(0)
    return buf


# ---------- wordpress ------------------------------------------------------
def upload_media(token, buf, filename, alt_text):
    r = requests.post(
        f"{API}/media",
        headers=headers(token, {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "image/webp",
        }),
        data=buf.read(),
    )
    if r.status_code != 201:
        sys.exit(f"Media upload failed (HTTP {r.status_code}): {r.text[:300]}")
    media_id = r.json()["id"]
    # alt text is invisible to readers - pure accessibility / SEO
    requests.post(f"{API}/media/{media_id}", headers=headers(token),
                  json={"alt_text": alt_text})
    return media_id


def get_or_create_category(token, name):
    r = requests.get(f"{API}/categories", headers=headers(token),
                     params={"search": name, "per_page": 100})
    for c in r.json():
        if c.get("name", "").lower() == name.lower():
            return c["id"]
    r = requests.post(f"{API}/categories", headers=headers(token), json={"name": name})
    return r.json()["id"]


def create_fragment(token, media_id, blurb, category_id):
    payload = {
        "title": "",                       # fragments are title-less notes
        "content": blurb,
        "status": "publish",
        "categories": [category_id],
        "featured_media": media_id,
        "format": "image",
        # The post-meta flag Jetpack uses to skip the subscriber email.
        # NOTE: treat this as belt, not braces - also exclude the Fragments
        # category from your Newsletter sending settings as the durable guard.
        "meta": {"_jetpack_dont_email_post_to_subs": True},
    }
    r = requests.post(f"{API}/posts", headers=headers(token), json=payload)
    if r.status_code not in (200, 201):
        sys.exit(f"Post creation failed (HTTP {r.status_code}): {r.text[:300]}")
    return r.json()["link"]


# ---------- main -----------------------------------------------------------
def main():
    if len(sys.argv) not in (3, 4):
        sys.exit('Usage: python wp_uploader.py "Your blurb" photo.jpg ["alt text"]')
    blurb = sys.argv[1]
    image_path = sys.argv[2]
    alt_text = sys.argv[3] if len(sys.argv) == 4 else blurb
    if not os.path.exists(image_path):
        sys.exit(f"File not found: {image_path}")

    token = get_token()
    print("Authenticated.")

    print("Compressing + resizing + stripping EXIF...")
    buf = compress(image_path)
    filename = os.path.splitext(os.path.basename(image_path))[0] + ".webp"

    print("Uploading media...")
    media_id = upload_media(token, buf, filename, alt_text)
    print(f"  media id: {media_id}")

    cat_id = get_or_create_category(token, FRAGMENTS_CATEGORY)
    print(f"Fragments category id: {cat_id}")

    link = create_fragment(token, media_id, blurb, cat_id)
    print(f"Fragment published (subscribers not emailed): {link}")


if __name__ == "__main__":
    main()