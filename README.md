# decoded-assets

Auto-publisher for DECODED carousels. A GitHub Actions cron publishes Instagram
carousels at their slot time and hands Facebook posts to Meta's own scheduler.

**This repo must be public.** Meta's servers download your slides by URL — they
cannot authenticate to a private repo. That is the single hard requirement, and
it is why the earlier Vercel route was stuck. Nothing sensitive lives here:
tokens go in GitHub Secrets, which stay encrypted and private even on a public repo.

---

## Setup, once

### 1. Push this folder

```bash
cd decoded-assets
git init && git branch -M main
git add . && git commit -m "Decoded auto-publisher"
gh repo create decoded-assets --public --source=. --push
```

No `gh`? Create the repo on github.com (**Public**), then:

```bash
git remote add origin https://github.com/<you>/decoded-assets.git
git push -u origin main
```

### 2. Point the publisher at your slides

Repo → **Settings → Secrets and variables → Actions → Variables → New**:

| Variable | Value |
|---|---|
| `ASSET_BASE_URL` | `https://cdn.jsdelivr.net/gh/<you>/decoded-assets@main` |
| `GRAPH_VERSION` | `v23.0` — only if you need to override the default |

jsDelivr is a CDN in front of GitHub and is the more reliable source for Meta's
image fetcher. `https://raw.githubusercontent.com/<you>/decoded-assets/main`
also works if jsDelivr gives trouble.

Check it resolves before going further — paste a slide URL in a browser:

```
https://cdn.jsdelivr.net/gh/<you>/decoded-assets@main/slides/D004/D004_01.png
```

If that 404s, nothing else will work. jsDelivr can take a few minutes to pick
up a brand-new repo.

### 3. Meta app and token

Your Instagram must be a **Professional account linked to your Facebook Page**.
Yours already is.

1. [developers.facebook.com](https://developers.facebook.com) → **My Apps → Create App** → type **Business**.
2. Leave it in **Development mode**. You are the app owner, so you get Standard
   Access to your own accounts — **no App Review, no 2–4 week wait.** Review is
   only needed to publish on behalf of *other people's* accounts.
3. **Tools → Graph API Explorer**, select your app, **Generate Access Token** with:
   `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`,
   `instagram_basic`, `instagram_content_publish`, `business_management`
4. Turn that short token into a long-lived one:
   ```
   GET /oauth/access_token
       ?grant_type=fb_exchange_token
       &client_id=APP_ID&client_secret=APP_SECRET
       &fb_exchange_token=SHORT_TOKEN
   ```
5. Get the **Page** token and Page id — a Page token derived from a long-lived
   user token does not expire unless you change your password or revoke access:
   ```
   GET /me/accounts?access_token=LONG_LIVED_USER_TOKEN
   ```
   Take `access_token` (→ `META_TOKEN`) and `id` (→ `FB_PAGE_ID`).
6. Get the Instagram id:
   ```
   GET /{FB_PAGE_ID}?fields=instagram_business_account&access_token=META_TOKEN
   ```
   → `IG_USER_ID`.

### 4. Add the secret

Repo → **Settings → Secrets and variables → Actions → Secrets**:

`META_TOKEN` — the **Page** token from step 3.5, not the user token.

That is the only secret needed. The Page id and the linked Instagram id are
resolved from the token at run time via `me`, so there is nothing to transpose.
`FB_PAGE_ID` and `IG_USER_ID` are no longer read; if they are still set and
disagree with the token, the log says so and uses the token's value.

Check the token before relying on it: paste it into the
[Access Token Debugger](https://developers.facebook.com/tools/debug/accesstoken/).
**Type** must be `Page` and **Expires** must be `Never`. A User token, or one
with an expiry date, will work for an hour or two and then fail with code 190.

### 5. Test before it can touch anything

**Actions → Publish Decoded posts → Run workflow**, leave **dry run ticked**.
Read the log. It should list every Facebook post it intends to schedule and
skip both held posts. Only when that looks right, run it again **unticked**.

From then on it runs itself every 15 minutes.

---

## How it behaves

**Instagram** has no scheduling API, and its media containers expire after 24
hours, so a carousel cannot be staged in advance. The cron builds and publishes
it at the slot. GitHub's cron is best-effort and can lag 5–20 minutes, so a slot
stays eligible for 90 minutes (`WINDOW_MIN`). Past that it is marked missed and
skipped rather than posted embarrassingly late.

**Facebook** schedules natively. Its post is handed to Meta once, as soon as the
cron sees it more than 15 minutes ahead, and Meta holds it. It will go out even
if GitHub, your PC, and this repo are all down.

`state.json` records every published id and is committed back after each run, so
nothing can post twice.

---

## Adding a post

Append to `schedule.json`, drop the PNGs in `slides/<ID>/`, push.

```json
{
  "id": "D016",
  "slot_utc": "2026-09-20T17:30:00Z",
  "platforms": ["instagram", "facebook"],
  "slides_dir": "slides/D016",
  "slides": ["D016_01.png", "D016_02.png"],
  "caption": "…",
  "caption_fb": "optional, if Facebook differs",
  "hashtags": "#Decoded …"
}
```

`slot_utc` is **UTC**. Decoded posts in SAST, which is UTC+2 — so 19:30 SAST is
`17:30:00Z`. Getting this wrong posts two hours out.

Add `"hold": "reason"` to keep a post in the file but stop it publishing.

Carousels take **2–10 slides**. Instagram crops every slide to the aspect ratio
of the first one; yours are all 1080×1350, so nothing distorts.

---

## Currently held

- **D001** — slot passed unposted. Give it a new slot, then remove the hold.
- **D010** — the caption states "forty-one pings down to thirteen" as fact, but
  those numbers are draft placeholders. Put real numbers in or cut the sentence.

---

## When it breaks

**All posts fail, "Invalid OAuth access token"** — token revoked or password
changed. Redo step 3.5 for a new Page token.

**"Unsupported get request" on the IG id** — Instagram unlinked from the Page.
Relink in Page settings.

**Images fail to ingest** — the repo went private, or `ASSET_BASE_URL` is wrong.
Open a slide URL in a browser.

**Everything 400s after months of working** — Meta deprecated the API version.
Set the `GRAPH_VERSION` variable to the current one. Roughly a once-a-year job.

**Fallback:** `RUN_SHEET.html` in the parent folder has every caption and the
slide order for manual scheduling in Meta Business Suite.
