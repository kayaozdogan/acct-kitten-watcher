import io
import json
import os
import sys
import requests
from PIL import Image
from playwright.sync_api import sync_playwright

PROTO = "https" + "://"
SL_DOMAIN = PROTO + "new.shelterluv.com"
ACCT_DOMAIN = PROTO + "acctphilly.org"

SOURCES = [
    (
        "Main Facility",
        SL_DOMAIN + "/embed/13458?saved_query=1886&embedded=1&iframeId=shelterluv_wrap_1792095625&columns=1",
        ACCT_DOMAIN + "/available-cats/available-cats-main-facility/",
    ),
    (
        "Offsite",
        SL_DOMAIN + "/embed/13458?saved_query=1857&embedded=1&iframeId=shelterluv_wrap_1737573472&columns=1",
        ACCT_DOMAIN + "/available-cats/available-cats-offsite/",
    ),
]
NTFY_TOPIC = "kaya-acct-kitten-9482"  # Telefondaki ntfy kanal adinla birebir ayni yap
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
        ntfy_url = PROTO + "ntfy.sh/" + NTFY_TOPIC.strip()
        r = requests.post(
            ntfy_url,
            data=body.encode("utf-8"),
            headers=headers,
            timeout=15,
        )
        print(f"ntfy durum kodu: {r.status_code} -> kanal: {NTFY_TOPIC.strip()}")
    except Exception as e:
        print(f"Bildirim hatasi: {e}")


def select_filter_option(page, button_label: str, option_label: str) -> str:
    triggers = page.get_by_text(button_label, exact=True).locator("visible=true")
    if triggers.count() == 0:
        return "UI_ERROR"

    triggers.first.click()
    page.wait_for_timeout(800)

    options = page.get_by_text(option_label, exact=True).locator("visible=true")
    if options.count() == 0:
        triggers.first.click()
        page.wait_for_timeout(400)
        return "NO_MATCH"

    options.first.click()
    page.wait_for_timeout(600)

    done_btns = page.get_by_text("Done", exact=True).locator("visible=true")
    if done_btns.count() > 0:
        done_btns.first.click()
        page.wait_for_timeout(1500)
    return "SELECTED"


def extract_cats(page, source_name: str, fallback_url: str) -> dict:
    found = {}
    imgs = page.locator("img:visible")
    for i in range(imgs.count()):
        img = imgs.nth(i)
        src = img.get_attribute("src") or ""
        alt = img.get_attribute("alt") or ""
        if not src or "logo" in src.lower():
            continue

        name = ""
        if "'s preview photo" in alt:
            name = alt.replace("'s preview photo", "").strip()

        info = img.evaluate(
            """el => {
                const card = el.closest('a, [class*="card"], [class*="animal"], li, div');
                const link = el.closest('a') || (card ? card.querySelector('a') : null);
                return {
                    text: card ? (card.innerText || '').trim() : '',
                    href: link ? (link.getAttribute('href') || '') : ''
                };
            }"""
        )
        if not name and info.get("text"):
            first_line = info["text"].splitlines()[0].strip()
            if 2 <= len(first_line) <= 40:
                name = first_line

        if not name:
            continue

        href = info.get("href", "")
        if href.startswith("/"):
            profile_url = SL_DOMAIN + href
        elif href.startswith("http"):
            profile_url = href
        else:
            profile_url = fallback_url

        cat_id = f"{source_name}:{href or name}"
        found[cat_id] = {
            "name": name,
            "source": source_name,
            "url": profile_url,
            "img_url": src,
        }
    return found


def scrape_all(page) -> tuple[dict, int]:
    all_cats = {}
    successful_sources = 0

    for source_name, embed_url, public_url in SOURCES:
        try:
            page.goto(embed_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(3500)
        except Exception as e:
            print(f"[{source_name}] Baglanti hatasi: {e}")
            continue

        sex_status = select_filter_option(page, "Sex", "Female")
        if sex_status == "UI_ERROR":
            snippet = " ".join(page.inner_text("body").split())[:220]
            print(f"[{source_name}] HATA: Sex filtresi gorunmedi. Sayfa ozeti: {snippet}")
            continue

        successful_sources += 1
        if sex_status == "NO_MATCH":
            print(f"[{source_name}] Basariyla tarandi -> Su an 0 Female kaydi var.")
            continue

        age_status = select_filter_option(page, "Age Group", "Kitten")
        if age_status == "NO_MATCH":
            print(f"[{source_name}] Basariyla tarandi -> Su an 0 Female + Kitten kaydi var.")
            continue

        cats = extract_cats(page, source_name, public_url)
        print(f"[{source_name}] Basariyla tarandi -> {len(cats)} disi yavru bulundu: {[c['name'] for c in cats.values()]}")
        all_cats.update(cats)

    return all_cats, successful_sources


def main():
    is_initial = not os.path.exists(STATE_FILE)
    seen = load_seen()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()
        current, ok_count = scrape_all(page)
        browser.close()

    if ok_count == 0:
        notify(
            title="⚠️ ACCT Botu Sayfayı Okuyamadı",
            body="Shelterluv vitrinine ulasilamadi. GitHub Actions logunu kontrol et.",
            click_url=ACCT_DOMAIN + "/available-cats/",
            urgent=True,
        )
        sys.exit(1)

    if is_initial:
        save_seen(current)
        names = ", ".join(f"{c['name']} ({c['source']})" for c in current.values()) or "su an 0 disi yavru var"
        notify(
            title="✅ ACCT Canlı Tarama Aktif",
            body=f"Main Facility ve Offsite canli baglandi ({names}). Yeni disi yavru dustugunde fotografiyle bildirim gelecek.",
            click_url=ACCT_DOMAIN + "/available-cats/",
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

    if current != seen:
        save_seen(current)


if __name__ == "__main__":
    main()
