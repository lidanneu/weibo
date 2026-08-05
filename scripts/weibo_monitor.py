#!/usr/bin/env python3
"""
Weibo Monitor - Fetches latest posts from Weibo bloggers via RSSHub.
Designed to run on GitHub Actions every 10 minutes.
Uses a self-hosted RSSHub Docker instance (localhost:1200) with public fallbacks.

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
        "name": "岚论",
        "uid": "1657450041",
        "url": "https://weibo.com/u/1657450041",
        "dir": "weibo_岚论",
    },
    {
        "name": "菩提树下那道光",
        "uid": "1002568141",
        "url": "https://weibo.com/u/1002568141",
        "dir": "weibo_菩提树下那道光",
    },
]

# RSSHub instances in fallback order (self-hosted first)
RSSHUB_INSTANCES = [
    "http://localhost:1200",
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
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
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
            found = re.findall(r"https?://[^\s\)\]\|]+", content)
            for url in found:
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
    for instance in RSSHUB_INSTANCES:
        rss_url = f"{instance}/weibo/user/{uid}"
        for attempt in range(2):
            try:
                if attempt > 0:
                    time.sleep(2)
                resp = requests.get(rss_url, headers=HEADERS, timeout=30)
                if resp.status_code == 200:
                    feed = feedparser.parse(resp.text)
                    if feed.entries:
                        print(f"  ✓ {instance} returned {len(feed.entries)} entries")
                        return feed
                    else:
                        print(f"  ⚠ {instance} returned 200 but no entries")
                else:
                    print(f"  ⚠ {instance} returned HTTP {resp.status_code}")
            except requests.exceptions.RequestException as e:
                # Don't print connection errors for localhost if it's not ready
                if "localhost" in instance:
                    print(f"  ⚠ {instance} not available")
                else:
                    print(f"  ⚠ {instance} failed: {type(e).__name__}")
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

    md_content = f"# 微博博主「{blogger['name']}」发文记录\n\n"
    md_content += f"| 项目 | 内容 |\n|------|------|\n"
    md_content += f"| 博主名称 | {blogger['name']} |\n"
    md_content += f"| 微博主页 | {blogger['url']} |\n"
    md_content += f"| 抓取时间 | {now_full} |\n"
    md_content += f"| 本次抓取条数 | {len(posts_to_save)} |\n"
    md_content += f"| 数据来源 | RSSHub |\n\n---\n\n"

    for i, entry in enumerate(posts_to_save, 1):
        title = entry.get("title", "无标题")
        link = entry.get("link", "")
        description = entry.get("description", "") or entry.get("summary", "")
        content_text = strip_html(description)
        published = entry.get("published", entry.get("updated", ""))

        md_content += f"### {i}. {title}\n\n"
        md_content += f"**链接**: {link}\n\n"
        if published:
            md_content += f"**发布时间**: {published}\n\n"
        md_content += f"**正文**:\n\n{content_text}\n\n"
        md_content += f"---\n\n"

    md_content += f"\n> 数据来源: RSSHub\n"

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
