import io
import json
import os
import re
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
NTFY_TOPIC = "kaya-acct-kitten-9482"
STATE_FILE = "seen_cats.json"
SCHEMA_VERSION = 2
MAX_AGE_MONTHS = 12.0  # 12 ay alti (yavru / genc) kediler


def load_checked() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("schema") == SCHEMA_VERSION:
                return data.get("checked", {})
        except Exception:
            pass
    return {}


def save_checked(checked: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"schema": SCHEMA_VERSION, "checked": checked}, f, ensure_ascii=False, indent=2)


def parse_age_months(age_str: str) -> float | None:
    if not age_str:
        return None
    y_m = re.search(r"(\d+)\s*Y", age_str, re.I)
    m_m = re.search(r"(\d+)\s*M", age_str, re.I)
    w_m = re.search(r"(\d+)\s*W", age_str, re.I)
    if not (y_m or m_m or w_m):
        return None
    years = int(y_m.group(1)) if y_m else 0
    months = int(m_m.group(1)) if m_m else 0
    weeks = int(w_m.group(1)) if w_m else 0
    return years * 12.0 + months + (weeks / 4.3)


def is_likely_orange(img_url: str, bio_text: str = "") -> bool:
    if re.search(r"\b(orange|ginger|marmalade)\b", bio_text, re.I):
        return True
    if not img_url or not img_url.startswith("http"):
        return False
    try:
        resp = requests.get(img_url, timeout=10)
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
        w, h = img.size
        # Kenarlardaki kafes/karton rengini elemek icin sadece ortadaki kediye odaklan
        center = img.crop((int(w * 0.30), int(h * 0.25), int(w * 0.70), int(h * 0.75)))
        hsv = center.convert("HSV").resize((60, 60))
        pixels = list(hsv.getdata())
        orange_hits = sum(
            1 for h_val, s_val, v_val in pixels
            if 8 <= h_val <= 26 and s_val > 95 and v_val > 90
        )
        return (orange_hits / len(pixels)) > 0.18
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
        print(f"ntfy durum kodu: {r.status_code}")
    except Exception as e:
        print(f"Bildirim hatasi: {e}")


def collect_listing_cards(page, source_name: str, fallback_url: str) -> list[dict]:
    cards = []
    seen_urls = set()
    imgs = page.locator("img:visible")

    for i in range(imgs.count()):
        img = imgs.nth(i)
        src = img.get_attribute("src") or ""
        alt = img.get_attribute("alt") or ""
        if not src or "logo" in src.lower():
            continue

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
        href = info.get("href", "")
        if not href:
            continue

        profile_url = (SL_DOMAIN + href) if href.startswith("/") else href
        if profile_url in seen_urls:
            continue
        seen_urls.add(profile_url)

        name = ""
        if "'s preview photo" in alt:
            name = alt.replace("'s preview photo", "").strip()
        if not name and info.get("text"):
            first_line = info["text"].splitlines()[0].strip()
            if 2 <= len(first_line) <= 40:
                name = first_line
        if not name:
            name = "Isimsiz Kedi"

        cards.append({
            "id": f"{source_name}:{href}",
            "name": name,
            "source": source_name,
            "url": profile_url,
            "img_url": src,
        })
    return cards


def inspect_animal_profile(page, cat: dict) -> dict | None:
    try:
        page.goto(cat["url"], wait_until="networkidle", timeout=45000)
        page.wait_for_timeout(1500)
        body_text = page.inner_text("body")
    except Exception as e:
        print(f"  -> Profil acilamadi ({cat['name']}): {e}")
        return None

    sex_match = re.search(r"\bSEX\s+(Female|Male)\b", body_text, re.I)
    age_match = re.search(r"\bAGE\s+([0-9YMW/]+)", body_text, re.I)

    sex = sex_match.group(1).capitalize() if sex_match else "Unknown"
    age_raw = age_match.group(1).upper() if age_match else "Unknown"
    age_months = parse_age_months(age_raw)

    return {
        "sex": sex,
        "age_raw": age_raw,
        "age_months": age_months,
        "bio": body_text,
    }


def main():
    checked = load_checked()
    all_cards = []
    ok_sources = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()

        for source_name, embed_url, public_url in SOURCES:
            try:
                page.goto(embed_url, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(3000)
                cards = collect_listing_cards(page, source_name, public_url)
                print(f"[{source_name}] Toplam {len(cards)} kedi karti listelendi.")
                if len(cards) > 0:
                    ok_sources += 1
                all_cards.extend(cards)
            except Exception as e:
                print(f"[{source_name}] Liste hatasi: {e}")

        if ok_sources == 0:
            browser.close()
            notify(
                title="⚠️ ACCT Botu Listeyi Okuyamadı",
                body="Shelterluv vitrinine ulasilamadi. GitHub Actions logunu kontrol et.",
                click_url=ACCT_DOMAIN + "/available-cats/",
                urgent=True,
            )
            sys.exit(1)

        new_cards = [c for c in all_cards if c["id"] not in checked]
        print(f"Daha once kontrol edilmemis yeni kedi sayisi: {len(new_cards)}")

        for cat in new_cards:
            details = inspect_animal_profile(page, cat)
            if not details:
                continue

            sex = details["sex"]
            age_raw = details["age_raw"]
            age_months = details["age_months"]

            checked[cat["id"]] = {
                "name": cat["name"],
                "source": cat["source"],
                "sex": sex,
                "age": age_raw,
            }

            is_female = (sex == "Female")
            is_kitten = (age_months is not None and age_months < MAX_AGE_MONTHS)

            if not (is_female and is_kitten):
                print(f"  [ELENDI] {cat['name']} ({cat['source']}) -> Sex: {sex}, Age: {age_raw}")
                continue

            print(f"  [ESLESME!] {cat['name']} ({cat['source']}) -> Sex: {sex}, Age: {age_raw}")
            orange = is_likely_orange(cat["img_url"], details["bio"])
            tag = "🧡 SARMAN DİŞİ YAVRU" if orange else "🐱 Yeni Dişi Yavru"
            title = f"{tag}: {cat['name']} ({age_raw})"
            body = (
                f"Konum: {cat['source']} | Cinsiyet: {sex} | Yaş: {age_raw}\n"
                f"Durum: {'Sarman tonu tespit edildi!' if orange else 'Yeni disi yavru eklendi.'}\n"
                f"Profilini acmak icin bildirime dokun."
            )
            notify(title=title, body=body, click_url=cat["url"], img_url=cat["img_url"], urgent=orange)

        browser.close()

    save_checked(checked)


if __name__ == "__main__":
    main()
