#!/usr/bin/env python3
"""
Weibo Monitor - Fetches latest posts from Weibo bloggers via m.weibo.cn API.
Designed to run on GitHub Actions every 10 minutes.

First run: saves the latest 1 post per blogger.
Subsequent runs: saves only new posts (detected by link comparison).
No new posts = no file generated (keeps repo clean).
"""

import os
import re
import sys
import time
import json
from datetime import datetime
from html import unescape

import requests

# ============================================================
# Bloggers — only need UID
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

# Request headers to mimic a mobile browser
HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://m.weibo.cn/",
    "MWeibo-Pwa": "1",
    "X-Requested-With": "XMLHttpRequest",
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


def fetch_weibo_posts(uid):
    """Fetch latest posts from m.weibo.cn API.

    Returns a list of dicts with keys: title, link, description, published.
    Returns None on failure.
    """
    url = f"https://m.weibo.cn/api/container/getIndex"
    params = {"type": "uid", "value": uid}

    for attempt in range(3):
        try:
            if attempt > 0:
                time.sleep(2 ** attempt)
                # Add a random cookie for retry to bypass rate limiting
                HEADERS["Cookie"] = f"_T_WM=retry{attempt}"

            resp = requests.get(url, headers=HEADERS, params=params, timeout=30)

            if resp.status_code != 200:
                print(f"  ⚠ m.weibo.cn returned HTTP {resp.status_code}")
                continue

            data = resp.json()

            if data.get("ok") != 1:
                msg = data.get("msg", "unknown error")
                print(f"  ⚠ m.weibo.cn API error: {msg}")
                # HTTP 432 usually means WAF block
                continue

            # Extract posts from cards
            posts = []
            cards = data.get("data", {}).get("cards", [])
            for card in cards:
                # card_type 9 = weibo post
                if card.get("card_type") == 9:
                    mblog = card.get("mblog", {})
                    if not mblog:
                        continue
                    post = parse_mblog(mblog, uid)
                    if post:
                        posts.append(post)
                # card_group may contain nested cards
                elif card.get("card_group"):
                    for sub_card in card["card_group"]:
                        if sub_card.get("card_type") == 9:
                            mblog = sub_card.get("mblog", {})
                            if not mblog:
                                continue
                            post = parse_mblog(mblog, uid)
                            if post:
                                posts.append(post)

            if posts:
                return posts
            else:
                print(f"  No posts found in API response")
                # Try to get the next page
                return []

        except requests.exceptions.RequestException as e:
            print(f"  ⚠ m.weibo.cn request failed: {e}")
        except json.JSONDecodeError as e:
            print(f"  ⚠ m.weibo.cn response parse error: {e}")
        except Exception as e:
            print(f"  ⚠ Unexpected error: {e}")

    return None


def parse_mblog(mblog, uid):
    """Parse an mblog object into a post dict."""
    try:
        bid = mblog.get("bid", "")
        id_str = mblog.get("id", "")
        # Build the weibo link
        if id_str:
            link = f"https://weibo.com/{uid}/{id_str}"
        elif bid:
            link = f"https://weibo.com/{uid}/{bid}"
        else:
            return None

        # Get text content
        raw_text = mblog.get("text", "")
        text = strip_html(raw_text)

        # Get title (if long post, use the title field)
        title = mblog.get("page_title", "") or text[:50]

        # Get created_at
        created_at = mblog.get("created_at", "")

        # Get long text if available (retweeted/long post)
        long_text = mblog.get("longText", {}).get("longTextContent", "") if mblog.get("isLongText") else ""
        if long_text:
            text = strip_html(long_text)

        # Get original post if retweet
        retweeted_status = mblog.get("retweeted_status", {})
        if retweeted_status:
            retweet_text = strip_html(retweeted_status.get("text", ""))
            retweet_user = retweeted_status.get("user", {}).get("screen_name", "")
            text += f"\n\n🔁 转发 @{retweet_user}:\n{retweet_text}"

        # Get images
        pics = mblog.get("pics", [])
        pic_urls = [p.get("large", {}).get("url", p.get("url", "")) for p in pics] if pics else []
        if pic_urls:
            text += "\n\n📷 图片:\n" + "\n".join(pic_urls)

        return {
            "title": title,
            "link": link,
            "description": text,
            "published": created_at,
        }
    except Exception as e:
        print(f"  ⚠ Error parsing mblog: {e}")
        return None


def process_blogger(blogger):
    """Process a single blogger: fetch posts, detect new ones, save to file."""
    blogger_dir = blogger["dir"]
    os.makedirs(blogger_dir, exist_ok=True)

    print(f"  Fetching posts for UID: {blogger['uid']}")
    posts = fetch_weibo_posts(blogger["uid"])

    if posts is None:
        print(f"  Failed to fetch posts for {blogger['name']}")
        return False

    if not posts:
        print(f"  No posts returned for {blogger['name']}")
        return False

    print(f"  Total posts fetched: {len(posts)}")

    # Get all links already recorded
    existing_links = get_existing_links(blogger_dir)
    print(f"  Existing links recorded: {len(existing_links)}")

    # Check if first run (no existing .md files)
    md_files = [f for f in os.listdir(blogger_dir) if f.endswith(".md")]
    is_first_run = len(md_files) == 0

    if is_first_run:
        print(f"  First run: saving only the latest 1 post")
        posts_to_save = [posts[0]] if posts else []
    else:
        # Find new posts by link comparison
        posts_to_save = []
        for post in posts:
            if post["link"] not in existing_links:
                posts_to_save.append(post)
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
    md_content += f"| 数据来源 | m.weibo.cn API |\n\n---\n\n"

    for i, post in enumerate(posts_to_save, 1):
        title = post["title"] or "无标题"
        link = post["link"]
        content_text = post["description"]
        published = post["published"]

        md_content += f"### {i}. {title}\n\n"
        md_content += f"**链接**: {link}\n\n"
        if published:
            md_content += f"**发布时间**: {published}\n\n"
        md_content += f"**正文**:\n\n{content_text}\n\n"
        md_content += f"---\n\n"

    md_content += f"\n> 数据来源: m.weibo.cn API\n"

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
