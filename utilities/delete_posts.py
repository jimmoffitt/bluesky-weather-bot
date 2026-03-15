#!/usr/bin/env python3
"""
delete_posts.py — Delete posts (and optionally replies) from a Bluesky account.

Safeguards:
  • Requires --match TEXT  to only delete posts whose text contains TEXT.
  • Dry-run mode is ON by default; pass --delete to actually delete.
  • Prints every post it would delete before doing anything.
  • Asks for confirmation before deleting unless --yes is passed.
  • Limits deletions to --limit N posts per run (default 50).

Usage examples:

  # Preview posts containing "ZipWx Integration test" (dry run)
  python3 scripts/delete_posts.py --match "ZipWx Integration test"

  # Delete up to 20 matching posts (with confirmation prompt)
  python3 scripts/delete_posts.py --match "ZipWx Integration test" --delete --limit 20

  # Delete matching posts AND their reply threads
  python3 scripts/delete_posts.py --match "ZipWx Integration test" --delete --replies

  # Delete without confirmation (CI / non-interactive)
  python3 scripts/delete_posts.py --match "ZipWx Integration test" --delete --yes

  # Delete posts matching multiple terms (AND logic) — e.g. old-format ZipWx posts
  python3 scripts/delete_posts.py --match "#ZipWx" --match "Wx" --replies --delete

  # Use explicit credentials instead of .local.env
  python3 scripts/delete_posts.py --handle zipwx.bsky.social --password "xxxx-xxxx" \\
      --match "ZipWx Integration test" --delete

Credentials are loaded from .local.env (BSKY_HANDLE / BSKY_APP_PASSWORD) unless
--handle / --password are supplied on the command line.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Allow running from anywhere in the repo
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent.parent))


def _load_env_credentials() -> tuple[str, str]:
    """Load handle + app-password from .local.env if present."""
    env_file = Path(__file__).parent.parent / ".local.env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    handle   = os.environ.get("BSKY_HANDLE", "")
    password = os.environ.get("BSKY_APP_PASSWORD", "")
    return handle, password


def _login(handle: str, password: str):
    try:
        from atproto import Client
    except ImportError:
        print("ERROR: atproto not installed — pip install atproto", file=sys.stderr)
        sys.exit(1)
    client = Client()
    client.login(handle, password)
    return client


def _fetch_author_feed(client, actor: str, limit: int, include_replies: bool) -> list[dict]:
    """
    Pages through the author's feed and returns raw post view dicts.
    Stops when `limit` matching candidates are collected or the feed is exhausted.
    """
    posts: list[dict] = []
    cursor: Optional[str] = None
    PAGE = 100  # max Bluesky allows per request

    while len(posts) < limit * 10:  # fetch generously; filter happens later
        params = {"actor": actor, "limit": PAGE}
        if cursor:
            params["cursor"] = cursor

        resp = client.app.bsky.feed.get_author_feed(params)
        feed = resp.feed or []
        if not feed:
            break

        for item in feed:
            post_view = getattr(item, "post", None)
            if post_view is None:
                continue
            # Optionally skip replies (posts whose record has a reply ref)
            if not include_replies:
                record = getattr(post_view, "record", None)
                if getattr(record, "reply", None) is not None:
                    continue
            posts.append(post_view)

        cursor = getattr(resp, "cursor", None)
        if not cursor:
            break

    return posts


def _post_text(post_view) -> str:
    record = getattr(post_view, "record", None)
    return getattr(record, "text", "") or ""


def _post_uri(post_view) -> str:
    return getattr(post_view, "uri", "") or ""


def _post_created_at(post_view) -> str:
    record = getattr(post_view, "record", None)
    return getattr(record, "created_at", "") or ""


def _delete_post(client, uri: str) -> bool:
    """Delete a single post by AT-URI. Returns True on success."""
    try:
        # AT-URI format: at://did/app.bsky.feed.post/rkey
        parts = uri.split("/")
        rkey = parts[-1]
        client.app.bsky.feed.post.delete(client.me.did, rkey)
        return True
    except Exception as exc:
        print(f"  ERROR deleting {uri}: {exc}", file=sys.stderr)
        return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete Bluesky posts matching a text filter.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--handle",   help="Bluesky handle (overrides .local.env)")
    parser.add_argument("--password", help="App password  (overrides .local.env)")
    parser.add_argument(
        "--match", required=True, metavar="TEXT", action="append",
        help="Only delete posts whose text contains this string (case-insensitive). "
             "Repeat to require multiple matches (AND logic).",
    )
    parser.add_argument(
        "--replies", action="store_true",
        help="Include reply posts (default: only top-level posts)",
    )
    parser.add_argument(
        "--limit", type=int, default=50, metavar="N",
        help="Maximum number of posts to delete in one run (default: 50)",
    )
    parser.add_argument(
        "--delete", action="store_true",
        help="Actually delete posts. Without this flag the script is a dry run.",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the confirmation prompt (use with --delete).",
    )
    parser.add_argument(
        "--delay", type=float, default=0.3, metavar="SEC",
        help="Seconds to wait between deletions (default: 0.3)",
    )
    args = parser.parse_args()

    # --- Credentials ---
    env_handle, env_password = _load_env_credentials()
    handle   = args.handle   or env_handle
    password = args.password or env_password
    if not handle or not password:
        print("ERROR: Bluesky handle/password not found. Set BSKY_HANDLE and "
              "BSKY_APP_PASSWORD in .local.env or pass --handle / --password.",
              file=sys.stderr)
        sys.exit(1)

    match_texts = [m.lower() for m in args.match]

    print(f"Logging in as {handle} ...")
    client = _login(handle, password)
    actor  = client.me.did
    print(f"Authenticated. DID: {actor}")

    # --- Fetch ---
    print(f"\nFetching posts{'+ replies' if args.replies else ''} from @{handle} ...")
    all_posts = _fetch_author_feed(client, actor, limit=args.limit, include_replies=args.replies)
    print(f"Fetched {len(all_posts)} post(s) from feed.")

    # --- Filter ---
    matching = [p for p in all_posts if all(m in _post_text(p).lower() for m in match_texts)]
    matching = matching[: args.limit]

    match_label = " AND ".join(repr(m) for m in args.match)

    if not matching:
        print(f"\nNo posts found matching {match_label}. Nothing to do.")
        return

    # --- Preview ---
    mode = "DRY RUN" if not args.delete else "TO DELETE"
    print(f"\n{mode} — {len(matching)} post(s) matching {match_label}:\n")
    for i, post in enumerate(matching, 1):
        ts   = _post_created_at(post)[:19].replace("T", " ")
        text = _post_text(post).replace("\n", " ")[:120]
        uri  = _post_uri(post)
        print(f"  {i:3}. [{ts}] {text}")
        print(f"       {uri}")

    if not args.delete:
        print(f"\nDry run complete. Pass --delete to actually remove these {len(matching)} post(s).")
        return

    # --- Confirm ---
    if not args.yes:
        print(f"\n⚠️  About to permanently delete {len(matching)} post(s) from @{handle}.")
        answer = input("Type 'yes' to confirm: ").strip().lower()
        if answer != "yes":
            print("Aborted.")
            return

    # --- Delete ---
    print(f"\nDeleting {len(matching)} post(s) ...")
    deleted = 0
    failed  = 0
    for post in matching:
        uri  = _post_uri(post)
        text = _post_text(post).replace("\n", " ")[:80]
        if _delete_post(client, uri):
            print(f"  ✓ Deleted: {text}")
            deleted += 1
        else:
            failed += 1
        if args.delay > 0:
            time.sleep(args.delay)

    print(f"\nDone. Deleted: {deleted}  Failed: {failed}")


if __name__ == "__main__":
    main()
