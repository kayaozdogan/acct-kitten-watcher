import io
import json
import os
import re
import requests
from PIL import Image
from playwright.sync_api import sync_playwright

URLS = [
    ("Main Facility", "https://acctphilly.org/available-cats/available-cats-main-facility/"),
    ("Offsite", "https://acctphilly.org/available-cats/available-cats-offsite/"),
]
NTFY_TOPIC = "kaya-acct-kitten-9482"  # Telefondaki ntfy kanal adınla değiştir
STATE_FILE = "seen_cats.json"


def load_seen() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_seen(data: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_likely_orange(img_url: str) -> bool:
    if not img_url:
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
    requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode("utf-8"), headers=headers, timeout=15)


def apply_female_kitten_filter(target) -> bool:
    try:
        sex_btn = target.get_by_text("Sex", exact=True)
        if sex_btn.count() == 0:
            return False
        sex_btn.first.click()
        target.wait_for_timeout(600)
        target.get_by_text("Female", exact=True).first.click()
        target.wait_for_timeout(400)
        target.get_by_text("Done", exact=True).first.click()
        target.wait_for_timeout(800)

        age_btn = target.get_by_text("Age Group", exact=True)
        age_btn.first.click()
        target.wait_for_timeout(600)
        target.get_by_text("Kitten", exact=True).first.click()
        target.wait_for_timeout(400)
        target.get_by_text("Done", exact=True).first.click()
        target.wait_for_timeout(1500)
        return True
    except Exception:
        return False


def scrape_kittens(page) -> dict:
    results = {}
    for source_name, url in URLS:
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(4000)

        for frame in page.frames:
            if not apply_female_kitten_filter(frame):
                continue
            links = frame.query_selector_all("a[href]")
            for link in links:
                href = link.get_attribute("href") or ""
                name = re.sub(r"\s+", " ", (link.inner_text() or "")).strip()
                if not name or len(name) < 2:
                    continue
                if name.lower() in {"back to all available cats", "donate", "learn more at www.shelterluv.com"}:
                    continue
                if "shelterluv.com" not in href and "animal" not in href.lower():
                    continue

                img_el = link.query_selector("img")
                img_url = img_el.get_attribute("src") if img_el else ""
                key = href if href.startswith("http") else f"{source_name}:{name}"
                results[key] = {
                    "name": name,
                    "source": source_name,
                    "url": href if href.startswith("http") else url,
                    "img_url": img_url or "",
                }
    return results


def main():
    seen = load_seen()
    is_initial = len(seen) == 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        current = scrape_kittens(page)
        browser.close()

    if not current:
        print("Liste yuklenemedi, sonraki calismada tekrar denenecek.")
        return

    if is_initial:
        save_seen(current)
        print(f"Ilk kayit tamamlandi: {len(current)} disi yavru kaydedildi.")
        return

    new_keys = [k for k in current if k not in seen]
    for key in new_keys:
        cat = current[key]
        orange = is_likely_orange(cat["img_url"])
        tag = "🧡 SARMAN DİŞİ YAVRU" if orange else "🐱 Yeni Dişi Yavru"
        title = f"{tag}: {cat['name']} ({cat['source']})"
        body = (
            f"Konum: {cat['source']}\n"
            f"Durum: {'Sarman tonu tespit edildi!' if orange else 'Yeni disi yavru eklendi.'}\n"
            f"Ilani acmak icin bildirime dokun."
        )
        notify(title=title, body=body, click_url=cat["url"], img_url=cat["img_url"], urgent=orange)

    if new_keys:
        seen.update(current)
        save_seen(seen)


if __name__ == "__main__":
    main()
