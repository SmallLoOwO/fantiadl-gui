#!/usr/bin/env python
# -*- coding: utf-8 -*-

from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter, Retry
import requests

from datetime import datetime as dt
from email.utils import parsedate_to_datetime
from urllib.parse import unquote
from urllib.parse import urljoin
from urllib.parse import urlparse
import http.cookiejar
import json
import math
import mimetypes
import os
import re
import sys
import time
import traceback

from .__version__ import __version__
from .db import FantiaDlDatabase

FANTIA_URL_RE = re.compile(r"(?:https?://(?:(?:www\.)?(?:fantia\.jp/(fanclubs|posts)/)))([0-9]+)")
EXTERNAL_LINKS_RE = re.compile(r"(?:[\s]+)?((?:(?:https?://)?(?:(?:www\.)?(?:mega\.nz|mediafire\.com|(?:drive|docs)\.google\.com|youtube.com|dropbox.com)\/))[^\s]+)")

DOMAIN = "fantia.jp"
BASE_URL = "https://fantia.jp/"

LOGIN_SIGNIN_URL = "https://fantia.jp/sessions/signin"
LOGIN_SESSION_URL = "https://fantia.jp/sessions"

ME_API = "https://fantia.jp/api/v1/me"

FANCLUB_API = "https://fantia.jp/api/v1/fanclubs/{}"
FANCLUBS_FOLLOWING_API = "https://fantia.jp/api/v1/me/fanclubs"
FANCLUBS_PAID_HTML = "https://fantia.jp/mypage/users/plans?type=not_free&page={}"
FANCLUB_POSTS_HTML = "https://fantia.jp/fanclubs/{}/posts?page={}"

POST_API = "https://fantia.jp/api/v1/posts/{}"
POST_URL = "https://fantia.jp/posts/{}"
POSTS_URL = "https://fantia.jp/posts"
POST_RELATIVE_URL = "/posts/"

TIMELINES_API = "https://fantia.jp/api/v1/me/timelines/posts?page={}&per=24"

USER_AGENT = "fantiadl/{}".format(__version__)

CRAWLJOB_FILENAME = "external_links.crawljob"

MIMETYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/webm": ".webm"
}

UNICODE_CONTROL_MAP = dict.fromkeys(range(32))


class FantiaClub:
    def __init__(self, fanclub_id):
        self.id = fanclub_id


class FantiaDownloader:
    def __init__(self, session_arg, chunk_size=1024 * 1024 * 5, dump_metadata=False, parse_for_external_links=False, download_thumb=False, directory=None, quiet=True, continue_on_error=False, use_server_filenames=False, mark_incomplete_posts=False, month_limit=None, exclude_file=None, db_path=None, db_bypass_post_check=False, max_retries=10, retry_backoff=3, retry_backoff_max=120, cooldown_seconds=60, cooldown_attempts=10, sleep_request=0.0, post_directory_format="{post_id}_{post_title}", max_title_length=80, post_order="newest"):
        # self.email = email
        # self.password = password
        self.session_arg = session_arg
        self.chunk_size = chunk_size
        self.dump_metadata = dump_metadata
        self.parse_for_external_links = parse_for_external_links
        self.download_thumb = download_thumb
        self.directory = directory or ""
        self.quiet = quiet
        self.continue_on_error = continue_on_error
        self.use_server_filenames = use_server_filenames
        self.mark_incomplete_posts = mark_incomplete_posts
        self.month_limit = dt.strptime(month_limit, "%Y-%m") if month_limit else None
        self.exclude_file = exclude_file
        self.exclusions = []
        self.db = FantiaDlDatabase(db_path)
        self.db_bypass_post_check = db_bypass_post_check
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.retry_backoff_max = retry_backoff_max
        self.cooldown_seconds = cooldown_seconds
        self.cooldown_attempts = cooldown_attempts
        self.sleep_request = sleep_request
        self.post_directory_format = post_directory_format
        self.max_title_length = max_title_length
        self.post_order = post_order

        self.initialize_session()
        self.login()
        self.create_exclusions()

    def _format_post_directory_title(self, post_id, post_title, post_creator, fanclub_id):
        """Build the per-post folder name from self.post_directory_format.

        Each field is sanitized before being inserted, so titles containing
        slashes / illegal chars cannot escape the intended directory level.
        """
        clean_title = sanitize_for_path((post_title or "").strip())
        fields = {
            "post_id": str(post_id),
            "post_title": clean_title,
            "post_creator": sanitize_for_path(post_creator or ""),
            "fanclub_id": str(fanclub_id) if fanclub_id is not None else "",
        }
        try:
            name = self.post_directory_format.format(**fields)
        except (KeyError, IndexError) as e:
            self.output("Invalid --post-directory-format ({}). Falling back to post id.\n".format(e))
            name = str(post_id)

        # If the title was empty, common patterns end up with a dangling
        # separator like "4055902_". Trim them so we don't get ugly names.
        if not clean_title:
            name = re.sub(r"[\s_\-]+$", "", name) or str(post_id)

        name = sanitize_for_path(name)
        if self.max_title_length and len(name) > self.max_title_length:
            name = name[:self.max_title_length].rstrip()
            # Re-strip trailing dots/spaces produced by truncation
            name = re.sub(r'[\s.]+$', '', name) or str(post_id)
        return name

    def _cooldown_retry(self, label, func, *args, **kwargs):
        """Run func(*args, **kwargs); on RetryError / connection failures, cool down and retry."""
        attempt = 0
        while True:
            try:
                return func(*args, **kwargs)
            except KeyboardInterrupt:
                raise
            except (requests.exceptions.RetryError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.Timeout) as e:
                attempt += 1
                if attempt > self.cooldown_attempts:
                    self.output("Cooldown attempts exhausted for {} ({} attempts). Giving up.\n".format(label, self.cooldown_attempts))
                    raise
                wait = self.cooldown_seconds * attempt
                self.output("\nRate limited / connection error on {}: {}\nCooling down for {}s before retrying (cooldown attempt {}/{})...\n".format(label, type(e).__name__, wait, attempt, self.cooldown_attempts))
                try:
                    time.sleep(wait)
                except KeyboardInterrupt:
                    raise

    def output(self, output):
        """Write output to the console."""
        if not self.quiet:
            try:
                sys.stdout.write(output.encode(sys.stdout.encoding, errors="backslashreplace").decode(sys.stdout.encoding))
                sys.stdout.flush()
            except (UnicodeEncodeError, UnicodeDecodeError):
                sys.stdout.buffer.write(output.encode("utf-8"))
                sys.stdout.flush()

    def initialize_session(self):
        """Initialize session with necessary headers and config."""

        self.session = requests.session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        retries = Retry(
            total=self.max_retries,
            connect=self.max_retries,
            read=self.max_retries,
            status=self.max_retries,
            status_forcelist=[429, 500, 502, 503, 504, 507, 508],
            allowed_methods=None,  # retry on all HTTP methods
            backoff_factor=self.retry_backoff, # retry delay = {backoff factor} * (2 ** ({retry number} - 1))
            respect_retry_after_header=True,
            raise_on_status=True
        )
        # backoff_max caps the per-retry sleep (urllib3 >= 1.26 uses attribute; older versions use class attr)
        try:
            retries.backoff_max = self.retry_backoff_max
        except Exception:
            pass
        self.session.mount("http://", HTTPAdapter(max_retries=retries))
        self.session.mount("https://", HTTPAdapter(max_retries=retries))

        # Optional throttle: sleep before every top-level request to prevent 429.
        # Hooked on session.send so urllib3-level retries (inside HTTPAdapter.send)
        # are not double-counted.
        if self.sleep_request and self.sleep_request > 0:
            _original_send = self.session.send

            def _send_with_sleep(request, **kwargs):
                time.sleep(self.sleep_request)
                return _original_send(request, **kwargs)

            self.session.send = _send_with_sleep

    def login(self):
        """Login to Fantia using the provided email and password."""
        try:
            with open(self.session_arg, "r") as cookies_file:
                cookies = http.cookiejar.MozillaCookieJar(self.session_arg)
                cookies.load()
                self.session.cookies = cookies
        except FileNotFoundError:
            login_cookie = requests.cookies.create_cookie(domain=DOMAIN, name="_session_id", value=self.session_arg)
            self.session.cookies.set_cookie(login_cookie)

        check_user = self.session.get(ME_API)
        if not (check_user.ok or check_user.status_code == 304):
            sys.exit("Error: Invalid session. Please verify your session cookie")

        # Login flow, requires reCAPTCHA token

        # login_json = {
        #     "utf8": "✓",
        #     "button": "",
        #     "user[email]": self.email,
        #     "user[password]": self.password,
        # }

        # login_session = self.session.get(LOGIN_SIGNIN_URL)
        # login_page = BeautifulSoup(login_session.text, "html.parser")
        # authenticity_token = login_page.select_one("input[name=\"authenticity_token\"]")["value"]
        # print(login_page.select_one("input[name=\"recaptcha_response\"]"))
        # login_json["authenticity_token"] = authenticity_token
        # login_json["recaptcha_response"] = ...

        # create_session = self.session.post(LOGIN_SESSION_URL, data=login_json)
        # if not create_session.headers.get("Location"):
        #     sys.exit("Error: Bad login form data")
        # elif create_session.headers["Location"] == LOGIN_SIGNIN_URL:
        #     sys.exit("Error: Failed to login. Please verify your username and password")

        # check_user = self.session.get(ME_API)
        # if not (check_user.ok or check_user.status_code == 304):
        #     sys.exit("Error: Invalid session")

    def create_exclusions(self):
        """Read files to exclude from downloading."""
        if self.exclude_file:
            with open(self.exclude_file, "r") as file:
                self.exclusions = [line.rstrip("\n") for line in file]

    def process_content_type(self, url):
        """Process the Content-Type from a request header and use it to build a filename."""
        url_header = self.session.head(url, allow_redirects=True)
        mimetype = url_header.headers["Content-Type"]
        extension = guess_extension(mimetype, url)
        return extension

    def collect_post_titles(self, post_metadata):
        """Collect all post titles to check for duplicate names and rename as necessary by appending a counter."""
        post_titles = []
        for post in post_metadata["post_contents"]:
            try:
                potential_title = post["title"] or post["parent_post"]["title"]
                if not potential_title:
                    potential_title = str(post["id"])
            except KeyError:
                potential_title = str(post["id"])

            title = potential_title
            counter = 2
            while title in post_titles:
                title = potential_title + "_{}".format(counter)
                counter += 1
            post_titles.append(title)

        return post_titles

    def download_fanclub_metadata(self, fanclub):
        """Download fanclub header, icon, and custom background."""
        response = self.session.get(FANCLUB_API.format(fanclub.id))
        response.raise_for_status()
        fanclub_json = json.loads(response.text)

        fanclub_creator = fanclub_json["fanclub"]["creator_name"]
        fanclub_directory = os.path.join(self.directory, sanitize_for_path(fanclub_creator))
        os.makedirs(fanclub_directory, exist_ok=True)

        self.save_metadata(fanclub_json, fanclub_directory)

        header_url = fanclub_json["fanclub"]["cover"]["original"]
        if header_url:
            header_filename = os.path.join(fanclub_directory, "header" + self.process_content_type(header_url))
            self.output("Downloading fanclub header...\n")
            self.perform_download(header_url, header_filename, use_server_filename=self.use_server_filenames)

        fanclub_icon_url = fanclub_json["fanclub"]["icon"]["original"]
        if fanclub_icon_url:
            fanclub_icon_filename = os.path.join(fanclub_directory, "icon" + self.process_content_type(fanclub_icon_url))
            self.output("Downloading fanclub icon...\n")
            self.perform_download(fanclub_icon_url, fanclub_icon_filename, use_server_filename=self.use_server_filenames)

        background_url = fanclub_json["fanclub"]["background"]
        if background_url:
            background_filename = os.path.join(fanclub_directory, "background" + self.process_content_type(background_url))
            self.output("Downloading fanclub background...\n")
            self.perform_download(background_url, background_filename, use_server_filename=self.use_server_filenames)

    def verify_fanclub(self, fanclub, output_json=None, auto_fix=True, limit=0):
        """Cross-check a fanclub's posts against local DB and report what's missing.

        Requires --db. Walks every post listed on the fanclub page, fetches its
        API to enumerate expected file URLs, and compares each against the
        `urls` table. Optionally redownloads anything that is incomplete.
        """
        if not self.db.conn:
            self.output("--verify requires --db <path>. Without a DB there is nothing to cross-check against. Aborting verify.\n")
            return None

        self.output("Verifying fanclub {}...\n".format(fanclub.id))
        post_ids = self.fetch_fanclub_posts(fanclub)
        if limit:
            post_ids = post_ids[:limit]

        report_posts = []
        counts = {"complete": 0, "incomplete": 0, "locked": 0, "error": 0}

        for idx, post_id in enumerate(post_ids):
            self.output("  [{}/{}] Verifying post {}...\n".format(idx + 1, len(post_ids), post_id))
            try:
                entry = self._cooldown_retry(
                    "verify post {}".format(post_id),
                    self._verify_post_inner,
                    post_id,
                )
            except KeyboardInterrupt:
                raise
            except Exception as e:
                self.output("    Error verifying post {}: {}\n".format(post_id, e))
                entry = {
                    "post_id": str(post_id),
                    "post_url": POST_URL.format(post_id),
                    "status": "error",
                    "error": "{}: {}".format(type(e).__name__, e),
                }
            report_posts.append(entry)
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1

        summary = {
            "total_posts": len(post_ids),
            "complete": counts.get("complete", 0),
            "incomplete": counts.get("incomplete", 0),
            "locked": counts.get("locked", 0),
            "error": counts.get("error", 0),
        }
        report = {
            "fanclub_id": str(fanclub.id),
            "fanclub_url": "https://fantia.jp/fanclubs/{}".format(fanclub.id),
            "verified_at": dt.utcnow().isoformat() + "Z",
            "summary": summary,
            "posts": report_posts,
        }

        self.output("\n=== Verification report for fanclub {} ===\n".format(fanclub.id))
        self.output("Total posts on server : {}\n".format(summary["total_posts"]))
        self.output("  Complete            : {}\n".format(summary["complete"]))
        self.output("  Incomplete (missing): {}\n".format(summary["incomplete"]))
        self.output("  Locked (no access)  : {}\n".format(summary["locked"]))
        self.output("  Error verifying     : {}\n".format(summary["error"]))

        incompletes = [p for p in report_posts if p["status"] == "incomplete"]
        if incompletes:
            self.output("\nIncomplete posts ({}):\n".format(len(incompletes)))
            for p in incompletes[:30]:
                self.output("  - {} ({} of {} files) {}  {}\n".format(
                    p["post_id"],
                    p["downloaded_files"], p["expected_files"],
                    p["post_url"],
                    (p.get("post_title") or "")[:40],
                ))
            if len(incompletes) > 30:
                self.output("  ... and {} more (see JSON report).\n".format(len(incompletes) - 30))

        if output_json:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(output_json)), exist_ok=True)
            except Exception:
                pass
            with open(output_json, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            self.output("\nReport written to: {}\n".format(output_json))

        if auto_fix and incompletes:
            self.output("\nRe-downloading {} incomplete posts...\n".format(len(incompletes)))
            for p in incompletes:
                pid = p["post_id"]
                # Force download_post to actually re-check this post even if its
                # row in `posts` has download_complete=1 from a prior run.
                try:
                    self.db.update_post_download_complete(pid, 0)
                except Exception:
                    pass
                try:
                    self.download_post(pid)
                except KeyboardInterrupt:
                    raise
                except Exception:
                    if self.continue_on_error:
                        self.output("Encountered an error redownloading post {}. Skipping...\n".format(pid))
                        traceback.print_exc()
                        continue
                    raise

        return report

    def _verify_post_inner(self, post_id):
        """Verify a single post; return one entry for the report."""
        post_html_response = self.session.get(POST_URL.format(post_id))
        post_html_response.raise_for_status()
        post_html = BeautifulSoup(post_html_response.text, "html.parser")
        csrf_meta = post_html.select_one("meta[name=\"csrf-token\"]")
        headers = {"X-Requested-With": "XMLHttpRequest"}
        if csrf_meta and csrf_meta.get("content"):
            headers["X-CSRF-Token"] = csrf_meta["content"]

        response = self.session.get(POST_API.format(post_id), headers=headers)
        response.raise_for_status()
        post_json = json.loads(response.text)["post"]

        post_title = post_json.get("title") or ""
        post_contents = post_json.get("post_contents") or []
        total_contents = len(post_contents)
        visible_count = 0
        expected_urls = []

        for content in post_contents:
            if content.get("visible_status") != "visible":
                continue
            visible_count += 1
            category = content.get("category")
            if category == "photo_gallery":
                for photo in content.get("post_content_photos", []) or []:
                    url = (photo.get("url") or {}).get("original")
                    if url:
                        expected_urls.append(url)
            elif category == "file":
                if content.get("download_uri"):
                    expected_urls.append(urljoin(POSTS_URL, content["download_uri"]))
            elif category == "blog":
                try:
                    blog_json = json.loads(content.get("comment") or "{}")
                    for op in blog_json.get("ops") or []:
                        insert = op.get("insert")
                        if isinstance(insert, dict) and insert.get("fantiaImage"):
                            original_url = insert["fantiaImage"].get("original_url")
                            if original_url:
                                expected_urls.append(urljoin(BASE_URL, original_url))
                except (ValueError, TypeError):
                    pass
            # 'embed' has no file we control; skip.

        missing = []
        for url in expected_urls:
            url_path = unquote(url.split("?", 1)[0])
            if not self.db.is_url_downloaded(url_path):
                missing.append(url)

        if total_contents > 0 and visible_count == 0:
            status = "locked"
        elif not expected_urls:
            # Visible but no downloadable file (embed-only / pure text) → not failure
            status = "complete"
        elif missing:
            status = "incomplete"
        else:
            status = "complete"

        return {
            "post_id": str(post_id),
            "post_title": post_title,
            "post_url": POST_URL.format(post_id),
            "status": status,
            "total_contents": total_contents,
            "visible_contents": visible_count,
            "expected_files": len(expected_urls),
            "downloaded_files": len(expected_urls) - len(missing),
            "missing_files": missing,
        }

    def download_fanclub(self, fanclub, limit=0):
        """Download a fanclub."""
        self.output("Downloading fanclub {}...\n".format(fanclub.id))
        post_ids = self.fetch_fanclub_posts(fanclub)

        if self.dump_metadata:
            self.download_fanclub_metadata(fanclub)

        for post_id in post_ids if limit == 0 else post_ids[:limit]:
            try:
                self.download_post(post_id)
            except KeyboardInterrupt:
                raise
            except:
                if self.continue_on_error:
                    self.output("Encountered an error downloading post. Skipping...\n")
                    traceback.print_exc()
                    continue
                else:
                    raise

    def download_followed_fanclubs(self, limit=0):
        """Download all followed fanclubs."""
        response = self.session.get(FANCLUBS_FOLLOWING_API)
        response.raise_for_status()
        fanclub_ids = json.loads(response.text)["fanclub_ids"]

        for fanclub_id in fanclub_ids:
            try:
                fanclub = FantiaClub(fanclub_id)
                self.download_fanclub(fanclub, limit)
            except KeyboardInterrupt:
                raise
            except:
                if self.continue_on_error:
                    self.output("Encountered an error downloading fanclub. Skipping...\n")
                    traceback.print_exc()
                    continue
                else:
                    raise

    def download_paid_fanclubs(self, limit=0):
        """Download all fanclubs backed on a paid plan."""
        all_paid_fanclubs = []
        page_number = 1
        self.output("Collecting paid fanclubs...\n")
        while True:
            response = self.session.get(FANCLUBS_PAID_HTML.format(page_number))
            response.raise_for_status()
            response_page = BeautifulSoup(response.text, "html.parser")
            fanclub_links = response_page.select("div.mb-5-children > div:nth-of-type(1) a[href^=\"/fanclubs\"]")

            for fanclub_link in fanclub_links:
                fanclub_id = fanclub_link["href"].lstrip("/fanclubs/")
                all_paid_fanclubs.append(fanclub_id)
            if not fanclub_links:
                self.output("Collected {} fanclubs.\n".format(len(all_paid_fanclubs)))
                break
            else:
                page_number += 1

        for fanclub_id in all_paid_fanclubs:
            try:
                fanclub = FantiaClub(fanclub_id)
                self.download_fanclub(fanclub, limit)
            except:
                if self.continue_on_error:
                    self.output("Encountered an error downloading fanclub. Skipping...\n")
                    traceback.print_exc()
                    continue
                else:
                    raise

    def download_new_posts(self, post_limit=24):
        all_new_post_ids = []
        total_pages = math.ceil(post_limit / 24)
        page_number = 1
        has_next = True
        self.output("Downloading {} new posts...\n".format(post_limit))

        while has_next and not len(all_new_post_ids) >= post_limit:
            response = self.session.get(TIMELINES_API.format(page_number))
            response.raise_for_status()
            json_response = json.loads(response.text)

            posts = json_response["posts"]
            has_next = json_response["has_next"]
            for post in posts:
                if len(all_new_post_ids) >= post_limit:
                    break
                post_id = post["id"]
                all_new_post_ids.append(post_id)
            page_number += 1

        for post_id in all_new_post_ids:
            try:
                self.download_post(post_id)
            except KeyboardInterrupt:
                raise
            except:
                if self.continue_on_error:
                    self.output("Encountered an error downloading post. Skipping...\n")
                    traceback.print_exc()
                    continue
                else:
                    raise

    def fetch_fanclub_posts(self, fanclub):
        """Iterate over a fanclub's HTML pages to fetch all post IDs."""
        all_posts = []
        post_found = False
        page_number = 1
        self.output("Collecting fanclub posts...\n")
        while True:
            response = self.session.get(FANCLUB_POSTS_HTML.format(fanclub.id, page_number))
            response.raise_for_status()
            response_page = BeautifulSoup(response.text, "html.parser")
            posts = response_page.select("div.post")
            new_post_ids = []
            for post in posts:
                link = post.select_one("a.link-block")["href"]
                post_id = link.lstrip(POST_RELATIVE_URL)
                date_string = post.select_one(".post-date .mr-5").text if post.select_one(".post-date .mr-5") else post.select_one(".post-date").text
                parsed_date = dt.strptime(date_string, "%Y-%m-%d %H:%M")
                if not self.month_limit or (parsed_date.year == self.month_limit.year and parsed_date.month == self.month_limit.month):
                    post_found = True
                    new_post_ids.append(post_id)
            all_posts += new_post_ids
            if not posts or (not new_post_ids and post_found): # No new posts found and we've already collected a post
                ordered = self._sort_post_ids(all_posts)
                self.output("Collected {} posts (order: {}).\n".format(len(ordered), self.post_order))
                return ordered
            else:
                page_number += 1

    def _sort_post_ids(self, post_ids):
        """Sort post IDs according to self.post_order.

        Fantia's `/fanclubs/<id>/posts?page=N` returns posts roughly newest
        first but it's not strictly guaranteed (edited posts, pinned items,
        scheduled posts can shift order). Sorting by numeric id gives a
        predictable, monotonic order so --limit / Ctrl+C behavior matches
        user intent ("抓最新 N 個" really means newest N).
        """
        if self.post_order == "api":
            return list(post_ids)
        reverse = (self.post_order == "newest")
        try:
            return sorted(post_ids, key=lambda x: int(x), reverse=reverse)
        except (ValueError, TypeError):
            # Defensive fallback: lexical sort if any id isn't numeric.
            return sorted(post_ids, reverse=reverse)

    def perform_download(self, url, filepath, use_server_filename=False, append_server_extension=False):
        """Perform a download for the specified URL, with outer cooldown retry."""
        return self._cooldown_retry(
            "download {}".format(url),
            self._perform_download_inner,
            url, filepath, use_server_filename=use_server_filename, append_server_extension=append_server_extension
        )

    def _perform_download_inner(self, url, filepath, use_server_filename=False, append_server_extension=False):
        """Perform a download for the specified URL while showing progress."""
        url_path = unquote(url.split("?", 1)[0])
        server_filename = os.path.basename(url_path)
        filename = os.path.basename(filepath)
        if use_server_filename:
            filepath = os.path.join(os.path.dirname(filepath), server_filename)

        # Check if filename is in exclusion list
        if server_filename in self.exclusions:
            self.output("Server filename in exclusion list (skipping): {}\n".format(server_filename))
            return
        elif filename in self.exclusions:
            self.output("Filename in exclusion list (skipping): {}\n".format(filename))
            return

        if self.db.conn and self.db.is_url_downloaded(url_path):
            self.output("URL already downloaded. Skipping...\n")
            return

        request = self.session.get(url, stream=True)
        if request.status_code == 404:
            self.output("Download URL returned 404. Skipping...\n")
            return
        request.raise_for_status()

        # Handle redirects so we can properly catch an excluded filename
        # Attachments typically route from fantia.jp/posts/#/download/#
        # Images typically are served directly from cc.fantia.jp
        # Metadata images typically are served from c.fantia.jp
        if request.url != url:
            url_path = unquote(request.url.split("?", 1)[0])
            server_filename = os.path.basename(url_path)
            if server_filename in self.exclusions:
                self.output("Server filename in exclusion list (skipping): {}\n".format(server_filename))
                return
            if use_server_filename:
                filepath = os.path.join(os.path.dirname(filepath), server_filename)

        if not use_server_filename and append_server_extension:
            filepath += guess_extension(request.headers["Content-Type"], url)

        file_size = int(request.headers["Content-Length"])
        if os.path.isfile(filepath) and os.stat(filepath).st_size == file_size:
            self.output("File found (skipping): {}\n".format(filepath))
            self.db.insert_url(url_path)
            return

        self.output("File: {}\n".format(filepath))
        incomplete_filename = filepath + ".part"

        downloaded = 0
        with open(incomplete_filename, "wb") as file:
            for chunk in request.iter_content(self.chunk_size):
                downloaded += len(chunk)
                file.write(chunk)
                done = int(25 * downloaded / file_size)
                percent = int(100 * downloaded / file_size)
                self.output("\r|{0}{1}| {2}% ".format("\u2588" * done, " " * (25 - done), percent))
        self.output("\n")

        if downloaded != file_size:
            raise Exception("Downloaded file size mismatch (expected {}, got {})".format(file_size, downloaded))

        if os.path.exists(filepath):
            os.remove(filepath)
        os.rename(incomplete_filename, filepath)

        self.db.insert_url(url_path)

        modification_time_string = request.headers["Last-Modified"]
        modification_time = int(dt.strptime(modification_time_string, "%a, %d %b %Y %H:%M:%S %Z").timestamp())
        if modification_time:
            access_time = int(time.time())
            os.utime(filepath, times=(access_time, modification_time))

    def download_photo(self, photo_url, photo_counter, gallery_directory):
        """Download a photo to the post's directory."""
        extension = self.process_content_type(photo_url)
        filename = os.path.join(gallery_directory, str(photo_counter) + extension) if gallery_directory else str()
        self.perform_download(photo_url, filename, use_server_filename=self.use_server_filenames)

    def download_file(self, download_url, filename, post_directory):
        """Download a file to the post's directory."""
        self.perform_download(download_url, filename, use_server_filename=True) # Force serve filenames to prevent duplicate collision

    def download_post_content(self, post_json, post_directory, post_title):
        """Parse the post's content to determine whether to save the content as a photo gallery or file."""
        self.output(f"> Content {post_json['id']}\n")

        if self.db.conn and self.db.is_post_content_downloaded(post_json["id"]):
            self.output("Post content already downloaded. Skipping...\n")
            return True

        if post_json["visible_status"] != "visible":
            self.output("Post content not available on current plan. Skipping...\n")
            return False

        if post_json.get("category"):
            if post_json["category"] == "photo_gallery":
                photo_gallery = post_json["post_content_photos"]
                photo_counter = 0
                gallery_directory = os.path.join(post_directory, sanitize_for_path(post_title))
                os.makedirs(gallery_directory, exist_ok=True)
                for photo in photo_gallery:
                    photo_url = photo["url"]["original"]
                    self.download_photo(photo_url, photo_counter, gallery_directory)
                    photo_counter += 1
            elif post_json["category"] == "file":
                filename = os.path.join(post_directory, post_json["filename"])
                download_url = urljoin(POSTS_URL, post_json["download_uri"])
                self.download_file(download_url, filename, post_directory)
            elif post_json["category"] == "embed":
                if self.parse_for_external_links:
                    # TODO: Check what URLs are allowed as embeds
                    link_as_list = [post_json["embed_url"]]
                    self.output("Adding embedded link {0} to {1}.\n".format(post_json["embed_url"], CRAWLJOB_FILENAME))
                    build_crawljob(link_as_list, self.directory, post_directory)
            elif post_json["category"] == "blog":
                blog_comment = post_json["comment"]
                blog_json = json.loads(blog_comment)
                photo_counter = 0
                gallery_directory = os.path.join(post_directory, sanitize_for_path(post_title))
                os.makedirs(gallery_directory, exist_ok=True)
                for op in blog_json["ops"]:
                    if type(op["insert"]) is dict and op["insert"].get("fantiaImage"):
                        photo_url = urljoin(BASE_URL, op["insert"]["fantiaImage"]["original_url"])
                        self.download_photo(photo_url, photo_counter, gallery_directory)
                        photo_counter += 1
            else:
                self.output("Post content category \"{}\" is not supported. Skipping...\n".format(post_json.get("category")))
                return False

        self.db.insert_post_content(post_json["id"], post_json["parent_post"]["url"].rsplit("/", 1)[1], post_json["title"], post_json["category"], post_json["foreign_plan_price"], post_json["currency_code"])

        if self.parse_for_external_links:
            post_description = post_json["comment"] or ""
            self.parse_external_links(post_description, os.path.abspath(post_directory))

        return True

    def download_thumbnail(self, thumb_url, post_directory):
        """Download a thumbnail to the post's directory."""
        extension = self.process_content_type(thumb_url)
        filename = os.path.join(post_directory, "thumb" + extension)
        self.perform_download(thumb_url, filename, use_server_filename=self.use_server_filenames)

    def download_post(self, post_id):
        """Download a post to its own directory, with outer cooldown retry."""
        return self._cooldown_retry(
            "post {}".format(post_id),
            self._download_post_inner,
            post_id
        )

    def _download_post_inner(self, post_id):
        """Download a post to its own directory."""
        db_post = self.db.find_post(post_id)
        if self.db_bypass_post_check and self.db.conn and db_post and db_post["download_complete"]:
            self.output("Post {} already downloaded. Skipping...\n".format(post_id))
            return

        self.output("Downloading post {}...\n".format(post_id))

        post_html_response = self.session.get(POST_URL.format(post_id))
        post_html_response.raise_for_status()
        post_html = BeautifulSoup(post_html_response.text, "html.parser")
        csrf_token = post_html.select_one("meta[name=\"csrf-token\"]")["content"]

        response = self.session.get(POST_API.format(post_id), headers={
            "X-CSRF-Token": csrf_token,
            "X-Requested-With": "XMLHttpRequest"
        })
        response.raise_for_status()
        post_json = json.loads(response.text)["post"]

        post_id = post_json["id"]
        post_creator = post_json["fanclub"]["creator_name"]
        post_title = post_json["title"]
        post_contents = post_json["post_contents"]

        post_posted_at = int(parsedate_to_datetime(post_json["posted_at"]).timestamp())
        post_converted_at = int(dt.fromisoformat(post_json["converted_at"]).timestamp()) if post_json["converted_at"] else post_posted_at

        if self.db.conn and db_post and db_post["download_complete"]:
            # Check if the post date changed, which may indicate new contents were added
            if db_post["converted_at"] != post_converted_at:
                self.output("Post date does not match date in database. Checking for new contents...\n")
                self.db.update_post_download_complete(post_id, download_complete=0)
                self.db.update_post_converted_at(post_id, post_converted_at)
            else:
                self.output("Post appears to have been downloaded completely. Skipping...\n".format(post_id))
                return
        if self.db.conn and not db_post:
            self.db.insert_post(post_id, post_title, post_json["fanclub"]["id"], post_posted_at, post_converted_at)

        post_directory_title = self._format_post_directory_title(post_id, post_title, post_creator, post_json.get("fanclub", {}).get("id"))

        post_directory = os.path.join(self.directory, sanitize_for_path(post_creator), post_directory_title)
        os.makedirs(post_directory, exist_ok=True)

        post_titles = self.collect_post_titles(post_json)

        if self.dump_metadata:
            self.save_metadata(post_json, post_directory)
        if self.mark_incomplete_posts:
            self.mark_incomplete_post(post_json, post_directory)
        if self.download_thumb and post_json["thumb"]:
            self.download_thumbnail(post_json["thumb"]["original"], post_directory)
        if self.parse_for_external_links:
            # Main post
            post_description = post_json["comment"] or ""
            self.parse_external_links(post_description, os.path.abspath(post_directory))

        download_complete_counter = 0
        for post_index, post in enumerate(post_contents):
            post_title = post_titles[post_index]
            if self.download_post_content(post, post_directory, post_title):
                download_complete_counter += 1
        if self.db.conn and download_complete_counter == len(post_contents):
            self.output("All post content appears to have been downloaded. Marking as complete in database...\n")
            self.db.update_post_download_complete(post_id)

        if not os.listdir(post_directory):
            self.output("No content downloaded for post {}. Deleting directory.\n".format(post_id))
            os.rmdir(post_directory)

    def parse_external_links(self, post_description, post_directory):
        """Parse the post description for external links, e.g. Mega and Google Drive links."""
        link_matches = EXTERNAL_LINKS_RE.findall(post_description)
        if link_matches:
            self.output("Found {} external link(s) in post. Saving...\n".format(len(link_matches)))
            build_crawljob(link_matches, self.directory, post_directory)

    def save_metadata(self, metadata, directory):
        """Save the metadata for a post to the post's directory."""
        filename = os.path.join(directory, "metadata.json")
        with open(filename, "w", encoding='utf-8') as file:
            json.dump(metadata, file, sort_keys=True, ensure_ascii=False, indent=4)

    def mark_incomplete_post(self, post_metadata, post_directory):
        """Mark incomplete posts with a .incomplete file."""
        is_incomplete = False
        incomplete_filename = os.path.join(post_directory, ".incomplete")
        for post in post_metadata["post_contents"]:
            if post["visible_status"] != "visible":
                is_incomplete = True
                break
        if is_incomplete:
            if not os.path.exists(incomplete_filename):
                open(incomplete_filename, 'a').close()
        else:
            if os.path.exists(incomplete_filename):
                os.remove(incomplete_filename)


def guess_extension(mimetype, download_url):
    """
    Guess the file extension from the mimetype or force a specific extension for certain mimetypes.
    If the mimetype returns no found extension, guess based on the download URL.
    """
    extension = MIMETYPES.get(mimetype) or mimetypes.guess_extension(mimetype, strict=True)
    if not extension:
        try:
            path = urlparse(download_url).path
            extension = os.path.splitext(path)[1]
        except IndexError:
            extension = ".unknown"
    return extension

def sanitize_for_path(value, replace=' '):
    """Remove potentially illegal characters from a path."""
    sanitized = re.sub(r'[<>\"\?\\\/\*:|]', replace, value)
    sanitized = sanitized.translate(UNICODE_CONTROL_MAP)
    return re.sub(r'[\s.]+$', '', sanitized)

def build_crawljob(links, root_directory, post_directory):
    """Append to a root .crawljob file with external links gathered from a post."""
    filename = os.path.join(root_directory, CRAWLJOB_FILENAME)
    with open(filename, "a", encoding="utf-8") as file:
        for link in links:
            crawl_dict = {
                "packageName": "Fantia",
                "text": link,
                "downloadFolder": post_directory,
                "enabled": "true",
                "autoStart": "true",
                "forcedStart": "true",
                "autoConfirm": "true",
                "addOfflineLink": "true",
                "extractAfterDownload": "false"
            }

            for key, value in crawl_dict.items():
                file.write(key + "=" + value + "\n")
            file.write("\n")
