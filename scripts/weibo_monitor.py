#!/usr/bin/env python3
"""
Weibo Monitor - Fetches latest posts from Weibo bloggers via weibo.cn WAP page.
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

# Mobile browser headers for weibo.cn
HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://weibo.cn/",
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
    """Fetch latest posts from weibo.cn WAP page.

    Returns a list of dicts with keys: title, link, description, published.
    Returns None on failure.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    for attempt in range(3):
        try:
            if attempt > 0:
                time.sleep(3)

            # First visit the homepage to get cookies
            if attempt == 0:
                session.get("https://weibo.cn/", timeout=15)

            # Fetch the user's profile page
            url = f"https://weibo.cn/{uid}"
            resp = session.get(url, timeout=30)

            if resp.status_code != 200:
                print(f"  ⚠ weibo.cn returned HTTP {resp.status_code}")
                continue

            html = resp.text

            # Check if we got redirected to a login page
            if "登录" in html and "密码" in html and len(html) < 5000:
                print(f"  ⚠ Redirected to login page")
                continue

            posts = parse_weibo_cn_html(html, uid)

            if posts:
                print(f"  ✓ Parsed {len(posts)} posts from weibo.cn")
                return posts
            else:
                print(f"  ⚠ No posts found in page (page size: {len(html)} bytes)")
                # Try page 2 as well
                resp2 = session.get(f"https://weibo.cn/{uid}?page=2", timeout=30)
                if resp2.status_code == 200:
                    posts2 = parse_weibo_cn_html(resp2.text, uid)
                    if posts2:
                        print(f"  ✓ Parsed {len(posts2)} posts from page 2")
                        return posts2
                continue

        except requests.exceptions.RequestException as e:
            print(f"  ⚠ weibo.cn request failed: {type(e).__name__}: {e}")
        except Exception as e:
            print(f"  ⚠ Unexpected error: {type(e).__name__}: {e}")

    return None


def parse_weibo_cn_html(html, uid):
    """Parse weibo.cn HTML page to extract posts.

    weibo.cn uses simple HTML with divs containing post content.
    Each post has a link like /{uid}/{post_id}
    """
    posts = []

    # Find all post links - format: /{uid}/{post_id} (without /u/)
    # Also try the pattern href="/uid/postid"
    post_pattern = rf'href="https?://weibo\.cn/{uid}/(\w+)"'
    post_ids = re.findall(post_pattern, html)

    if not post_ids:
        # Try alternative pattern: href="/uid/postid"
        post_pattern2 = rf'href="/{uid}/(\w+)"'
        post_ids = re.findall(post_pattern2, html)

    if not post_ids:
        # Try weibo.com pattern
        post_pattern3 = rf'href="https?://weibo\.com/{uid}/(\w+)"'
        post_ids = re.findall(post_pattern3, html)

    # Deduplicate while preserving order
    seen_ids = set()
    unique_post_ids = []
    for pid in post_ids:
        if pid not in seen_ids and pid != uid:
            seen_ids.add(pid)
            unique_post_ids.append(pid)

    # Extract post content blocks
    # weibo.cn posts are in <div class="c"> elements
    # Each post block contains the text and a link to the post
    div_blocks = re.findall(r'<div class="c"[^>]*>(.*?)</div>', html, re.DOTALL)

    for post_id in unique_post_ids[:10]:  # Limit to 10 posts
        link = f"https://weibo.com/{uid}/{post_id}"

        # Try to find the content for this post
        content = ""

        # Search for the post_id in div blocks to find the right block
        for block in div_blocks:
            if post_id in block:
                # Extract text content - remove HTML tags
                # Remove link tags first
                block = re.sub(r'<a[^>]*>.*?</a>', '', block, flags=re.DOTALL)
                # Remove spans with style (usually timestamps or metadata)
                block = re.sub(r'<span[^>]*class="[^"]*ct[^"]*"[^>]*>.*?</span>', '', block, flags=re.DOTALL)
                content = strip_html(block)
                break

        if not content:
            # Fallback: try to extract any text near the post_id
            # Look for text before the link
            pattern = rf'(.*?)href="[^"]*{post_id}"'
            match = re.search(pattern, html, re.DOTALL)
            if match:
                content = strip_html(match.group(1))
            else:
                content = "(content not available)"

        # Try to find the timestamp
        published = ""
        # Look for timestamp pattern near the post
        time_pattern = rf'{post_id}.*?(\d{{4}}-\d{{2}}-\d{{2}}[\s\d:]+(?:来自[^\s<]+)?)'
        time_match = re.search(time_pattern, html, re.DOTALL)
        if time_match:
            published = strip_html(time_match.group(1))

        # Also try the simpler pattern: date string in the block
        if not published:
            date_pattern = r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)'
            date_match = re.search(date_pattern, html)
            if date_match:
                published = date_match.group(1)

        # Title = first 50 chars of content
        title = content[:50] + ("..." if len(content) > 50 else "")

        posts.append({
            "title": title,
            "link": link,
            "description": content,
            "published": published,
        })

    return posts


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
    md_content += f"| 数据来源 | weibo.cn |\n\n---\n\n"

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

    md_content += f"\n> 数据来源: weibo.cn\n"

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
