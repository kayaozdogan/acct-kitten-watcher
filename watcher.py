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
    ("Main Facility", 1886, ACCT_DOMAIN + "/available-cats/available-cats-main-facility/"),
    ("Offsite", 1857, ACCT_DOMAIN + "/available-cats/available-cats-offsite/"),
]
NTFY_TOPIC = "kaya-acct-kitten-9482"  # Telefondaki ntfy kanal adinla birebir ayni olmali
STATE_FILE = "seen_cats.json"


def build_embed_html(wrap_id: str, saved_query: int) -> str:
    js_src = SL_DOMAIN + "/misc/shelterluv_embed.js"
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
        f"<div id='{wrap_id}'></div>"
        f"<script type='text/javascript' src='{js_src}'></script>"
        "<script type='text/javascript'>"
        "window.addEventListener('load', function () {"
        f"  EmbedAvailablePets('{wrap_id}', 13458, {{'saved_query': {saved_query}}}, 1, '{SL_DOMAIN}', '', 2);"
        "});"
        "</script></body></html>"
    )


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

    for source_name, saved_query, public_url in SOURCES:
        wrap_id = f"shelterluv_wrap_{saved_query}"
        html = build_embed_html(wrap_id, saved_query)
        virtual_url = ACCT_DOMAIN + f"/watcher-embed-{saved_query}"
        page.route(
            virtual_url,
            lambda route, request, body=html: route.fulfill(
                status=200, content_type="text/html", body=body
            ),
        )

        try:
            page.goto(virtual_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(4000)
        except Exception as e:
            print(f"[{source_name}] Yukleme hatasi: {e}")
            continue

        scopes = [page] + [f for f in page.frames if f != page.main_frame]
        matched_scope = None
        sex_status = "UI_ERROR"

        for scope in scopes:
            status = select_filter_option(scope, "Sex", "Female")
            if status != "UI_ERROR":
                matched_scope = scope
                sex_status = status
                break

        if not matched_scope:
            print(f"[{source_name}] HATA: Shelterluv arayuzu yuklenemedi.")
            continue

        successful_sources += 1
        if sex_status == "NO_MATCH":
            print(f"[{source_name}] Basariyla tarandi -> Su an 0 Female kaydi var.")
            continue

        age_status = select_filter_option(matched_scope, "Age Group", "Kitten")
        if age_status == "NO_MATCH":
            print(f"[{source_name}] Basariyla tarandi -> Su an 0 Female + Kitten kaydi var.")
            continue

        cats = extract_cats(matched_scope, source_name, public_url)
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
