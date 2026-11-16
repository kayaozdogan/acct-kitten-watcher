import io
import json
import os
import re
import sys
import requests
from PIL import Image
from playwright.sync_api import sync_playwright

# ACCT Philly kaynak kodundan alinan gercek Shelterluv vitrin kimlikleri
SOURCES = [
    ("Main Facility", 1886, "https://acctphilly.org/available-cats/available-cats-main-facility/"),
    ("Offsite", 1857, "https://acctphilly.org/available-cats/available-cats-offsite/"),
]
NTFY_TOPIC = "kaya-acct-kitten-9482"  # Telefondaki ntfy kanal adinla birebir ayni yap
STATE_FILE = "seen_cats.json"

EMBED_TEMPLATE = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body>
  <div id="__WRAP_ID__"></div>
  <script type="text/javascript" src="https://new.shelterluv.com/misc/shelterluv_embed.js"></script>
  <script type="text/javascript">
    window.addEventListener("load", function () {
      var sourceDomain = "https://new.shelterluv.com";
      var base_path = "";
      var GID = 13458;
      var filters = {"saved_query": __QUERY__};
      EmbedAvailablePets("__WRAP_ID__", GID, filters, 1, sourceDomain, base_path, 2);
    });
  </script>
</body>
</html>"""


def load_seen() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_seen(data: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_likely_orange(img_url: str) -> bool:
    if not img_url or not img_url.startswith("http"):
        return False
    try:
        resp = requests.get(img_url, timeout=10)
        img = Image.open(io.BytesIO(resp.content)).convert("RGB").resize((80, 80))
        pixels = list(img.getdata())
        orange_hits = sum(
            1 for r, g, b in pixels
            if r > 155 and 75 < g < 195 and b < 115 and (r - b) > 55 and r > g
        )
        return (orange_hits / len(pixels)) > 0.14
    except Exception:
        return False


def notify(title: str, body: str, click_url: str, img_url: str = "", urgent: bool = False) -> None:
    headers = {
        "Title": title.encode("utf-8"),
        "Click": click_url,
        "Priority": "urgent" if urgent else "high",
        "Tags": "cat,rotating_light" if urgent else "cat",
    }
    if img_url.startswith("http"):
        headers["Attach"] = img_url
    try:
        r = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers=headers,
            timeout=15,
        )
        print(f"ntfy durum kodu: {r.status_code}")
    except Exception as e:
        print(f"Bildirim hatasi: {e}")


def select_filter_option(scope, button_label: str, option_label: str) -> str:
    btn = scope.locator("button:visible, [role='button']:visible", has_text=re.compile(rf"^{button_label}$", re.I))
    if btn.count() == 0:
        return "UI_ERROR"

    btn.first.click()
    scope.wait_for_timeout(700)

    opt = scope.locator(":visible", has_text=re.compile(rf"^{option_label}$", re.I))
    if opt.count() == 0:
        btn.first.click()
        scope.wait_for_timeout(400)
        return "NO_MATCH"

    opt.last.click()
    scope.wait_for_timeout(500)

    done_btn = scope.locator("button:visible, [role='button']:visible", has_text=re.compile(r"^Done$", re.I))
    if done_btn.count() > 0:
        done_btn.first.click()
        scope.wait_for_timeout(1200)
    return "SELECTED"


def extract_cats(scope, source_name: str, fallback_url: str) -> dict:
    found = {}
    ignore_names = {
        "sex", "attributes", "age group", "breed", "sort", "reset", "done",
        "species", "size groups", "color", "campus", "view all attributes",
        "view all breeds", "view all sorts",
    }

    imgs = scope.locator("img:visible")
    for i in range(imgs.count()):
        img = imgs.nth(i)
        src = img.get_attribute("src") or ""
        alt = img.get_attribute("alt") or ""
        if not src or "logo" in src.lower():
            continue

        info = img.evaluate(
            """el => {
                const card = el.closest('a, button, [role="button"], div[class*="card"], div[class*="animal"], div');
                const link = el.closest('a') || (card ? card.querySelector('a') : null);
                return {
                    text: card ? (card.innerText || '').trim() : '',
                    href: link ? (link.getAttribute('href') || '') : ''
                };
            }"""
        )
        name = re.sub(r"\s+", " ", info.get("text", "")).strip()
        if not name and "'s preview photo" in alt:
            name = alt.replace("'s preview photo", "").strip()

        if not name or len(name) < 2 or len(name) > 45 or name.lower() in ignore_names:
            continue

        href = info.get("href", "")
        if href.startswith("/"):
            profile_url = f"
