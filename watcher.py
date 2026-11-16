import io
import json
import os
import re
import sys
import requests
from PIL import Image
from playwright.sync_api import sync_playwright

# ACCT Philly'nin dogrudan canli Shelterluv vitrin adresleri (acctphilly.org engelini asar)
URLS = [
    ("ACCT Vitrin 1", "https://new.shelterluv.com/embed/13458?saved_query=1886&embedded=1"),
    ("ACCT Vitrin 2", "https://new.shelterluv.com/embed/13458?saved_query=1876&embedded=1"),
]
NTFY_TOPIC = "kaya-acct-kitten-9482"  # Telefondaki ntfy kanal adinla ayni yap
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
        r = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers=headers,
            timeout=15,
        )
        print(f"ntfy durum kodu: {r.status_code}")
    except Exception as e:
        print(f"Bildirim hatasi: {e}")


def select_filter_option(page, button_label: str, option_label: str) -> str:
    """
    Donus degerleri:
    - 'SELECTED': Secenek bulundu ve secildi
    - 'NO_MATCH': Filtre acildi ama o an bu secenekte hic kedi yok
    - 'UI_ERROR': Filtre butonu hic bulunamadi (sayfa yuklenmedi)
    """
    btn = page.locator("button:visible, [role='button']:visible", has_text=re.compile(rf"^{button_label}$", re.I))
    if btn.count() == 0:
        return "UI_ERROR"

    btn.first.click()
    page.wait_for_timeout(700)

    opt = page.locator(":visible", has_text=re.compile(rf"^{option_label}$", re.I))
    if opt.count() == 0:
        btn.first.click()
        page.wait_for_timeout(400)
        return "NO_MATCH"

    opt.last.click()
    page.wait_for_timeout(500)

    done_btn = page.locator("button:visible, [role='button']:visible", has_text=re.compile(r"^Done$", re.I))
    if done_btn.count() > 0:
        done_btn.first.click()
        page.wait_for_timeout(1200)
    return "SELECTED"


def extract_cats(page, source_name: str) -> dict:
    found = {}
    ignore_names = {
        "sex", "attributes", "age group", "breed", "sort", "reset", "done",
        "species", "size groups", "color", "campus", "view all attributes",
        "view all breeds", "view all sorts",
    }

    imgs = page.locator("img:visible")
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
            profile_url = f"https://new.shelterluv.com{href}"
        elif href.startswith("http"):
            profile_url = href
        else:
            profile_url = "https://acctphilly.org/available-cats/"

        cat_id = href if href else name
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

    for source_name, url in URLS:
        try:
            page.goto(url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(3000)
        except Exception as e:
            print(f"[{source_name}] Baglanti hatasi: {e}")
            continue

        sex_status = select_filter_option(page, "Sex", "Female")
        if sex_status == "UI_ERROR":
            print(f"[{source_name}] HATA: Sex filtresi bulunamadi!")
            continue

        successful_sources += 1
        if sex_status == "NO_MATCH":
            print(f"[{source_name}] Basariyla tarandi -> Su an 0 Female kaydi var.")
            continue

        age_status = select_filter_option(page, "Age Group", "Kitten")
        if age_status == "NO_MATCH":
            print(f"[{source_name}] Basariyla tarandi -> Su an 0 Female + Kitten kaydi var.")
            continue

        cats = extract_cats(page, source_name)
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

    # Hicbir vitrin okunamadiysa SESSIZCE GECME, alarm ver ve hata koduyla cik!
    if ok_count == 0:
        notify(
            title="⚠️ ACCT Botu Sayfayı Okuyamadı",
            body="Shelterluv vitrinine ulasilamadi veya arayuz degisti. GitHub Actions logunu kontrol et.",
            click_url="https://acctphilly.org/available-cats/",
            urgent=True,
        )
        sys.exit(1)

    if is_initial:
        save_seen(current)
        names = ", ".join(c["name"] for c in current.values()) or "su an 0 disi yavru var"
        notify(
            title="✅ ACCT Canlı Tarama Doğrulandı",
            body=f"Shelterluv veritabanina baglanildi. Mevcut durum: {names}. Yeni disi yavru dustugunde fotografiyle bildirim gelecek.",
            click_url="https://acctphilly.org/available-cats/",
        )
        return

    new_keys = [k for k in current if k not in seen]
    for key in new_keys:
        cat = current[key]
        orange = is_likely_orange(cat["img_url"])
        tag = "🧡 SARMAN DİŞİ YAVRU" if orange else "🐱 Yeni Dişi Yavru"
        title = f"{tag}: {cat['name']}"
        body = (
            f"Kaynak: {cat['source']}\n"
            f"Durum: {'Sarman tonu tespit edildi!' if orange else 'Yeni disi yavru eklendi.'}\n"
            f"Profilini acmak icin bildirime dokun."
        )
        notify(title=title, body=body, click_url=cat["url"], img_url=cat["img_url"], urgent=orange)

    if current != seen:
        save_seen(current)


if __name__ == "__main__":
    main()
