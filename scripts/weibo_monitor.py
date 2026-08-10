#!/usr/bin/env python3
"""
Weibo Monitor - Fetches latest posts from Weibo bloggers via m.weibo.cn API.
Uses SSO cross-domain login to convert weibo.com cookies to m.weibo.cn cookies.

CRITICAL: For long posts, the list API returns truncated text (~140 chars).
We use the /statuses/extend API to fetch the FULL text for EVERY post,
ensuring the complete content is preserved even if the blogger deletes it later.

Requires WEIBO_COOKIE environment variable (GitHub Actions secret).
Get the cookie from m.weibo.cn (mobile Weibo) Chrome DevTools.

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
        "Referer": "https://m.weibo.cn/",
        "Origin": "https://m.weibo.cn",
        "X-Requested-With": "XMLHttpRequest",
    })

    # m.weibo.cn does NOT issue its own SUB auth cookie — it relies on the
    # weibo.com SUB cookie being passed in the request. So we inject the
    # weibo.com cookie into the jar for the .weibo.cn domain ONLY (so it is
    # sent to m.weibo.cn for both the SSO handshake and the getIndex call).
    # Injecting it for .weibo.com too would create duplicate cookies of the same
    # name, which makes the XSRF-TOKEN lookup ambiguous and can get the request
    # rejected with 403.
    for item in WEIBO_COOKIE.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            try:
                session.cookies.set(k.strip(), v.strip(), domain=".weibo.cn")
            except Exception:
                pass

    # Step 1: Visit m.weibo.cn to trigger SSO
    try:
        resp = session.get("https://m.weibo.cn/", timeout=15, allow_redirects=False)
        print(f"  SSO step 1: m.weibo.cn/ → HTTP {resp.status_code}")
        set_cookies = resp.headers.get("Set-Cookie", "")
        print(f"  SSO step 1 Set-Cookie: {set_cookies[:200]}")
        print(f"  SSO step 1 jar after: {sorted(session.cookies.keys())}")

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
                print(f"  SSO login SUCCESS! jar cookies: {sorted(session.cookies.keys())}")
                _diagnose_cookie_expiry(session)
                _attach_xsrf(session)
                return session
    except Exception as e:
        print(f"  SSO check error: {e}")

    # Step 3: Try the API directly - sometimes the SSO completes even if the check fails
    print("  SSO check failed, trying API directly...")
    _attach_xsrf(session)
    return session


def _diagnose_cookie_expiry(session):
    """Decode the ALF cookie (weibo login expiry, a Unix timestamp) to check
    whether the cookie has expired — a common cause of getIndex 403 while
    config still reports login=True."""
    try:
        import time as _time
        alf = None
        for c in session.cookies:
            if c.name == "ALF" and c.domain.endswith("weibo.cn"):
                alf = c.value
                break
        if not alf:
            print("  ⚠ No ALF cookie found (cannot check expiry)")
            return
        try:
            exp = int(alf)
            now = int(_time.time())
            delta = exp - now
            state = "EXPIRED" if delta <= 0 else f"valid ({delta}s left)"
            print(f"  ALF expiry={exp} now={now} → {state}")
        except ValueError:
            print(f"  ALF value not a timestamp: {alf[:20]}")
    except Exception as e:
        print(f"  ⚠ expiry diag error: {e}")


def _attach_xsrf(session):
    """m.weibo.cn getIndex requires an X-XSRF-TOKEN header matching the
    XSRF-TOKEN cookie, otherwise it returns 403 '请求被拒绝'. The SSO response
    sets XSRF-TOKEN twice (first = 'deleted', then the real one), so we must
    take the LAST non-deleted value."""
    try:
        xsrf = None
        for c in session.cookies:
            if c.name == "XSRF-TOKEN" and c.domain.endswith("weibo.cn") and c.value != "deleted":
                xsrf = c.value
        if xsrf:
            session.headers["X-XSRF-TOKEN"] = xsrf
            print(f"  Attached X-XSRF-TOKEN header (len={len(xsrf)})")
        else:
            print("  ⚠ No valid XSRF-TOKEN (weibo.cn) in jar to attach")
    except Exception as e:
        print(f"  ⚠ XSRF attach error: {e}")


def fetch_post_full_text(post_id, session, list_len=0):
    """Fetch the FULL text of a single post via m.weibo.cn statuses/extend API.

    The list API returns truncated text (~140 chars). This endpoint returns
    the complete long text content for any post that has isLongText=True.

    Returns the full text string, or None on failure.
    """
    url = f"https://m.weibo.cn/statuses/extend?id={post_id}"
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
            print(f"    ⚠ extend API returned ok but no longTextContent")
    except Exception as e:
        print(f"    ⚠ Full text fetch error for {post_id}: {e}")
    return None


def _probe_clean_cookie(uid, url):
    """Diagnostic probe: test the m.weibo.cn HTML profile page, which is NOT
    the JSON getIndex API and may not be WAF-blocked. If it contains post data
    we can scrape it instead."""
    try:
        html_url = f"https://m.weibo.cn/u/{uid}"
        r = session.get(html_url, timeout=30) if False else None
        # Use a fresh session with the same auth approach
        p = requests.Session()
        p.headers.update({
            "User-Agent": MOBILE_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://m.weibo.cn/",
            "Cookie": WEIBO_COOKIE,
        })
        r = p.get(html_url, timeout=30)
        has_render = "$render_data" in r.text or "render_data" in r.text
        has_weibo = "weibo" in r.text.lower()
        print(f"  [PROBE html page] HTTP {r.status_code} len={len(r.text)} has_render_data={has_render} has_weibo={has_weibo}")
        # Try to find a post link in the HTML
        import re as _re
        links = _re.findall(r"/status/(\d+)", r.text)
        print(f"  [PROBE html page] status links found: {len(set(links))}")
    except Exception as e:
        print(f"  [PROBE html page] error: {e}")


def fetch_weibo_posts(uid, session):
    """Fetch posts from m.weibo.cn API using an authenticated session.
    For long posts, also fetches full text via the statuses/extend API.
    """
    url = "https://m.weibo.cn/api/container/getIndex"

    # PROBE: test getIndex with ONLY the clean weibo.com cookie (Aug 8 style),
    # excluding the SSO-set m.weibo.cn cookies, to isolate whether the 403 is
    # caused by cookie mixing or by a WAF on the endpoint itself.
    _probe_clean_cookie(uid, url)

    for attempt in range(3):
        try:
            if attempt > 0:
                time.sleep(3)

            # Step 1: Get containerid
            params1 = {"type": "uid", "value": uid}
            resp1 = session.get(url, params=params1, timeout=30)

            if resp1.status_code != 200:
                ck_names = sorted(session.cookies.keys())
                body_snip = resp1.text[:200].replace("\n", " ")
                print(f"  ⚠ HTTP {resp1.status_code} | jar cookies: {ck_names} | body: {body_snip}")
                continue

            data1 = resp1.json()

            if data1.get("ok") != 1:
                msg = data1.get("msg", "unknown")
                sso_url = data1.get("url", "")
                print(f"  ⚠ API error (attempt {attempt+1}/3): {msg}")
                if sso_url:
                    print(f"  SSO redirect, following...")
                    try:
                        r = session.get(sso_url, timeout=15, allow_redirects=True)
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
                posts = extract_posts_from_cards(data1, uid, session)
                if posts:
                    print(f"  ✓ Got {len(posts)} posts (profile)")
                    return posts
                print(f"  ⚠ No containerid")
                continue

            # Step 2: Fetch posts from the weibo tab
            params2 = {"type": "uid", "value": uid, "containerid": container_id}
            resp2 = session.get(url, params=params2, timeout=30)

            if resp2.status_code != 200:
                continue

            data2 = resp2.json()
            if data2.get("ok") != 1:
                continue

            posts = extract_posts_from_cards(data2, uid, session)
            if posts:
                print(f"  ✓ Got {len(posts)} posts")
                return posts

            # Try page 2
            params3 = {"type": "uid", "value": uid, "containerid": container_id, "page": 2}
            resp3 = session.get(url, params=params3, timeout=30)
            if resp3.status_code == 200:
                data3 = resp3.json()
                posts3 = extract_posts_from_cards(data3, uid, session)
                if posts3:
                    print(f"  ✓ Got {len(posts3)} posts (page 2)")
                    return posts3

        except Exception as e:
            print(f"  ⚠ Error: {type(e).__name__}: {e}")

    return None


def extract_posts_from_cards(data, uid, session):
    """Extract parsed posts from API response cards.
    For long posts, fetches full text from statuses/extend API.
    """
    posts = []
    cards = data.get("data", {}).get("cards", [])
    for card in cards:
        if card.get("card_type") == 9:
            mblog = card.get("mblog", {})
            if mblog:
                post = parse_mblog(mblog, uid, session)
                if post:
                    posts.append(post)
        elif card.get("card_group"):
            for sub_card in card["card_group"]:
                if sub_card.get("card_type") == 9:
                    mblog = sub_card.get("mblog", {})
                    if mblog:
                        post = parse_mblog(mblog, uid, session)
                        if post:
                            posts.append(post)
    return posts


def parse_mblog(mblog, uid, session):
    """Parse a single mblog dict into a post dict. Fetches full text for long posts."""
    try:
        id_str = mblog.get("id", "")
        bid = mblog.get("bid", "")
        if id_str:
            link = f"https://weibo.com/{uid}/{id_str}"
        elif bid:
            link = f"https://weibo.com/{uid}/{bid}"
        else:
            return None

        # Get text — always try to fetch full text for long posts
        raw_text = mblog.get("text", "")
        text = strip_html(raw_text)
        is_long = mblog.get("isLongText", False)

        # Try built-in longText field first (sometimes present in newer API responses)
        if is_long:
            long_text = mblog.get("longText", {}).get("longTextContent", "")
            if long_text:
                text = strip_html(long_text)
                is_long = False  # already got full text

        # Mark post as needing full-text fetch (we fetch AFTER all posts are parsed
        # to avoid one-by-one delays; the actual fetch happens in enrich_posts_full_text)
        post = {
            "title": text[:50] + ("..." if len(text) > 50 else ""),
            "link": link,
            "description": text,
            "published": mblog.get("created_at", ""),
            "_id": id_str,
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
    retweeted = mblog.get("retweeted_status", {})
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
    retweeted = mblog.get("retweeted_status", {})
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
    md_content += f"| 数据来源 | m.weibo.cn API (SSO + 全文抓取) |\n\n---\n\n"

    for i, post in enumerate(posts_to_save, 1):
        title = post["title"] or "无标题"
        md_content += f"### {i}. {title}\n\n"
        md_content += f"**链接**: {post['link']}\n\n"
        if post["published"]:
            md_content += f"**发布时间**: {post['published']}\n\n"
        desc = post["description"]
        md_content += f"**正文 (全文)**:\n\n{desc}\n\n---\n\n"

    md_content += f"\n> 数据来源: m.weibo.cn API (SSO + 全文抓取)\n"

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
