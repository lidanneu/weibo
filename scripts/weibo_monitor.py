#!/usr/bin/env python3
"""
Weibo Monitor - Fetches latest posts from Weibo bloggers via m.weibo.cn API.
Uses SSO cross-domain login to convert weibo.com cookies to m.weibo.cn cookies.

Requires WEIBO_COOKIE environment variable (GitHub Actions secret).
Get the cookie from weibo.com (desktop site) Chrome DevTools.

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


def do_sso_login():
    """Perform SSO cross-domain login. Returns a requests.Session with m.weibo.cn cookies.

    The SSO flow:
    1. Visit m.weibo.cn → get redirected to passport.weibo.com/sso/signin
    2. Visit the SSO URL with weibo.com SUB cookie → get redirected back to m.weibo.cn
    3. m.weibo.cn sets its own cookies (SUB, _T_WM, etc.)
    4. Now the session has valid m.weibo.cn cookies
    """
    if not WEIBO_COOKIE:
        return None

    session = requests.Session()

    # Use mobile UA for m.weibo.cn
    session.headers.update({
        "User-Agent": MOBILE_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": WEIBO_COOKIE,  # Send weibo.com cookie to all requests
    })

    # Step 1: Visit m.weibo.cn to trigger SSO
    try:
        resp = session.get("https://m.weibo.cn/", timeout=15, allow_redirects=False)
        print(f"  SSO step 1: m.weibo.cn/ → HTTP {resp.status_code}")

        # If redirected, follow the chain
        if resp.status_code in (301, 302):
            redirect_url = resp.headers.get("Location", "")
            if redirect_url:
                if not redirect_url.startswith("http"):
                    redirect_url = "https://m.weibo.cn" + redirect_url
                print(f"  SSO step 2: following redirect to {redirect_url[:80]}...")

                # Follow SSO redirect (passport.weibo.com will set m.weibo.cn cookies)
                resp2 = session.get(redirect_url, timeout=15, allow_redirects=False)
                print(f"  SSO step 2: → HTTP {resp2.status_code}")

                # Follow redirect back to m.weibo.cn
                if resp2.status_code in (301, 302):
                    redirect2 = resp2.headers.get("Location", "")
                    if redirect2:
                        if not redirect2.startswith("http"):
                            redirect2 = "https://m.weibo.cn" + redirect2
                        print(f"  SSO step 3: following redirect to {redirect2[:80]}...")
                        resp3 = session.get(redirect2, timeout=15, allow_redirects=True)
                        print(f"  SSO step 3: → HTTP {resp3.status_code}")
                elif resp2.status_code == 200:
                    # SSO might return HTML with JavaScript redirect
                    body = resp2.text[:1000]
                    if "location.replace" in body or "window.location" in body:
                        # Extract redirect URL from JavaScript
                        match = re.search(r'(?:location\.replace|window\.location(?:\.href)?)\s*[=(]\s*["\']([^"\']+)', body)
                        if match:
                            js_url = match.group(1)
                            if not js_url.startswith("http"):
                                js_url = "https://m.weibo.cn" + js_url
                            print(f"  SSO step 3 (JS): following to {js_url[:80]}...")
                            session.get(js_url, timeout=15, allow_redirects=True)
    except Exception as e:
        print(f"  SSO step 1 error: {e}")

    # Step 2: Try API call to check if we got m.weibo.cn cookies
    try:
        check = session.get("https://m.weibo.cn/api/config", timeout=15)
        if check.status_code == 200:
            data = check.json()
            login_status = data.get("data", {}).get("login", False)
            print(f"  SSO login check: login={login_status}")
            if login_status:
                print("  SSO login SUCCESS!")
                return session
    except Exception as e:
        print(f"  SSO check error: {e}")

    # Step 3: Try the API directly - sometimes the SSO completes even if the check fails
    print("  SSO check failed, trying API directly...")
    return session


def fetch_weibo_posts(uid, session):
    """Fetch posts from m.weibo.cn API using an authenticated session."""
    url = "https://m.weibo.cn/api/container/getIndex"

    for attempt in range(3):
        try:
            if attempt > 0:
                time.sleep(3)

            # Step 1: Get containerid
            params1 = {"type": "uid", "value": uid}
            resp1 = session.get(url, params=params1, timeout=30)

            if resp1.status_code != 200:
                print(f"  ⚠ HTTP {resp1.status_code}")
                continue

            data1 = resp1.json()

            if data1.get("ok") != 1:
                msg = data1.get("msg", "unknown")
                sso_url = data1.get("url", "")
                print(f"  ⚠ API error (attempt {attempt+1}/3): {msg}")
                if sso_url:
                    print(f"  SSO redirect, following...")
                    # Follow SSO with Cookie header
                    try:
                        r = session.get(sso_url, timeout=15, allow_redirects=True)
                        # Retry API
                        resp1 = session.get(url, params=params1, timeout=30)
                        data1 = resp1.json()
                        if data1.get("ok") != 1:
                            if attempt == 2:
                                print(f"  SSO failed: {data1.get('msg', 'unknown')}")
                            continue
                    except Exception as e:
                        print(f"  SSO error: {e}")
                        continue

            # Extract containerid
            tabs_info = data1.get("data", {}).get("tabsInfo", {})
            tabs = tabs_info.get("tabs", [])
            container_id = None
            for tab in tabs:
                if tab.get("tab_type") == "weibo":
                    container_id = tab.get("containerid")
                    break

            if not container_id:
                # Try to get posts directly from profile response
                posts = extract_posts_from_cards(data1, uid)
                if posts:
                    print(f"  ✓ Got {len(posts)} posts (profile)")
                    return posts
                print(f"  ⚠ No containerid")
                continue

            # Step 2: Fetch posts
            params2 = {"type": "uid", "value": uid, "containerid": container_id}
            resp2 = session.get(url, params=params2, timeout=30)

            if resp2.status_code != 200:
                continue

            data2 = resp2.json()
            if data2.get("ok") != 1:
                continue

            posts = extract_posts_from_cards(data2, uid)
            if posts:
                print(f"  ✓ Got {len(posts)} posts")
                return posts

            # Try page 2
            params3 = {"type": "uid", "value": uid, "containerid": container_id, "page": 2}
            resp3 = session.get(url, params=params3, timeout=30)
            if resp3.status_code == 200:
                data3 = resp3.json()
                posts3 = extract_posts_from_cards(data3, uid)
                if posts3:
                    print(f"  ✓ Got {len(posts3)} posts (page 2)")
                    return posts3

        except Exception as e:
            print(f"  ⚠ Error: {type(e).__name__}: {e}")

    return None


def extract_posts_from_cards(data, uid):
    posts = []
    cards = data.get("data", {}).get("cards", [])
    for card in cards:
        if card.get("card_type") == 9:
            mblog = card.get("mblog", {})
            if mblog:
                post = parse_mblog(mblog, uid)
                if post:
                    posts.append(post)
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
    try:
        id_str = mblog.get("id", "")
        bid = mblog.get("bid", "")
        if id_str:
            link = f"https://weibo.com/{uid}/{id_str}"
        elif bid:
            link = f"https://weibo.com/{uid}/{bid}"
        else:
            return None

        raw_text = mblog.get("text", "")
        text = strip_html(raw_text)

        if mblog.get("isLongText"):
            long_text = mblog.get("longText", {}).get("longTextContent", "")
            if long_text:
                text = strip_html(long_text)

        retweeted = mblog.get("retweeted_status", {})
        if retweeted:
            retweet_text = strip_html(retweeted.get("text", ""))
            retweet_user = retweeted.get("user", {}).get("screen_name", "")
            text += f"\n\n🔁 转发 @{retweet_user}:\n{retweet_text}"

        pics = mblog.get("pics", [])
        if pics:
            pic_urls = [p.get("large", {}).get("url", p.get("url", "")) for p in pics]
            text += "\n\n📷 图片:\n" + "\n".join(pic_urls)

        created_at = mblog.get("created_at", "")
        title = text[:50] + ("..." if len(text) > 50 else "")

        return {"title": title, "link": link, "description": text, "published": created_at}
    except Exception as e:
        print(f"  ⚠ Parse error: {e}")
        return None


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
    md_content += f"| 数据来源 | m.weibo.cn API (SSO) |\n\n---\n\n"

    for i, post in enumerate(posts_to_save, 1):
        title = post["title"] or "无标题"
        md_content += f"### {i}. {title}\n\n"
        md_content += f"**链接**: {post['link']}\n\n"
        if post["published"]:
            md_content += f"**发布时间**: {post['published']}\n\n"
        md_content += f"**正文**:\n\n{post['description']}\n\n---\n\n"

    md_content += f"\n> 数据来源: m.weibo.cn API (SSO)\n"

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
    else:
        print("  ⚠ No WEIBO_COOKIE set!")
    print(f"{'=' * 60}")

    # Perform SSO login once, reuse session for all bloggers
    print("\n  Performing SSO cross-domain login...")
    session = do_sso_login()
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
