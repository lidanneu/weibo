#!/usr/bin/env python3
"""
Weibo Monitor - Fetches latest posts from Weibo bloggers via RSSHub.
Designed to run on GitHub Actions every 10 minutes.

First run: saves the latest 1 post per blogger.
Subsequent runs: saves only new posts (detected by link comparison).
No new posts = no file generated (keeps repo clean).
"""

import os
import re
import sys
import time
from datetime import datetime
from html import unescape

import requests
import feedparser

# ============================================================
# Bloggers — only need UID, RSS URL is built from instances
# ============================================================
BLOGGERS = [
    {
        "name": "\u5c9a\u8bba",
        "uid": "1657450041",
        "url": "https://weibo.com/u/1657450041",
        "dir": "weibo_\u5c9a\u8bba",
    },
    {
        "name": "\u83e9\u63d0\u6811\u4e0b\u90a3\u9053\u5149",
        "uid": "1002568141",
        "url": "https://weibo.com/u/1002568141",
        "dir": "weibo_\u83e9\u63d0\u6811\u4e0b\u90a3\u9053\u5149",
    },
]

# RSSHub instances in fallback order
RSSHUB_INSTANCES = [
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://rsshub.pseudoyu.com",
]

# Request headers to mimic a normal browser
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


def strip_html(text):
    """Remove HTML tags and unescape HTML entities."""
    # Remove script and style elements
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Remove all HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Unescape HTML entities
    text = unescape(text)
    # Clean up excessive whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def get_existing_links(blogger_dir):
    """Get all Weibo post links already recorded in existing .md files."""
    links = set()
    if not os.path.exists(blogger_dir):
        return links
    for filename in sorted(os.listdir(blogger_dir)):
        if not filename.endswith(".md"):
            continue
        filepath = os.path.join(blogger_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
            # Extract all URLs from the file
            found = re.findall(r"https?://[^\s\)\]\|]+", content)
            for url in found:
                # Only keep weibo.com post links (format: weibo.com/UID/POSTID)
                # Skip RSSHub source URLs, profile URLs (/u/), and image URLs (sinaimg.cn)
                if "weibo.com" not in url:
                    continue
                if "/u/" in url:
                    continue
                links.add(url)
        except Exception as e:
            print(f"  Warning: Could not read {filepath}: {e}")
    return links


def fetch_rss(uid):
    """Fetch RSS feed from RSSHub with fallback instances and retries."""
    last_error = None
    for instance in RSSHUB_INSTANCES:
        rss_url = f"{instance}/weibo/user/{uid}"
        for attempt in range(3):
            try:
                if attempt > 0:
                    time.sleep(2 ** attempt)
                resp = requests.get(rss_url, headers=HEADERS, timeout=30)
                if resp.status_code == 200:
                    return feedparser.parse(resp.text)
                else:
                    print(f"  ⚠ {instance} returned HTTP {resp.status_code}")
            except requests.exceptions.RequestException as e:
                print(f"  ⚠ {instance} failed: {e}")
                last_error = e
                break  # Network error, skip to next instance
        print(f"  → Switching to next RSSHub instance...")
    return None


def process_blogger(blogger):
    """Process a single blogger: fetch RSS, detect new posts, save to file."""
    blogger_dir = blogger["dir"]
    os.makedirs(blogger_dir, exist_ok=True)

    print(f"  Fetching RSS for UID: {blogger['uid']}")
    feed = fetch_rss(blogger["uid"])

    if not feed:
        print(f"  Failed to fetch feed for {blogger['name']}")
        return False

    if not feed.entries:
        print(f"  No entries in feed for {blogger['name']}")
        return False

    feed_title = feed.feed.get("title", blogger["name"])
    print(f"  Feed title: {feed_title}")
    print(f"  Total entries in feed: {len(feed.entries)}")

    # Get all links already recorded
    existing_links = get_existing_links(blogger_dir)
    print(f"  Existing links recorded: {len(existing_links)}")

    # Check if first run (no existing .md files)
    md_files = [f for f in os.listdir(blogger_dir) if f.endswith(".md")]
    is_first_run = len(md_files) == 0

    if is_first_run:
        print(f"  First run: saving only the latest 1 post")
        posts_to_save = [feed.entries[0]] if feed.entries else []
    else:
        # Find new posts by link comparison
        posts_to_save = []
        for entry in feed.entries:
            link = entry.get("link", "")
            if link and link not in existing_links:
                posts_to_save.append(entry)
        print(f"  New posts found: {len(posts_to_save)}")

    if not posts_to_save:
        print(f"  No new posts for {blogger['name']} - skipping")
        return False

    # Generate markdown content
    now_dt = datetime.now()
    now_str = now_dt.strftime("%Y-%m-%d_%H-%M")
    now_full = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    md_content = f"# \u5fae\u535a\u535a\u4e3b\u300c{blogger['name']}\u300d\u53d1\u6587\u8bb0\u5f55\n\n"
    md_content += f"| \u9879\u76ee | \u5185\u5bb9 |\n|------|------|\n"
    md_content += f"| \u535a\u4e3b\u540d\u79f0 | {blogger['name']} |\n"
    md_content += f"| \u5fae\u535a\u4e3b\u9875 | {blogger['url']} |\n"
    md_content += f"| \u6293\u53d6\u65f6\u95f4 | {now_full} |\n"
    md_content += f"| \u672c\u6b21\u6293\u53d6\u6761\u6570 | {len(posts_to_save)} |\n"
    md_content += f"| \u6570\u636e\u6765\u6e90 | RSSHub |\n\n---\n\n"

    for i, entry in enumerate(posts_to_save, 1):
        title = entry.get("title", "\u65e0\u6807\u9898")
        link = entry.get("link", "")
        # Try description first, then summary
        description = entry.get("description", "") or entry.get("summary", "")
        content_text = strip_html(description)

        md_content += f"### {i}. {title}\n\n"
        md_content += f"**\u94fe\u63a5**: {link}\n\n"
        md_content += f"**\u6b63\u6587**:\n\n{content_text}\n\n"
        md_content += f"---\n\n"

    md_content += f"\n> \u6570\u636e\u6765\u6e90: RSSHub\n"

    # Save file
    filename = f"{now_str}.md"
    filepath = os.path.join(blogger_dir, filename)

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(md_content)
        print(f"  Saved {len(posts_to_save)} posts to {filepath}")
        return True
    except Exception as e:
        print(f"  Error saving file: {e}")
        return False


def main():
    """Main entry point."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{'=' * 60}")
    print(f"  Weibo Monitor - Started at {now_str}")
    print(f"{'=' * 60}")

    any_saved = False
    for blogger in BLOGGERS:
        print(f"\nProcessing: {blogger['name']} (UID: {blogger['uid']})")
        print(f"{'- ' * 30}")
        saved = process_blogger(blogger)
        if saved:
            any_saved = True

    print(f"\n{'=' * 60}")
    end_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"  Weibo Monitor - Finished at {end_str}")
    if any_saved:
        print("  New posts were saved!")
    else:
        print("  No new posts found.")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
