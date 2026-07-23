
Scrape hypixel · PY
"""
Scrapes the Hypixel forums SkyBlock Patch Notes subforum for new threads
and posts any new ones to a Discord webhook.
 
State (which thread IDs have already been posted) is kept in state.json,
which the workflow commits back to the repo after each run.
"""
 
import json
import os
import sys
from pathlib import Path
 
import requests
from bs4 import BeautifulSoup
 
FORUM_URL = "https://hypixel.net/forums/skyblock-patch-notes.158/"
STATE_FILE = Path(__file__).parent.parent / "state.json"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
 
HEADERS = {
    # A normal browser UA avoids some basic bot filtering. Be a reasonable
    # citizen: this only hits the forum on a schedule (e.g. every 30 min).
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
 
 
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen_ids": []}
 
 
def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))
 
 
def fetch_threads() -> list[dict]:
    resp = requests.get(FORUM_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
 
    threads = []
    # XenForo 2 forum listings use <div class="structItem structItem--thread">
    for item in soup.select("div.structItem--thread"):
        title_el = item.select_one(".structItem-title a[href]")
        if not title_el:
            continue
 
        href = title_el["href"]
        # href looks like /threads/some-slug.6087215/
        thread_id = href.rstrip("/").split(".")[-1]
        url = f"https://hypixel.net{href}"
        title = title_el.get_text(strip=True)
 
        date_el = item.select_one(".structItem-startDate time")
        date_str = date_el["title"] if date_el and date_el.has_attr("title") else None
 
        threads.append(
            {
                "id": thread_id,
                "title": title,
                "url": url,
                "date": date_str,
            }
        )
 
    return threads
 
 
def post_to_discord(thread: dict) -> None:
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL not set, skipping Discord post", file=sys.stderr)
        return
 
    date_suffix = f" ({thread['date']})" if thread["date"] else ""
    payload = {
        "embeds": [
            {
                "title": thread["title"],
                "url": thread["url"],
                "description": f"New SkyBlock patch notes thread posted{date_suffix}.",
                "color": 0x2ECC71,
            }
        ]
    }
    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
    resp.raise_for_status()
 
 
def main() -> None:
    state = load_state()
    seen_ids = set(state.get("seen_ids", []))
 
    threads = fetch_threads()
    if not threads:
        print("No threads found — page structure may have changed.", file=sys.stderr)
        return
 
    new_threads = [t for t in threads if t["id"] not in seen_ids]
 
    # First run: don't blast every existing thread to Discord, just baseline.
    first_run = not seen_ids
    if first_run:
        print(f"First run — baselining {len(threads)} threads without posting.")
    else:
        for thread in reversed(new_threads):  # oldest new thread first
            print(f"New thread: {thread['title']} ({thread['url']})")
            post_to_discord(thread)
 
    seen_ids.update(t["id"] for t in threads)
    save_state({"seen_ids": sorted(seen_ids)})
 
 
if __name__ == "__main__":
    main()
 
