#!/usr/bin/env python3
"""
Weibo Monitor - Fetches latest posts from Weibo bloggers via weibo.com Ajax API.
Requires WEIBO_COOKIE environment variable (GitHub Actions secret).
Get the cookie from weibo.com (desktop site) Chrome DevTools.

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

# Desktop browser headers for weibo.com
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://weibo.com/",
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


def get_xsrf_token():
    """Extract XSRF-TOKEN from cookie string."""
    if not WEIBO_COOKIE:
        return None
    for part in WEIBO_COOKIE.split(";"):
        part = part.strip()
        if part.startswith("XSRF-TOKEN="):
            return part.split("=", 1)[1]
    return None


def create_session():
    """Create a requests session with cookie and XSRF token set."""
    session = requests.Session()
    session.headers.update(HEADERS)

    if WEIBO_COOKIE:
        # Set cookie as header
        session.headers["Cookie"] = WEIBO_COOKIE

        # Parse into cookie jar
        for cookie_pair in WEIBO_COOKIE.split(";"):
            cookie_pair = cookie_pair.strip()
            if "=" in cookie_pair:
                name, value = cookie_pair.split("=", 1)
                session.cookies.set(name.strip(), value.strip(), domain=".weibo.com")
                session.cookies.set(name.strip(), value.strip(), domain=".sina.com.cn")

        # Set XSRF-TOKEN header (required by weibo.com API)
        xsrf = get_xsrf_token()
        if xsrf:
            session.headers["X-XSRF-TOKEN"] = xsrf
            print(f"  XSRF-TOKEN: found ({len(xsrf)} chars)")

    return session


def fetch_weibo_posts(uid):
    """Fetch latest posts from weibo.com Ajax API.

    Returns a list of dicts with keys: title, link, description, published.
    Returns None on failure.
    """
    session = create_session()

    # Visit the user's profile page first (needed to get proper tokens)
    try:
        profile_url = f"https://weibo.com/u/{uid}"
        session.get(profile_url, timeout=15, allow_redirects=True)
    except Exception:
        pass

    # Try multiple API endpoints
    endpoints = [
        # Format: (url, params_dict, description)
        ("https://weibo.com/ajax/statuses/profileProfilePage",
         {"uid": uid, "page": 1, "feature": 0},
         "profileProfilePage"),
        ("https://weibo.com/ajax/profile/mystimeline",
         {"uid": uid, "page": 1, "feature": 0},
         "mystimeline"),
        ("https://weibo.com/ajax/profile/getcontent",
         {"uid": uid, "page": 1},
         "getcontent"),
    ]

    for api_url, params, desc in endpoints:
        print(f"  Trying {desc}...")
        for attempt in range(2):
            try:
                if attempt > 0:
                    time.sleep(2)

                resp = session.get(api_url, params=params, timeout=30)

                if resp.status_code != 200:
                    print(f"  ⚠ HTTP {resp.status_code}")
                    continue

                data = resp.json()

                # Check for error response
                if isinstance(data, dict) and (data.get("ok") == 0 or "error" in str(data.get("error", ""))):
                    msg = data.get("message", "") or data.get("msg", "")
                    print(f"  ⚠ API error: {msg}")
                    continue

                # Try to extract posts
                posts = extract_posts_from_weibo_com(data, uid)

                if posts:
                    print(f"  ✓ Got {len(posts)} posts (via {desc})")
                    return posts
                else:
                    print(f"  ⚠ No posts found (via {desc})")
                    if attempt == 1:
                        # Log response structure for debugging
                        if isinstance(data, dict):
                            print(f"  Response keys: {list(data.keys())}")
                            if "data" in data and isinstance(data["data"], dict):
                                print(f"  data keys: {list(data['data'].keys())}")

            except requests.exceptions.RequestException as e:
                print(f"  ⚠ Request failed: {type(e).__name__}")
            except json.JSONDecodeError:
                print(f"  ⚠ Not JSON response")
            except Exception as e:
                print(f"  ⚠ Error: {type(e).__name__}: {e}")

    # Fallback: try m.weibo.cn API with SSO
    print("  Trying m.weibo.cn API as fallback...")
    return fetch_weibo_posts_m(uid, session)


def fetch_weibo_posts_m(uid, session):
    """Fallback: try m.weibo.cn API with SSO cross-domain login."""
    url = "https://m.weibo.cn/api/container/getIndex"
    m_headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        "Referer": "https://m.weibo.cn/",
        "MWeibo-Pwa": "1",
        "X-Requested-With": "XMLHttpRequest",
    }

    for attempt in range(2):
        try:
            params = {"type": "uid", "value": uid}
            resp = session.get(url, params=params, headers=m_headers, timeout=30)

            if resp.status_code != 200:
                print(f"  ⚠ m.weibo.cn HTTP {resp.status_code}")
                continue

            data = resp.json()

            if data.get("ok") != 1:
                # Try SSO redirect
                sso_url = data.get("url", "")
                if sso_url:
                    print(f"  SSO redirect, following...")
                    try:
                        # Follow SSO redirect chain
                        current_url = sso_url
                        for _ in range(5):
                            if not current_url:
                                break
                            r = session.get(current_url, headers=m_headers, timeout=15, allow_redirects=False)
                            if r.status_code in (301, 302):
                                current_url = r.headers.get("Location", "")
                                if current_url and not current_url.startswith("http"):
                                    current_url = "https://m.weibo.cn" + current_url
                            else:
                                # Check if response has callback for setting cookies
                                break
                        # Retry API call
                        resp = session.get(url, params=params, headers=m_headers, timeout=30)
                        data = resp.json()
                        if data.get("ok") == 1:
                            pass  # Success, continue
                        else:
                            print(f"  ⚠ SSO failed: {data.get('msg', 'unknown')}")
                            continue
                    except Exception as e:
                        print(f"  ⚠ SSO error: {e}")
                        continue
                else:
                    print(f"  ⚠ m.weibo.cn error: {data.get('msg', 'unknown')}")
                    continue

            # Extract containerid and fetch posts
            tabs_info = data.get("data", {}).get("tabsInfo", {})
            tabs = tabs_info.get("tabs", [])
            container_id = None
            for tab in tabs:
                if tab.get("tab_type") == "weibo":
                    container_id = tab.get("containerid")
                    break

            if not container_id:
                posts = extract_posts_from_cards(data, uid)
                if posts:
                    print(f"  ✓ Got {len(posts)} posts (m.weibo.cn profile)")
                    return posts
                continue

            params2 = {"type": "uid", "value": uid, "containerid": container_id}
            resp2 = session.get(url, params=params2, headers=m_headers, timeout=30)
            if resp2.status_code == 200:
                data2 = resp2.json()
                if data2.get("ok") == 1:
                    posts = extract_posts_from_cards(data2, uid)
                    if posts:
                        print(f"  ✓ Got {len(posts)} posts (m.weibo.cn)")
                        return posts

        except Exception as e:
            print(f"  ⚠ m.weibo.cn fallback error: {e}")

    return None


def extract_posts_from_weibo_com(data, uid):
    """Extract posts from weibo.com Ajax API response."""
    posts = []

    # Try multiple response structures
    list_data = data.get("data", {}).get("list", [])
    if not list_data:
        list_data = data.get("data", {}).get("statuses", [])
    if not list_data:
        list_data = data.get("statuses", [])
    if not list_data:
        list_data = data.get("data", {}).get("data", {}).get("list", [])
    if not list_data:
        # Maybe the data IS the list
        if isinstance(data, list):
            list_data = data
        elif isinstance(data.get("data"), list):
            list_data = data["data"]

    for item in list_data:
        post = parse_weibo_com_post(item, uid)
        if post:
            posts.append(post)

    return posts


def parse_weibo_com_post(item, uid):
    """Parse a weibo.com Ajax API post item."""
    try:
        id_str = str(item.get("id", ""))
        bid = item.get("bid", "")
        mid = item.get("mid", "")

        if id_str:
            link = f"https://weibo.com/{uid}/{id_str}"
        elif bid:
            link = f"https://weibo.com/{uid}/{bid}"
        elif mid:
            link = f"https://weibo.com/{uid}/{mid}"
        else:
            return None

        # Get text content
        raw_text = item.get("text_raw", "") or item.get("text", "")
        text = strip_html(raw_text) if raw_text else ""

        # Get long text if available
        if item.get("isLongText"):
            long_text_content = ""
            if isinstance(item.get("longText"), dict):
                long_text_content = item["longText"].get("longTextContent", "")
            elif isinstance(item.get("longTextContent"), str):
                long_text_content = item["longTextContent"]
            if long_text_content:
                text = strip_html(long_text_content)

        # Get original post if retweet
        retweeted = item.get("retweeted_status", {})
        if retweeted:
            retweet_text = strip_html(retweeted.get("text_raw", "") or retweeted.get("text", ""))
            retweet_user = retweeted.get("user", {}).get("screen_name", "")
            text += f"\n\n🔁 转发 @{retweet_user}:\n{retweet_text}"

        # Get images
        pics = item.get("pic_ids", [])
        if pics:
            text += "\n\n📷 图片:\n" + "\n".join(f"https://wx1.sinaimg.cn/large/{p}" for p in pics)
        elif item.get("pics"):
            pic_urls = [p.get("url", p.get("large", {}).get("url", "")) for p in item["pics"]]
            text += "\n\n📷 图片:\n" + "\n".join(pic_urls)

        # Get created_at
        created_at = item.get("created_at", "")
        title = text[:50] + ("..." if len(text) > 50 else "")

        return {
            "title": title,
            "link": link,
            "description": text,
            "published": created_at,
        }
    except Exception as e:
        print(f"  ⚠ Error parsing post: {e}")
        return None


def extract_posts_from_cards(data, uid):
    """Extract posts from m.weibo.cn API response cards."""
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
    """Parse an m.weibo.cn mblog object into a post dict."""
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

        retweeted_status = mblog.get("retweeted_status", {})
        if retweeted_status:
            retweet_text = strip_html(retweeted_status.get("text", ""))
            retweet_user = retweeted_status.get("user", {}).get("screen_name", "")
            text += f"\n\n🔁 转发 @{retweet_user}:\n{retweet_text}"

        pics = mblog.get("pics", [])
        if pics:
            pic_urls = [p.get("large", {}).get("url", p.get("url", "")) for p in pics]
            text += "\n\n📷 图片:\n" + "\n".join(pic_urls)

        created_at = mblog.get("created_at", "")
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

    existing_links = get_existing_links(blogger_dir)
    print(f"  Existing links recorded: {len(existing_links)}")

    md_files = [f for f in os.listdir(blogger_dir) if f.endswith(".md")]
    is_first_run = len(md_files) == 0

    if is_first_run:
        print(f"  First run: saving only the latest 1 post")
        posts_to_save = [posts[0]] if posts else []
    else:
        posts_to_save = []
        for post in posts:
            if post["link"] not in existing_links:
                posts_to_save.append(post)
        print(f"  New posts found: {len(posts_to_save)}")

    if not posts_to_save:
        print(f"  No new posts for {blogger['name']} - skipping")
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
    md_content += f"| 数据来源 | weibo.com Ajax API |\n\n---\n\n"

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

    md_content += f"\n> 数据来源: weibo.com Ajax API\n"

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
        has_sub = "SUB=" in WEIBO_COOKIE or "SUBP=" in WEIBO_COOKIE
        print(f"  Cookie has SUB/SUBP: {has_sub}")
        has_xsrf = "XSRF-TOKEN=" in WEIBO_COOKIE
        print(f"  Cookie has XSRF-TOKEN: {has_xsrf}")
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
