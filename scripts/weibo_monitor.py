#!/usr/bin/env python3
"""
Weibo Monitor - Fetches latest posts from Weibo bloggers via m.weibo.cn API.
Requires WEIBO_COOKIE environment variable (GitHub Actions secret).

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

# Get cookie from environment
WEIBO_COOKIE = os.environ.get("WEIBO_COOKIE", "")

# Request headers
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
    """Fetch latest posts from m.weibo.cn API using cookie authentication.

    Returns a list of dicts with keys: title, link, description, published.
    Returns None on failure.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    # Set cookie
    if WEIBO_COOKIE:
        session.headers["Cookie"] = WEIBO_COOKIE

    # First visit m.weibo.cn to establish session
    try:
        session.get("https://m.weibo.cn/", timeout=15)
    except Exception:
        pass

    url = "https://m.weibo.cn/api/container/getIndex"

    for attempt in range(3):
        try:
            if attempt > 0:
                time.sleep(3)

            # Step 1: Get the containerid for the user
            params1 = {"type": "uid", "value": uid}
            resp1 = session.get(url, params=params1, timeout=30)

            if resp1.status_code != 200:
                print(f"  ⚠ m.weibo.cn returned HTTP {resp1.status_code}")
                continue

            data1 = resp1.json()

            if data1.get("ok") != 1:
                msg = data1.get("msg", "unknown error")
                print(f"  ⚠ API error (attempt {attempt+1}/3): {msg}")
                if attempt == 2:
                    print(f"  Response: {json.dumps(data1, ensure_ascii=False)[:300]}")
                continue

            # Extract containerid from tabsInfo
            tabs_info = data1.get("data", {}).get("tabsInfo", {})
            tabs = tabs_info.get("tabs", [])
            container_id = None
            for tab in tabs:
                if tab.get("tab_type") == "weibo":
                    container_id = tab.get("containerid")
                    break

            if not container_id:
                # Fallback: try to get posts directly from the first response
                posts = extract_posts_from_cards(data1, uid)
                if posts:
                    print(f"  ✓ Got {len(posts)} posts (from profile page)")
                    return posts
                print(f"  ⚠ No containerid found")
                continue

            # Step 2: Fetch posts using the containerid
            params2 = {"type": "uid", "value": uid, "containerid": container_id}
            resp2 = session.get(url, params=params2, timeout=30)

            if resp2.status_code != 200:
                print(f"  ⚠ m.weibo.cn posts API returned HTTP {resp2.status_code}")
                continue

            data2 = resp2.json()

            if data2.get("ok") != 1:
                print(f"  ⚠ Posts API error: {data2.get('msg', 'unknown')}")
                continue

            posts = extract_posts_from_cards(data2, uid)

            if posts:
                print(f"  ✓ Got {len(posts)} posts")
                return posts
            else:
                print(f"  ⚠ No posts in response")
                # Try page 2
                params3 = {"type": "uid", "value": uid, "containerid": container_id, "page": 2}
                resp3 = session.get(url, params=params3, timeout=30)
                if resp3.status_code == 200:
                    data3 = resp3.json()
                    posts3 = extract_posts_from_cards(data3, uid)
                    if posts3:
                        print(f"  ✓ Got {len(posts3)} posts (page 2)")
                        return posts3

        except requests.exceptions.RequestException as e:
            print(f"  ⚠ Request failed: {type(e).__name__}: {e}")
        except json.JSONDecodeError as e:
            print(f"  ⚠ JSON parse error: {e}")
        except Exception as e:
            print(f"  ⚠ Error: {type(e).__name__}: {e}")

    return None


def extract_posts_from_cards(data, uid):
    """Extract posts from API response cards."""
    posts = []
    cards = data.get("data", {}).get("cards", [])

    for card in cards:
        # card_type 9 = weibo post
        if card.get("card_type") == 9:
            mblog = card.get("mblog", {})
            if mblog:
                post = parse_mblog(mblog, uid)
                if post:
                    posts.append(post)
        # card_group may contain nested cards
        elif card.get("card_group"):
            for sub_card in card["card_group"]:
                if sub_card.get("card_type") == 9:
                    mblog = sub_card.get("mblog", {})
                    if mblog:
                        post = parse_mblog(mblog, uid)
                        if post:
                            posts.append(post)

    return posts


def parse_mblog(mblog, uid):
    """Parse an mblog object into a post dict."""
    try:
        id_str = mblog.get("id", "")
        bid = mblog.get("bid", "")

        if id_str:
            link = f"https://weibo.com/{uid}/{id_str}"
        elif bid:
            link = f"https://weibo.com/{uid}/{bid}"
        else:
            return None

        # Get text content
        raw_text = mblog.get("text", "")
        text = strip_html(raw_text)

        # Get long text if available
        if mblog.get("isLongText"):
            long_text = mblog.get("longText", {}).get("longTextContent", "")
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
        if pics:
            pic_urls = [p.get("large", {}).get("url", p.get("url", "")) for p in pics]
            text += "\n\n📷 图片:\n" + "\n".join(pic_urls)

        # Get created_at
        created_at = mblog.get("created_at", "")

        # Title = first 50 chars
        title = text[:50] + ("..." if len(text) > 50 else "")

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
    if WEIBO_COOKIE:
        print(f"  Cookie: configured ({len(WEIBO_COOKIE)} chars)")
    else:
        print(f"  ⚠ No WEIBO_COOKIE set! API will likely fail.")
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
