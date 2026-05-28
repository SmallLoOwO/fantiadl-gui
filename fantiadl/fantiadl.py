#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Download media and other data from Fantia"""

import argparse
import getpass
import netrc
import sys
import traceback

from .models import FantiaDownloader, FantiaClub, FANTIA_URL_RE
from .__version__ import __version__

__author__ = "bitbybyte"
__copyright__ = "Copyright 2024 bitbybyte"

__license__ = "MIT"

BASE_HOST = "fantia.jp"

cmdl_usage = "%(prog)s [options] url"
cmdl_version = __version__
cmdl_parser = argparse.ArgumentParser(usage=cmdl_usage, conflict_handler="resolve")

cmdl_parser.add_argument("-c", "--cookie", dest="session_arg", metavar="SESSION_COOKIE", help="_session_id cookie or cookies.txt")
cmdl_parser.add_argument("-e", "--email", dest="email", metavar="EMAIL", help=argparse.SUPPRESS)
cmdl_parser.add_argument("-p", "--password", dest="password", metavar="PASSWORD", help=argparse.SUPPRESS)
cmdl_parser.add_argument("-n", "--netrc", action="store_true", dest="netrc", help=argparse.SUPPRESS)
cmdl_parser.add_argument("-q", "--quiet", action="store_true", dest="quiet", help="suppress output")
cmdl_parser.add_argument("-v", "--version", action="version", version=cmdl_version)
cmdl_parser.add_argument("--db", dest="db_path", help="database to track post download state (creates tables when first specified)")
cmdl_parser.add_argument("--db-bypass-post-check", action="store_true", dest="db_bypass_post_check", help="bypass checking a post for new content if it's marked as completed on the database")
cmdl_parser.add_argument("url", action="store", nargs="*", help="fanclub or post URL")

dl_group = cmdl_parser.add_argument_group("download options")
dl_group.add_argument("-i", "--ignore-errors", action="store_true", dest="continue_on_error", help="continue on download errors")
dl_group.add_argument("-l", "--limit", dest="limit", metavar="#", type=int, default=0, help="limit the number of posts to process per fanclub (excludes -n)")
dl_group.add_argument("-o", "--output-directory", dest="output_path", help="directory to download to")
dl_group.add_argument("-s", "--use-server-filenames", action="store_true", dest="use_server_filenames", help="download using server defined filenames")
dl_group.add_argument("-r", "--mark-incomplete-posts", action="store_true", dest="mark_incomplete_posts", help="add .incomplete file to post directories that are incomplete")
dl_group.add_argument("-m", "--dump-metadata", action="store_true", dest="dump_metadata", help="store metadata to file (including fanclub icon, header, and background)")
dl_group.add_argument("-x", "--parse-for-external-links", action="store_true", dest="parse_for_external_links", help="parse posts for external links")
dl_group.add_argument("-t", "--download-thumbnail", action="store_true", dest="download_thumb", help="download post thumbnails")
dl_group.add_argument("-f", "--download-fanclubs", action="store_true", dest="download_fanclubs", help="download posts from all followed fanclubs")
dl_group.add_argument("-p", "--download-paid-fanclubs", action="store_true", dest="download_paid_fanclubs", help="download posts from all fanclubs backed on a paid plan")
dl_group.add_argument("-n", "--download-new-posts", dest="download_new_posts", metavar="#", type=int, help="download a specified number of new posts from your fanclub timeline")
dl_group.add_argument("-d", "--download-month", dest="month_limit", metavar="%Y-%m", help="download posts only from a specific month, e.g. 2007-08 (excludes -n)")
dl_group.add_argument("--exclude", dest="exclude_file", metavar="EXCLUDE_FILE", help="file containing a list of filenames to exclude from downloading")

retry_group = cmdl_parser.add_argument_group("retry options")
retry_group.add_argument("--max-retries", dest="max_retries", type=int, default=10, help="max urllib3 retries per request (default: 10)")
retry_group.add_argument("--retry-backoff", dest="retry_backoff", type=float, default=3, help="urllib3 backoff factor: delay = factor * (2 ** (n-1)) (default: 3)")
retry_group.add_argument("--retry-backoff-max", dest="retry_backoff_max", type=float, default=120, help="cap on a single urllib3 retry sleep in seconds (default: 120)")
retry_group.add_argument("--cooldown-seconds", dest="cooldown_seconds", type=int, default=60, help="base cooldown wait in seconds when inner retries exhaust; scales linearly with cooldown attempt count (default: 60)")
retry_group.add_argument("--cooldown-attempts", dest="cooldown_attempts", type=int, default=10, help="how many outer cooldown rounds to try before giving up on a post / file (default: 10)")
retry_group.add_argument("--sleep-request", dest="sleep_request", type=float, default=0.0, help="sleep this many seconds before EVERY HTTP request; preventative throttle to avoid 429 in the first place (default: 0)")

verify_group = cmdl_parser.add_argument_group("verify options")
verify_group.add_argument("--verify", action="store_true", dest="verify", help="cross-check a fanclub URL against the DB to find missing/incomplete posts (requires --db). On its own this prints a report. Adds an auto-fix download pass by default; pass --verify-no-fix to skip it.")
verify_group.add_argument("--verify-json", dest="verify_json", metavar="PATH", help="write the verify report to this JSON file (defaults to verify_<fanclub_id>.json next to the cwd)")
verify_group.add_argument("--verify-no-fix", action="store_true", dest="verify_no_fix", help="report only; do not redownload incomplete posts")

naming_group = cmdl_parser.add_argument_group("naming options")
naming_group.add_argument("--post-directory-format", dest="post_directory_format", default="{post_id}_{post_title}", help="format string for per-post folder name. Fields: {post_id} {post_title} {post_creator} {fanclub_id}. Default: '{post_id}_{post_title}'. Pass '{post_id}' to restore old ID-only behavior.")
naming_group.add_argument("--max-title-length", dest="max_title_length", type=int, default=80, help="truncate the formatted folder name to this many characters to keep Windows MAX_PATH safe (default: 80; 0 disables)")

order_group = cmdl_parser.add_argument_group("ordering options")
order_group.add_argument("--post-order", dest="post_order", choices=["newest", "oldest", "api"], default="newest", help="order to walk posts in a fanclub (affects both download and --verify). 'newest' = highest post_id first (default), 'oldest' = lowest first, 'api' = whatever Fantia's listing page returned (no sort).")


cmdl_opts = cmdl_parser.parse_args()

def main():
    session_arg = cmdl_opts.session_arg
    email = cmdl_opts.email
    password = cmdl_opts.password

    if (email or password or cmdl_opts.netrc) and not session_arg:
        sys.exit("Logging in from the command line is no longer supported. Please provide a session cookie using -c/--cookie. See the README for more information.")

    if not (cmdl_opts.download_fanclubs or cmdl_opts.download_paid_fanclubs or cmdl_opts.download_new_posts) and not cmdl_opts.url:
        sys.exit("Error: No valid input provided")

    if not session_arg:
        session_arg = input("Fantia session cookie (_session_id or cookies.txt path): ")

    # if cmdl_opts.netrc:
    #     login = netrc.netrc().authenticators(BASE_HOST)
    #     if login:
    #         email = login[0]
    #         password = login[2]
    #     else:
    #         sys.exit("Error: No Fantia login found in .netrc")
    # else:
    #     if not email:
    #         email = input("Email: ")
    #     if not password:
    #         password = getpass.getpass("Password: ")

    try:
        downloader = FantiaDownloader(session_arg=session_arg, dump_metadata=cmdl_opts.dump_metadata, parse_for_external_links=cmdl_opts.parse_for_external_links, download_thumb=cmdl_opts.download_thumb, directory=cmdl_opts.output_path, quiet=cmdl_opts.quiet, continue_on_error=cmdl_opts.continue_on_error, use_server_filenames=cmdl_opts.use_server_filenames, mark_incomplete_posts=cmdl_opts.mark_incomplete_posts, month_limit=cmdl_opts.month_limit, exclude_file=cmdl_opts.exclude_file, db_path=cmdl_opts.db_path, db_bypass_post_check=cmdl_opts.db_bypass_post_check, max_retries=cmdl_opts.max_retries, retry_backoff=cmdl_opts.retry_backoff, retry_backoff_max=cmdl_opts.retry_backoff_max, cooldown_seconds=cmdl_opts.cooldown_seconds, cooldown_attempts=cmdl_opts.cooldown_attempts, sleep_request=cmdl_opts.sleep_request, post_directory_format=cmdl_opts.post_directory_format, max_title_length=cmdl_opts.max_title_length, post_order=cmdl_opts.post_order)
        if cmdl_opts.download_fanclubs:
            try:
                downloader.download_followed_fanclubs(limit=cmdl_opts.limit)
            except KeyboardInterrupt:
                raise
            except:
                if cmdl_opts.continue_on_error:
                    downloader.output("Encountered an error downloading followed fanclubs. Skipping...\n")
                    traceback.print_exc()
                    pass
                else:
                    raise
        elif cmdl_opts.download_paid_fanclubs:
            try:
                downloader.download_paid_fanclubs(limit=cmdl_opts.limit)
            except:
                if cmdl_opts.continue_on_error:
                    downloader.output("Encountered an error downloading paid fanclubs. Skipping...\n")
                    traceback.print_exc()
                    pass
                else:
                    raise
        elif cmdl_opts.download_new_posts:
            try:
                downloader.download_new_posts(post_limit=cmdl_opts.download_new_posts)
            except:
                if cmdl_opts.continue_on_error:
                    downloader.output("Encountered an error downloading new posts from timeline. Skipping...\n")
                    traceback.print_exc()
                    pass
                else:
                    raise
        if cmdl_opts.url:
            for url in cmdl_opts.url:
                    url_match = FANTIA_URL_RE.match(url)
                    if url_match:
                        try:
                            url_groups = url_match.groups()
                            if url_groups[0] == "fanclubs":
                                fanclub = FantiaClub(url_groups[1])
                                if cmdl_opts.verify:
                                    verify_json_path = cmdl_opts.verify_json or "verify_{}.json".format(fanclub.id)
                                    downloader.verify_fanclub(
                                        fanclub,
                                        output_json=verify_json_path,
                                        auto_fix=not cmdl_opts.verify_no_fix,
                                        limit=cmdl_opts.limit,
                                    )
                                else:
                                    downloader.download_fanclub(fanclub, cmdl_opts.limit)
                            elif url_groups[0] == "posts":
                                if cmdl_opts.verify:
                                    downloader.output("--verify only applies to fanclub URLs (got a post URL). Ignoring --verify and downloading the post normally.\n")
                                downloader.download_post(url_groups[1])
                        except KeyboardInterrupt:
                            raise
                        except:
                            if cmdl_opts.continue_on_error:
                                downloader.output("Encountered an error downloading URL. Skipping...\n")
                                traceback.print_exc()
                                continue
                            else:
                                raise
                    else:
                        sys.stderr.write("Error: {} is not a valid URL. Please provide a fully qualified Fantia URL (https://fantia.jp/posts/[id], https://fantia.jp/fanclubs/[id])\n".format(url))
    except KeyboardInterrupt:
        sys.exit("Interrupted by user. Exiting...")


def cli():
    global cmdl_opts
    cmdl_opts = cmdl_parser.parse_args()
    main()

if __name__ == "__main__":
    cli()