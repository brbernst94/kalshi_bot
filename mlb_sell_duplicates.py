"""
MLB The Show 26 - Auto-sell duplicate cards
Uses Playwright to log in via Xbox, then calls the inventory API
to find and list all duplicate MLB cards for sale.

Setup:
    pip install playwright python-dotenv
    playwright install chromium

Environment variables (in .env or shell):
    MLB_XBOX_EMAIL    - Your Microsoft/Xbox email
    MLB_XBOX_PASSWORD - Your Microsoft/Xbox password
    MLB_SELL_PRICE    - Price to list duplicates at (default: 1, i.e. best sell price)
"""

import os
import time
import json
import math
import requests
from dotenv import load_dotenv

load_dotenv()

XBOX_EMAIL = os.getenv("MLB_XBOX_EMAIL")
XBOX_PASSWORD = os.getenv("MLB_XBOX_PASSWORD")
# If 0 or not set, script will list at the current best sell price minus 1
SELL_PRICE = int(os.getenv("MLB_SELL_PRICE", "0"))

BASE_URL = "https://mlb26.theshow.com"


def login_and_get_session() -> requests.Session:
    """
    Launch a headless browser, complete the Xbox OAuth flow,
    then transfer cookies into a requests.Session.
    """
    from playwright.sync_api import sync_playwright

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    })

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        print("Navigating to MLB The Show login page...")
        page.goto(f"{BASE_URL}/sessions/new", wait_until="networkidle", timeout=30000)

        # Click the Xbox/Microsoft login button
        print("Clicking Xbox login...")
        xbox_btn = page.locator("a[href*='xbox'], a[href*='microsoft'], button:has-text('Xbox')")
        xbox_btn.first.click()
        page.wait_for_url("**/login.live.com/**", timeout=15000)

        # Fill Microsoft email
        print("Entering Xbox email...")
        page.fill("input[type='email'], input[name='loginfmt']", XBOX_EMAIL)
        page.click("input[type='submit'], button[type='submit']")
        page.wait_for_timeout(2000)

        # Fill password
        print("Entering Xbox password...")
        page.fill("input[type='password'], input[name='passwd']", XBOX_PASSWORD)
        page.click("input[type='submit'], button[type='submit']")
        page.wait_for_timeout(3000)

        # Handle "Stay signed in?" prompt if it appears
        try:
            stay_signed_in = page.locator("input#idBtn_Back, input[value='No']")
            if stay_signed_in.is_visible(timeout=3000):
                stay_signed_in.click()
                page.wait_for_timeout(2000)
        except Exception:
            pass

        # Wait to land back on theshow.com
        print("Waiting for redirect back to MLB The Show...")
        page.wait_for_url(f"{BASE_URL}/**", timeout=30000)
        page.wait_for_load_state("networkidle")

        # Transfer cookies to requests session
        for cookie in context.cookies():
            session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"])

        browser.close()
        print("Login successful.")

    return session


def get_inventory_page(session: requests.Session, page: int) -> dict:
    params = {
        "type": "mlb_card",
        "ownership": "owned",
        "show_duplicates": "yes",
        "page": page,
    }
    resp = session.get(f"{BASE_URL}/inventory.json", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_all_duplicates(session: requests.Session) -> list[dict]:
    """Fetch all pages of inventory and return only duplicate items."""
    print("Fetching inventory...")
    first = get_inventory_page(session, 1)
    total_pages = math.ceil(first.get("total_items", 0) / first.get("per_page", 25))
    items = first.get("items", [])

    for p in range(2, total_pages + 1):
        data = get_inventory_page(session, p)
        items.extend(data.get("items", []))
        time.sleep(0.5)  # be polite

    # Duplicates: items where the player/uuid appears more than once
    seen: dict[str, list] = {}
    for item in items:
        uuid = item.get("item", {}).get("uuid") or item.get("uuid")
        if uuid:
            seen.setdefault(uuid, []).append(item)

    duplicates = []
    for uuid, copies in seen.items():
        if len(copies) > 1:
            # Keep one, sell the rest
            duplicates.extend(copies[1:])

    print(f"Found {len(duplicates)} duplicate cards to sell.")
    return duplicates


def get_best_sell_price(session: requests.Session, uuid: str) -> int:
    """Look up the current best sell price for a card."""
    resp = session.get(f"{BASE_URL}/listings/{uuid}.json", timeout=10)
    if resp.status_code == 200:
        data = resp.json()
        listing = data.get("listing", {})
        price = listing.get("best_sell_price") or listing.get("best_buy_price", 1)
        return max(1, int(price) - 1)
    return 1


def list_card_for_sale(session: requests.Session, item: dict, price: int) -> bool:
    """POST a sell order for the given inventory item."""
    # The item UUID in inventory (not the card UUID)
    inv_uuid = item.get("uuid") or item.get("item", {}).get("uuid")
    card_uuid = item.get("item", {}).get("uuid", inv_uuid)
    name = item.get("item", {}).get("name", card_uuid)

    payload = {
        "listing": {
            "uuid": card_uuid,
            "type": "mlb_card",
            "price": price,
        }
    }

    # Try the community market listing endpoint
    headers = {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    }

    # Fetch CSRF token if present
    csrf = session.cookies.get("_theshow_session") or ""
    if csrf:
        headers["X-CSRF-Token"] = session.cookies.get("authenticity_token", "")

    resp = session.post(
        f"{BASE_URL}/community_market/listings.json",
        json=payload,
        headers=headers,
        timeout=15,
    )

    if resp.status_code in (200, 201):
        print(f"  Listed '{name}' for {price} stubs")
        return True
    else:
        print(f"  Failed to list '{name}': {resp.status_code} - {resp.text[:200]}")
        return False


def main():
    if not XBOX_EMAIL or not XBOX_PASSWORD:
        print("ERROR: Set MLB_XBOX_EMAIL and MLB_XBOX_PASSWORD in your .env file.")
        return

    session = login_and_get_session()
    duplicates = get_all_duplicates(session)

    if not duplicates:
        print("No duplicates found. Nothing to sell.")
        return

    success = 0
    fail = 0
    for item in duplicates:
        card_uuid = item.get("item", {}).get("uuid") or item.get("uuid", "")
        if SELL_PRICE > 0:
            price = SELL_PRICE
        else:
            price = get_best_sell_price(session, card_uuid)

        ok = list_card_for_sale(session, item, price)
        if ok:
            success += 1
        else:
            fail += 1
        time.sleep(0.75)  # avoid rate limiting

    print(f"\nDone. Listed: {success} | Failed: {fail}")


if __name__ == "__main__":
    main()
