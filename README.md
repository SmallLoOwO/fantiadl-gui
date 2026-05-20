# FantiaDL

> **Note — fork:** This is a low-maintenance fork of
> [bitbybyte/fantiadl](https://github.com/bitbybyte/fantiadl) with a few
> extras layered on top of the upstream CLI. Original work © 2018 bitbybyte,
> MIT License (preserved in `LICENSE`). I won't actively maintain this — PRs
> and issues may get attention occasionally.
>
> **What's added in this fork:**
> - Robust retry & cooldown around 429 / connection errors so a single rate
>   limit doesn't kill a long fanclub run (`--max-retries`,
>   `--retry-backoff`, `--retry-backoff-max`, `--cooldown-seconds`,
>   `--cooldown-attempts`)
> - `--sleep-request` preventative throttle (a sleep before every HTTP
>   request) — the simplest way to avoid 429 in the first place
> - `--post-directory-format` for customizable per-post folder names
>   (default `{post_id}_{post_title}`; restore old behavior with
>   `--post-directory-format "{post_id}"`)
> - `--verify` cross-check mode: for a fanclub, compare server posts vs
>   local `--db` and report what's missing, optionally re-download the
>   incomplete ones. Writes a JSON report (`--verify-json`).
> - Simple tkinter GUI: run `python fantiadl_gui.py` (or build a single exe
>   with `pyinstaller --onefile --noconsole --name fantiadl_gui
>   --collect-submodules fantiadl fantiadl_gui.py`). Has tabs for download,
>   verify, and advanced flags; can create a new SQLite DB from inside the UI.
>
> See the new flags in `--help` and the GUI itself for details. Everything
> below this note is the original upstream README, unmodified.
>
> ---

Download media and other data from Fantia fanclubs and posts. A session cookie must be provided with the -c/--cookie argument directly or by passing the path to a legacy Netscape cookies file. Please see the [About Session Cookies](#about-session-cookies) section.

```
usage: fantiadl [options] url

positional arguments:
  url                   fanclub or post URL

options:
  -h, --help            show this help message and exit
  -c SESSION_COOKIE, --cookie SESSION_COOKIE
                        _session_id cookie or cookies.txt
  -q, --quiet           suppress output
  -v, --version         show program's version number and exit
  --db DB_PATH          database to track post download state (creates tables when first specified)"
  --db-bypass-post-check
                        bypass checking a post for new content if it's marked as completed on the database

download options:
  -i, --ignore-errors   continue on download errors
  -l #, --limit #       limit the number of posts to process per fanclub (excludes -n)
  -o OUTPUT_PATH, --output-directory OUTPUT_PATH
                        directory to download to
  -s, --use-server-filenames
                        download using server defined filenames
  -r, --mark-incomplete-posts
                        add .incomplete file to post directories that are incomplete
  -m, --dump-metadata   store metadata to file (including fanclub icon, header, and background)
  -x, --parse-for-external-links
                        parse posts for external links
  -t, --download-thumbnail
                        download post thumbnails
  -f, --download-fanclubs
                        download posts from all followed fanclubs
  -p, --download-paid-fanclubs
                        download posts from all fanclubs backed on a paid plan
  -n #, --download-new-posts #
                        download a specified number of new posts from your fanclub timeline
  -d %Y-%m, --download-month %Y-%m
                        download posts only from a specific month, e.g. 2007-08 (excludes -n)
  --exclude EXCLUDE_FILE
                        file containing a list of filenames to exclude from downloading
```

To track post downloads, specify a database path using `--db`, e.g. `--db ~/fantiadl.db`. When existing post content downloads are encountered, they will be skipped over. When all post contents under a parent post have been downloaded, the post will be marked complete on the database. If future requests to download a post indicate the post was modified based on its timestamp, new contents will be checked for; this behavior can be disabled by setting `--db-bypass-post-check`.

When parsing for external links using `-x`, a .crawljob file is created in your root directory (either the directory provided with `-o` or the directory the script is being run from) that can be parsed by [JDownloader](http://jdownloader.org/). As posts are parsed, links will be appended and assigned their appropriate post directories for download. You can import this file manually into JDownloader (File -> Load Linkcontainer) or setup the Folder Watch plugin to watch your root directory for .crawljob files.

## About Session Cookies
Due to recent changes imposed by Fantia, providing an email and password to login from the command line is no longer supported. In order to login, you will need to provide the `_session_id` cookie for your Fantia login session using -c/--cookie. After logging in normally on your browser, this value can then be extracted and used with FantiaDL. This value expires and may need to be updated with some regularity.

### Mozilla Firefox
1. On https://fantia.jp, press Ctrl + Shift + I to open Developer Tools.
2. Select the Storage tab at the top. In the sidebar, select https://fantia.jp under the Cookies heading.
3. Locate the `_session_id` cookie name. Click on the value to copy it.

### Google Chrome
1. On https://fantia.jp, press Ctrl + Shift + I to open DevTools.
2. Select the Application tab at the top. In the sidebar, expand Cookies under the Storage heading and select https://fantia.jp.
3. Locate the `_session_id` cookie name. Click on the value to copy it.

### Third-Party Extensions (cookies.txt)
You also have the option of passing the path to a legacy Netscape format cookies file with -c/--cookie, e.g. `-c ~/cookies.txt`. Using an extension like [cookies.txt](https://chrome.google.com/webstore/detail/cookiestxt/njabckikapfpffapmjgojcnbfjonfjfg), create a text file matching the accepted format:

```
# Netscape HTTP Cookie File
# https://curl.haxx.se/rfc/cookie_spec.html
# This is a generated file! Do not edit.

fantia.jp	FALSE	/	FALSE	1595755239	_session_id	a1b2c3d4...
```

Only the `_session_id` cookie is required.

## Download
`pip install fantiadl`
https://pypi.org/project/fantiadl/

Binaries are also provided for [new releases](https://github.com/bitbybyte/fantiadl/releases/latest).

## Build Requirements
 - Python >=3.7
 - requests
 - beautifulsoup4

## Roadmap
 - More robust logging
