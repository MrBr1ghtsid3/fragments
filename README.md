# fragments

I like to take a lot of photos! I am not very good at it, but I do it nonetheless.

This tool I am working on with Claude should help me get them onto my WordPress site in the form of "instagram-lite" feed version, without thinking about it much.

You give it a caption and an image file. It compresses the image to WebP, strips all EXIF data (including GPS), uploads it, creates a post in a category of choice, and skips the subscriber email notification (by default). That's it.

## requirements

```bash
pip install -r requirements.txt
```

## setup

Create a `.env` file next to the script. You can either supply a ready access token (simplest), or let the script mint one each run using the password grant:

```env
WP_SITE = tsvetoslavshalev.com

# Option 1 — preferred: paste a long-lived token directly
WPCOM_ACCESS_TOKEN = ...

# Option 2 — the script will fetch a token automatically on each run
WPCOM_CLIENT_ID     = ...
WPCOM_CLIENT_SECRET = ...
WPCOM_USERNAME      = your_wpcom_username
WPCOM_APP_PASSWORD  = xxxx xxxx xxxx xxxx xxxx xxxx
```

Register your app and get a client ID and secret at [developer.wordpress.com/apps](https://developer.wordpress.com/apps).

## usage

```bash
python wp_uploader.py "Caption for the photo" photo.jpg
# optionally, pass different alt text as a third argument
python wp_uploader.py "Caption" photo.jpg "Alt text for screen readers"
# dry run — parses EXIF, prints what would be posted, skips the upload
python wp_uploader.py "Caption" photo.jpg --dry-run
```

## what the EXIF footer looks like

The script reads camera metadata before compressing the image (compression strips it). It reverse-geocodes the GPS coordinates to a place name, then appends a small footer below the caption. The GPS data is only used as text in the footer — it is not embedded in the uploaded file.

**Full EXIF (GPS + camera + optics):**

> *📍 Tutrakan, Bulgaria · 10 June 2026, 13:31 · Nothing Phone (2a) Plus · f/1.87 · 1/3078s · 5.56mm · ISO 103*

**Partial EXIF (no GPS, datetime from file):**

> *10 June 2026, 13:31 · f/1.87 · 1/3078s · ISO 103*

**No EXIF at all** (AI-generated image, screenshot): no footer is appended.

## why OAuth2 and not Basic Auth

The script used to use HTTP Basic Auth with a WordPress.com Application Password. That does not work against the `public-api.wordpress.com` gateway — the gateway only accepts OAuth2 Bearer tokens, so Basic Auth requests arrive as anonymous and get rejected with a 401 regardless of the account's role.

WordPress.com's developer docs cover the full OAuth2 flow: [developer.wordpress.com/docs/oauth2](https://developer.wordpress.com/docs/oauth2/).
