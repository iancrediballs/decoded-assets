#!/usr/bin/env python3
"""
DECODED auto-publisher.

Runs on a GitHub Actions cron. For each post in schedule.json:

  Instagram - has no scheduling API and its media containers expire after 24h,
              so the carousel is built and published at the moment the slot
              arrives. That is what this cron is for.
  Facebook  - DOES schedule natively, so its post is handed to Meta once, well
              ahead of time, and Meta holds it. No cron needed after that.

State is written to state.json and committed back by the workflow, so a post
can never go out twice.

Required environment:
  META_TOKEN       long-lived Page access token
  ASSET_BASE_URL   public base for slides, no trailing slash

The Page id and the linked Instagram account id are NOT configured. A Page
access token already knows which Page it belongs to, so both are resolved from
`me` at run time. Supplying them by hand was a standing trap: a transposed or
stale id fails as a generic code-100 "Object with ID does not exist", which
looks like a permissions problem and is not.
Optional:
  GRAPH_VERSION    default v23.0 - bump if Meta deprecates it
  DRY_RUN          "1" to log intentions without calling Meta
  WINDOW_MIN       how late a slot may be and still fire (default 90 min)
"""

import json, os, sys, time, urllib.parse, urllib.request, urllib.error
import concurrent.futures as cf
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEDULE = os.path.join(HERE, "schedule.json")
STATE = os.path.join(HERE, "state.json")

GV = os.environ.get("GRAPH_VERSION", "v23.0")
GRAPH = f"https://graph.facebook.com/{GV}"
TOKEN = os.environ.get("META_TOKEN", "")
BASE = os.environ.get("ASSET_BASE_URL", "").rstrip("/")
DRY = os.environ.get("DRY_RUN") == "1"
WINDOW = int(os.environ.get("WINDOW_MIN", "90"))

# Meta refuses a scheduled_publish_time less than 10 minutes out.
FB_MIN_LEAD = 15 * 60


def log(*a):
    print(*a, flush=True)


def api(path, params=None, method="POST"):
    """Call the Graph API. Raises RuntimeError with Meta's own message on failure."""
    params = dict(params or {})
    params["access_token"] = TOKEN
    url = f"{GRAPH}/{path.lstrip('/')}"
    data = urllib.parse.urlencode(params).encode()

    if method == "GET":
        url = f"{url}?{data.decode()}"
        req = urllib.request.Request(url, method="GET")
    else:
        req = urllib.request.Request(url, data=data, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            err = json.loads(body)["error"]
            msg = f"{err.get('type')}: {err.get('message')} (code {err.get('code')})"
        except Exception:
            msg = body[:400]
        raise RuntimeError(f"Graph {method} {path} -> HTTP {e.code}: {msg}") from None


_IDS = {}


def ids():
    """Resolve the Page and Instagram ids from the token itself, once per run.

    With a Page access token `me` IS the Page, so this cannot disagree with the
    token the way a hand-entered id can. Any mismatch between the two was
    previously invisible until Meta returned a code-100 that named neither.
    """
    if _IDS:
        return _IDS

    me = api("me", {"fields": "id,name"}, method="GET")
    fb = me["id"]
    log(f"  page: {me.get('name')} ({fb})")

    r = api(fb, {"fields": "instagram_business_account"}, method="GET")
    ig = (r.get("instagram_business_account") or {}).get("id", "")
    log(f"  instagram: {ig}" if ig else
        "  WARNING: no Instagram account linked to this Page - IG posts will fail")

    # A stale secret left over from the old configured-id design is worth
    # naming rather than silently ignoring.
    for var, got in (("FB_PAGE_ID", fb), ("IG_USER_ID", ig)):
        env = os.environ.get(var, "")
        if env and env != got:
            log(f"  note: {var} is set to {env} but the token resolves to {got}"
                f" - using the token's value and ignoring the secret")

    _IDS.update(fb=fb, ig=ig)
    return _IDS


def load(p, default):
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save(p, obj):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def slide_urls(post):
    return [f"{BASE}/{post['slides_dir']}/{n}" for n in post["slides"]]


def check_urls(urls):
    """Confirm every slide is publicly fetchable before Meta is asked to fetch it.

    Meta reports a bad image URL as a generic container ERROR with no detail,
    so catching it here is the difference between a clear message and an
    afternoon of guessing. Returns a list of problems, empty if all good.
    """
    def one(u):
        try:
            req = urllib.request.Request(u, method="HEAD")
            with urllib.request.urlopen(req, timeout=30) as r:
                ctype = r.headers.get("Content-Type", "")
                if not ctype.startswith("image/"):
                    return f"{u} -> not an image ({ctype or 'no content-type'})"
        except urllib.error.HTTPError as e:
            return f"{u} -> HTTP {e.code}"
        except Exception as e:
            return f"{u} -> {type(e).__name__}: {e}"
        return None

    # CDN round-trips are ~2s each; serial checking of a full run blows past
    # any sensible job timeout, so fan them out.
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        return [r for r in pool.map(one, urls) if r]


def full_caption(post, platform):
    body = post.get(f"caption_{platform}") or post["caption"]
    tags = post.get("hashtags", "")
    return f"{body}\n\n{tags}".strip() if tags else body


# --------------------------------------------------------------------------
# Instagram - build containers, assemble carousel, publish
# --------------------------------------------------------------------------
def wait_ready(container_id, tries=30, delay=5):
    """Poll a container until Meta finishes ingesting the image."""
    for i in range(tries):
        r = api(container_id, {"fields": "status_code,status"}, method="GET")
        sc = r.get("status_code")
        if sc == "FINISHED":
            return
        if sc == "ERROR":
            raise RuntimeError(f"container {container_id} ERROR: {r.get('status')}")
        time.sleep(delay)
    raise RuntimeError(f"container {container_id} not ready after {tries * delay}s")


def publish_instagram(post):
    urls = slide_urls(post)
    if not 2 <= len(urls) <= 10:
        raise RuntimeError(f"carousel needs 2-10 slides, got {len(urls)}")

    if DRY:
        bad = check_urls(urls)
        if bad:
            raise RuntimeError("slides unreachable:\n      " + "\n      ".join(bad))
        log(f"    DRY: {len(urls)} slides reachable, would publish to IG")
        return "dry-run-ig"

    # Pre-flight. Cheaper to fail here than halfway through building a carousel.
    bad = check_urls(urls[:1])
    if bad:
        raise RuntimeError("first slide unreachable: " + bad[0])

    ig_id = ids()["ig"]
    if not ig_id:
        raise RuntimeError("no Instagram Business account linked to this Page")

    children = []
    for i, u in enumerate(urls, 1):
        r = api(f"{ig_id}/media", {"image_url": u, "is_carousel_item": "true"})
        children.append(r["id"])
        log(f"    container {i}/{len(urls)} -> {r['id']}")

    for c in children:
        wait_ready(c)

    parent = api(f"{ig_id}/media", {
        "media_type": "CAROUSEL",
        "children": ",".join(children),
        "caption": full_caption(post, "ig"),
    })["id"]
    wait_ready(parent)

    return api(f"{ig_id}/media_publish", {"creation_id": parent})["id"]


# --------------------------------------------------------------------------
# Facebook - upload unpublished photos, attach to a natively scheduled post
# --------------------------------------------------------------------------
def schedule_facebook(post, when_ts):
    urls = slide_urls(post)

    if DRY:
        bad = check_urls(urls)
        if bad:
            raise RuntimeError("slides unreachable:\n      " + "\n      ".join(bad))
        log(f"    DRY: {len(urls)} slides reachable, would schedule to FB")
        return "dry-run-fb"

    bad = check_urls(urls[:1])
    if bad:
        raise RuntimeError("first slide unreachable: " + bad[0])

    fb_id = ids()["fb"]

    fbids = []
    for i, u in enumerate(urls, 1):
        r = api(f"{fb_id}/photos", {"url": u, "published": "false"})
        fbids.append(r["id"])
        log(f"    photo {i}/{len(urls)} -> {r['id']}")

    params = {
        "message": full_caption(post, "fb"),
        "published": "false",
        "scheduled_publish_time": str(int(when_ts)),
    }
    for i, fid in enumerate(fbids):
        params[f"attached_media[{i}]"] = json.dumps({"media_fbid": fid})

    return api(f"{fb_id}/feed", params)["id"]


# --------------------------------------------------------------------------
def main():
    missing = [k for k, v in {
        "META_TOKEN": TOKEN, "ASSET_BASE_URL": BASE,
    }.items() if not v]
    if missing and not DRY:
        log("FATAL: missing env: " + ", ".join(missing))
        return 1

    posts = load(SCHEDULE, {"posts": []})["posts"]
    state = load(STATE, {})
    now = datetime.now(timezone.utc)
    changed = False
    failures = []

    log(f"Run at {now.isoformat()}  |  {len(posts)} posts  |  dry_run={DRY}")

    for post in posts:
        pid = post["id"]
        if post.get("hold"):
            continue

        st = state.setdefault(pid, {})
        slot = datetime.fromisoformat(post["slot_utc"].replace("Z", "+00:00"))
        age_min = (now - slot).total_seconds() / 60

        # ---- Facebook: hand to Meta's own scheduler, once, ahead of time ----
        if "facebook" in post["platforms"] and not st.get("fb_id"):
            lead = (slot - now).total_seconds()
            if lead > FB_MIN_LEAD:
                log(f"  {pid}: scheduling Facebook for {post['slot_utc']}")
                try:
                    st["fb_id"] = schedule_facebook(post, slot.timestamp())
                    st["fb_at"] = now.isoformat()
                    changed = True
                    log(f"    FB scheduled -> {st['fb_id']}")
                except Exception as e:
                    failures.append(f"{pid} FB: {e}")
                    log(f"    FB FAILED: {e}")

        # ---- Instagram: publish at the moment ----
        if "instagram" in post["platforms"] and not st.get("ig_id"):
            if 0 <= age_min <= WINDOW:
                log(f"  {pid}: publishing to Instagram (slot {post['slot_utc']})")
                try:
                    st["ig_id"] = publish_instagram(post)
                    st["ig_at"] = now.isoformat()
                    changed = True
                    log(f"    IG published -> {st['ig_id']}")
                except Exception as e:
                    failures.append(f"{pid} IG: {e}")
                    log(f"    IG FAILED: {e}")
            elif age_min > WINDOW:
                if not st.get("ig_missed"):
                    st["ig_missed"] = True
                    changed = True
                    failures.append(f"{pid} IG: slot missed by {int(age_min)} min")
                    log(f"  {pid}: MISSED by {int(age_min)} min - will not auto-post")

    if changed and not DRY:
        save(STATE, state)
        log("state.json updated")
    elif changed:
        log("DRY: state.json left untouched")

    if failures:
        log("\nFAILURES:")
        for f in failures:
            log("  - " + f)
        return 1

    log("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
