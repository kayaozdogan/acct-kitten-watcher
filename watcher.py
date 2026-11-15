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
NTFY_TOPIC = "kaya-acct-kitten-9482"  # Telefondaki ntfy kanal adınla aynı yap
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
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode("utf-8"), headers=headers, timeout=15)
    except Exception as e:
        print(f"Bildirim hatasi: {e}")


def apply_female_kitten_filter(target) -> bool:
    try:
        sex_btn = target.get_by_text("Sex", exact=True)
        if sex_btn.count() == 0:
            return False
        sex_btn.first.click()
        target.wait_for_timeout(700)
        target.get_by_text("Female", exact=True).first.click()
        target.wait_for_timeout(500)
        target.get_by_text("Done", exact=True).first.click()
        target.wait_for_timeout(1000)

        age_btn = target.get_by_text("Age Group", exact=True)
        age_btn.first.click()
        target.wait_for_timeout(700)
        target.get_by_text("Kitten", exact=True).first.click()
        target.wait_for_timeout(500)
        target.get_by_text("Done", exact=True).first.click()
        target.wait_for_timeout(2000)
        return True
    except Exception as e:
        print(f"Filtre uyarisi: {e}")
        return False


def extract_cards_from_frame(frame, source_name: str, fallback_url: str) -> dict:
    found = {}
    ignore_names = {
        "back to all available cats",
        "donate",
        "learn more at www.shelterluv.com",
        "sex",
        "attributes",
        "age group",
        "breed",
        "sort",
        "reset",
        "done",
    }

    # 1. Link iceren hayvan kartlarini tara
    for link in frame.query_selector_all("a[href]"):
        href = link.get_attribute("href") or ""
        name = re.sub(r"\s+", " ", (link.inner_text() or "")).strip()
        if not name or len(name) < 2 or len(name) > 40 or name.lower() in ignore_names:
            continue
        img_el = link.query_selector("img")
        if not img_el and "shelterluv" not in href.lower() and "animal" not in href.lower():
            continue
        img_url = (img_el.get_attribute("src") or "") if img_el else ""
        full_url = href if href.startswith("http") else fallback_url
        key = f"{source_name}:{name}:{href}"
        found[key] = {
            "name": name,
            "source": source_name,
            "url": full_url,
            "img_url": img_url,
        }

    # 2. Shelterluv kartlari <a> yerine <div>/button olarak render edildiyse yakala
    if not found:
        for img in frame.query_selector_all("img"):
            src = img.get_attribute("src") or ""
            if not src or "logo" in src.lower() or "acct" in src.lower():
                continue
            parent = img.evaluate_handle("el => el.closest('a, button, [role=\"button\"], div')")
            if not parent:
                continue
            text = re.sub(r"\s+", " ", (parent.as_element().inner_text() or "")).strip()
            if 2 <= len(text) <= 35 and text.lower() not in ignore_names:
                key = f"{source_name}:{text}"
                found[key] = {
                    "name": text,
                    "source": source_name,
                    "url": fallback_url,
                    "img_url": src,
                }

    return found


def scrape_kittens(page) -> dict:
    results = {}
    for source_name, url in URLS:
        loaded = False
        for attempt in range(2):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(5000)
                loaded = True
                break
            except Exception as e:
                print(f"[{source_name}] Deneme {attempt + 1} hatasi: {e}")
                page.wait_for_timeout(2000)

        if not loaded:
            continue

        for frame in page.frames:
            if apply_female_kitten_filter(frame):
                cards = extract_cards_from_frame(frame, source_name, url)
                print(f"[{source_name}] Filtrelenen disi yavru sayisi: {len(cards)}")
                results.update(cards)
                break

    return results


def main():
    seen = load_seen()
    is_initial = len(seen) == 0

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-http2",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()
        current = scrape_kittens(page)
        browser.close()

    if not current:
        print("Su an filtrelenmis disi yavru bulunamadi veya liste bos dondu.")
        return

    if is_initial:
        save_seen(current)
        names = ", ".join(f"{v['name']} ({v['source']})" for v in current.values())
        print(f"Ilk kayit tamamlandi: {names}")
        notify(
            title="✅ ACCT Takip Botu Aktif",
            body=f"Su anki {len(current)} disi yavru kaydedildi ({names}). Yeni bir dişi yavru dustugunde aninda bildirim gelecek.",
            click_url=URLS[0][1],
        )
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
