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


def select_filter_option(scope, button_label: str, option_label: str) -> bool:
    """Gorunur filtre butonunu acar, secenek varsa isaretler ve gorunur Done butonuna basar."""
    btn = scope.locator("button:visible, [role='button']:visible", has_text=re.compile(rf"^{button_label}$", re.I))
    if btn.count() == 0:
        return False
    btn.first.click()
    scope.wait_for_timeout(700)

    opt = scope.locator(":visible", has_text=re.compile(rf"^{option_label}$", re.I))
    if opt.count() == 0:
        # Secenek yoksa (ornegin o an hic Female yoksa) menuyu kapat
        btn.first.click()
        scope.wait_for_timeout(400)
        return False

    opt.last.click()
    scope.wait_for_timeout(500)

    done_btn = scope.locator("button:visible", has_text=re.compile(r"^Done$", re.I))
    if done_btn.count() > 0:
        done_btn.first.click()
        scope.wait_for_timeout(1200)
    return True


def extract_visible_cats(scope, source_name: str, fallback_url: str) -> dict:
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
        "english",
    }

    imgs = scope.locator("img:visible")
    count = imgs.count()
    for i in range(count):
        img = imgs.nth(i)
        src = img.get_attribute("src") or ""
        if not src or "logo" in src.lower() or "acct" in src.lower() or "wp-content" in src.lower():
            continue

        # Resmin bagli oldugu kart kapsayicisindan kedi adini ve varsa linkini al
        card_info = img.evaluate(
            """el => {
                const card = el.closest('a, button, [role="button"], div[class*="card"], div[class*="animal"], div');
                if (!card) return { name: '', href: '' };
                const link = card.closest('a') || card.querySelector('a');
                return {
                    name: (card.innerText || '').trim(),
                    href: link ? (link.href || '') : ''
                };
            }"""
        )
        name = re.sub(r"\s+", " ", card_info.get("name", "")).strip()
        href = card_info.get("href", "")
        if not name or len(name) < 2 or len(name) > 40 or name.lower() in ignore_names:
            continue

        key = f"{source_name}:{name}:{src.split('?')[0]}"
        found[key] = {
            "name": name,
            "source": source_name,
            "url": href if href.startswith("http") else fallback_url,
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

        # Hem ana sayfayi hem olasi iframe'leri tara
        scopes = [page] + [f for f in page.frames if f != page.main_frame]
        for scope in scopes:
            sex_btn = scope.locator("button:visible, [role='button']:visible", has_text=re.compile(r"^Sex$", re.I))
            if sex_btn.count() == 0:
                continue

            has_female = select_filter_option(scope, "Sex", "Female")
            has_kitten = select_filter_option(scope, "Age Group", "Kitten") if has_female else False

            if has_female and has_kitten:
                cards = extract_visible_cats(scope, source_name, url)
                print(f"[{source_name}] Filtrelenen disi yavru sayisi: {len(cards)}")
                results.update(cards)
            else:
                print(f"[{source_name}] Su an eslesen Female + Kitten bulunamadi (0 kayit).")
            break

    return results


def main():
    is_initial = not os.path.exists(STATE_FILE)
    seen = load_seen()

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

    if is_initial:
        save_seen(current)
        names = ", ".join(f"{v['name']} ({v['source']})" for v in current.values()) or "su an 0 disi yavru var"
        print(f"Ilk kayit tamamlandi: {names}")
        notify(
            title="✅ ACCT Takip Botu Aktif",
            body=f"Tarama basladi ({names}). Yeni bir disi yavru dustugunde aninda bildirim gelecek.",
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
