#!/usr/bin/env python3
"""
Weibo Monitor - Fetches latest posts from Weibo bloggers via weibo.com AJAX API.

Uses the WEIBO_COOKIE (weibo.com login cookie, set as a GitHub Actions secret) to
call https://weibo.com/ajax/statuses/mymblog, which returns the blogger's posts.

CRITICAL: For long posts, the list API may truncate or mark isLongText=True.
We use the /ajax/statuses/longtext endpoint to fetch the FULL text for EVERY
post when needed, ensuring the complete content is preserved even if the
blogger deletes it later.

Requires WEIBO_COOKIE environment variable (GitHub Actions secret).
Get the cookie from weibo.com (desktop) Chrome DevTools after logging in.

First run: saves the latest 1 post per blogger.
Subsequent runs: saves only new posts (detected by link comparison).
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
    {
        "name": "岚skl",
        "uid": "8018491606",
        "url": "https://weibo.com/u/8018491606",
        "dir": "weibo_岚skl",
    },
]

WEIBO_COOKIE = os.environ.get("WEIBO_COOKIE", "")

DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
MOBILE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"


def strip_html(text):
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def get_existing_links(blogger_dir):
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


def _extract_xsrf():
    """Extract XSRF-TOKEN value from the WEIBO_COOKIE string (weibo.com cookie)."""
    for part in WEIBO_COOKIE.split(";"):
        part = part.strip()
        if part.startswith("XSRF-TOKEN="):
            return part.split("=", 1)[1]
    return None


def make_session():
    """Build an authenticated requests.Session for the weibo.com AJAX API.

    We use the weibo.com cookie directly (no m.weibo.cn SSO needed). The cookie
    already carries the weibo.com SUB/SUBP and the XSRF-TOKEN required for the
    x-xsrf-token header. m.weibo.cn's JSON getIndex API is now WAF-blocked
    (HTTP 403), so we switched to weibo.com's own AJAX endpoint.
    """
    if not WEIBO_COOKIE:
        return None

    session = requests.Session()
    session.headers.update({
        "User-Agent": DESKTOP_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://weibo.com/",
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": WEIBO_COOKIE,
    })
    xsrf = _extract_xsrf()
    if xsrf:
        session.headers["x-xsrf-token"] = xsrf
    return session


def fetch_post_full_text(post_id, session, list_len=0):
    """Fetch the FULL text of a single post via weibo.com longtext API.

    Long posts in the myblog list may be truncated or marked isLongText=True.
    This endpoint returns the complete long text content.

    Returns the full text string, or None on failure.
    """
    url = f"https://weibo.com/ajax/statuses/longtext?id={post_id}"
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get("ok") != 1:
            return None
        long_text = data.get("data", {}).get("longTextContent", "")
        if long_text:
            full = strip_html(long_text)
            if list_len:
                print(f"    ✓ Full text: {len(full)} chars (vs list: {list_len} chars)")
            else:
                print(f"    ✓ Full text: {len(full)} chars")
            return full
        else:
            print(f"    ⚠ longtext API returned ok but no longTextContent")
    except Exception as e:
        print(f"    ⚠ Full text fetch error for {post_id}: {e}")
    return None


def fetch_weibo_posts(uid, session):
    """Fetch posts from weibo.com AJAX API (mymblog). Returns list of post dicts."""
    url = "https://weibo.com/ajax/statuses/mymblog"
    all_posts = []

    for page in range(1, 4):  # up to 3 pages (~30-45 posts)
        try:
            if page > 1:
                time.sleep(2)
            params = {"uid": uid, "page": page, "feature": 0}
            resp = session.get(url, params=params, timeout=30)

            if resp.status_code != 200:
                print(f"  ⚠ HTTP {resp.status_code} on page {page}")
                break

            data = resp.json()
            if data.get("ok") != 1:
                print(f"  ⚠ API error (page {page}): {data.get('msg', 'unknown')}")
                break

            posts_list = data.get("data", {}).get("list", [])
            if not posts_list:
                break

            for mblog in posts_list:
                post = parse_weibo_post(mblog, uid)
                if post:
                    all_posts.append(post)

            # Stop if fewer than a full page was returned
            if len(posts_list) < 10:
                break

        except Exception as e:
            print(f"  ⚠ Error on page {page}: {type(e).__name__}: {e}")
            break

    if all_posts:
        print(f"  ✓ Got {len(all_posts)} posts")
    return all_posts if all_posts else None


def parse_weibo_post(mblog, uid):
    """Parse a weibo.com myblog post dict into a post dict.
    Fetches full text for long posts later (in enrich)."""
    try:
        pid = str(mblog.get("id", "") or mblog.get("mid", ""))
        mblogid = mblog.get("mblogid", "") or mblog.get("bid", "")
        if pid:
            link = f"https://weibo.com/{uid}/{pid}"
        elif mblogid:
            link = f"https://weibo.com/{uid}/{mblogid}"
        else:
            return None

        raw_text = mblog.get("text", "")
        text = strip_html(raw_text)
        is_long = mblog.get("isLongText", False)

        # weibo.com myblog usually includes full longTextContent for long posts
        if is_long:
            long_text = mblog.get("longTextContent", "")
            if long_text:
                text = strip_html(long_text)
                is_long = False  # already got full text

        post = {
            "title": text[:50] + ("..." if len(text) > 50 else ""),
            "link": link,
            "description": text,
            "published": mblog.get("created_at", ""),
            "_id": pid,
            "_is_long": is_long,
            "_mblog": mblog,  # keep raw data for enrichment
        }
        return post
    except Exception as e:
        print(f"  ⚠ Parse error: {e}")
        return None


def enrich_post_full_text(post, session):
    """Fetch and update full text for a single post. Handles both the main post
    and any retweeted post's full text."""
    if not post.get("_is_long") or not post.get("_id"):
        return

    # Fetch full text for the main post
    cur_len = len(post.get("description", ""))
    full = fetch_post_full_text(post["_id"], session, list_len=cur_len)
    if full:
        post["description"] = full
        post["title"] = full[:50] + ("..." if len(full) > 50 else "")

    # Also fetch full text for retweeted posts if needed
    mblog = post.get("_mblog", {})
    retweeted = mblog.get("retweeted_status") or {}
    if retweeted and retweeted.get("isLongText"):
        retweet_id = retweeted.get("id", "")
        if retweet_id:
            retweet_full = fetch_post_full_text(retweet_id, session)
            if retweet_full:
                retweeted["_full_text"] = retweet_full


def finalize_post_content(post):
    """After enrichment, finalize the post content with retweets and images."""
    text = post.get("description", "")
    mblog = post.pop("_mblog", {})
    post.pop("_id", None)
    post.pop("_is_long", None)

    # Handle retweet
    retweeted = mblog.get("retweeted_status") or {}
    if retweeted:
        retweet_text = retweeted.get("_full_text", "")
        if not retweet_text:
            retweet_text = strip_html(retweeted.get("text", ""))
        retweet_user = retweeted.get("user", {}).get("screen_name", "")
        text += f"\n\n🔁 转发 @{retweet_user}:\n{retweet_text}"

    # Handle images
    pics = mblog.get("pics", [])
    if pics:
        pic_urls = [p.get("large", {}).get("url", p.get("url", "")) for p in pics]
        text += "\n\n📷 图片:\n" + "\n".join(pic_urls)

    post["description"] = text
    # Update title from potentially longer text
    post["title"] = text[:50] + ("..." if len(text) > 50 else "")

    return post


def enrich_all_posts(posts, session):
    """Fetch full text for all long posts and finalize content."""
    long_count = sum(1 for p in posts if p.get("_is_long"))
    if long_count > 0:
        print(f"  Fetching full text for {long_count} long post(s)...")
        for post in posts:
            enrich_post_full_text(post, session)
            finalize_post_content(post)
    else:
        for post in posts:
            finalize_post_content(post)


def process_blogger(blogger, session):
    blogger_dir = blogger["dir"]
    os.makedirs(blogger_dir, exist_ok=True)

    print(f"  Fetching posts for UID: {blogger['uid']}")
    posts = fetch_weibo_posts(blogger["uid"], session)

    if posts is None:
        print(f"  Failed to fetch posts for {blogger['name']}")
        return False

    if not posts:
        print(f"  No posts returned for {blogger['name']}")
        return False

    print(f"  Total posts fetched: {len(posts)}")

    # Enrich with full text for long posts
    enrich_all_posts(posts, session)

    existing_links = get_existing_links(blogger_dir)
    print(f"  Existing links recorded: {len(existing_links)}")

    md_files = [f for f in os.listdir(blogger_dir) if f.endswith(".md")]
    is_first_run = len(md_files) == 0

    if is_first_run:
        print(f"  First run: saving only the latest 1 post")
        posts_to_save = [posts[0]] if posts else []
    else:
        posts_to_save = [p for p in posts if p["link"] not in existing_links]
        print(f"  New posts found: {len(posts_to_save)}")

    if not posts_to_save:
        print(f"  No new posts - skipping")
        return False

    now_dt = datetime.now()
    now_str = now_dt.strftime("%Y-%m-%d_%H-%M")
    now_full = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    md_content = f"# 微博博主「{blogger['name']}」发文记录\n\n"
    md_content += f"| 项目 | 内容 |\n|------|------|\n"
    md_content += f"| 博主名称 | {blogger['name']} |\n"
    md_content += f"| 微博主页 | {blogger['url']} |\n"
    md_content += f"| 抓取时间 | {now_full} |\n"
    md_content += f"| 本次抓取条数 | {len(posts_to_save)} |\n"
    md_content += f"| 数据来源 | weibo.com AJAX API (全文抓取) |\n\n---\n\n"

    for i, post in enumerate(posts_to_save, 1):
        title = post["title"] or "无标题"
        md_content += f"### {i}. {title}\n\n"
        md_content += f"**链接**: {post['link']}\n\n"
        if post["published"]:
            md_content += f"**发布时间**: {post['published']}\n\n"
        desc = post["description"]
        md_content += f"**正文 (全文)**:\n\n{desc}\n\n---\n\n"

    md_content += f"\n> 数据来源: weibo.com AJAX API (全文抓取)\n"

    filename = f"{now_str}.md"
    filepath = os.path.join(blogger_dir, filename)
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(md_content)
        print(f"  Saved {len(posts_to_save)} posts to {filepath}")
        return True
    except Exception as e:
        print(f"  Error saving: {e}")
        return False


def main():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{'=' * 60}")
    print(f"  Weibo Monitor - Started at {now_str}")
    if WEIBO_COOKIE:
        print(f"  Cookie: configured ({len(WEIBO_COOKIE)} chars)")
        has_sub = "SUB=" in WEIBO_COOKIE
        print(f"  Has SUB: {has_sub}")
        has_xsrf = "XSRF-TOKEN=" in WEIBO_COOKIE
        print(f"  Has XSRF-TOKEN: {has_xsrf}")
    else:
        print("  ⚠ No WEIBO_COOKIE set!")
    print(f"{'=' * 60}")

    print("\n  Building weibo.com session...")
    session = make_session()
    if session is None:
        print("  No cookie, cannot login")
        return

    any_saved = False
    for blogger in BLOGGERS:
        print(f"\nProcessing: {blogger['name']} (UID: {blogger['uid']})")
        print(f"{'- ' * 30}")
        if process_blogger(blogger, session):
            any_saved = True

    print(f"\n{'=' * 60}")
    print(f"  Finished at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if any_saved:
        print("  New posts were saved!")
    else:
        print("  No new posts found.")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
