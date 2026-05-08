import os, json, requests, gspread, base64, hashlib, time
import numpy as np
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from google.oauth2.service_account import Credentials
from datetime import datetime

TEMPLATE_PATH = "Monday.png"
SHEET_NAME = "eSIMsData.com"
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
TEXT_COLOR = (255, 255, 255)

DAY_TEMPLATES = {
    "monday":    "Monday.png",
    "wednesday": "Wednesday.png",
    "thursday":  "Thursday.png",
    "friday":    "Friday.png",
}

PHOTO_BOX        = {"x": 248, "y": 237, "w": 553, "h": 555}
OPERATOR_LOGO_BOX = {"x": 258, "y": 143, "w": 370, "h": 77}
COUNTRY_TEXT     = {"cx": 524, "y1": 810, "y2": 865, "font_size": 44}
HEADERS = {"User-Agent": "eSIMSDataBot/1.0 (esimsdata.com)"}

def get_sheets_client():
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    return gspread.authorize(creds)

def get_next_pending_row(sheet):
    records = sheet.get_all_records()
    for i, row in enumerate(records):
        if str(row.get("\u0421\u0442\u0430\u0442\u0443\u0441", "")).strip() == "\u041e\u0436\u0438\u0434\u0430\u0435\u0442":
            return i + 2, row
    return None, None

def update_row_status(sheet, row_index, status, post_url=""):
    sheet.update_cell(row_index, 6, status)
    if post_url:
        sheet.update_cell(row_index, 7, datetime.now().strftime("%d.%m.%Y"))
        sheet.update_cell(row_index, 8, post_url)

def get_country_photo(country_name):
    import re, unicodedata

    def fix_name(s):
        s = ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')
        s = re.sub(r'([aeiouAEIOU])\\1+', r'\\1', s)
        return s

    fixed_name = fix_name(country_name)

    # Try Unsplash first
    access_key = os.environ.get("UNSPLASH_ACCESS_KEY", "")
    if access_key:
        first_orig = country_name.split()[0] if ' ' in country_name else country_name
        queries = [country_name, first_orig + " country", fixed_name]
        for q in queries:
            try:
                resp = requests.get(
                    "https://api.unsplash.com/search/photos",
                    params={"query": f"{q} travel landscape", "per_page": 3, "orientation": "landscape"},
                    headers={"Authorization": f"Client-ID {access_key}"},
                    timeout=15
                )
                if resp.status_code != 200:
                    print(f"Unsplash invalid key, skipping")
                    break
                results = resp.json().get("results", [])
                print(f"Unsplash '{q}': {len(results)}")
                if results:
                    img_resp = requests.get(results[0]["urls"]["regular"], timeout=20)
                    return Image.open(BytesIO(img_resp.content)).convert("RGB")
            except Exception as e:
                print(f"Unsplash error: {e}")
                break

    # Wikimedia Commons fallback - search with landscape keyword
    # Using fixed name (Aland Islands) gives much better landscape results
    wm_queries = [
        f"{country_name} view",
        f"{country_name} scenery",
        f"{country_name} nature",
        f"{country_name} landscape",
        f"{fixed_name} view",
        f"{fixed_name} scenery",
        f"{fixed_name} landscape",
        fixed_name,
    ]
    print(f"Trying Wikimedia Commons for: {country_name} (fixed: {fixed_name})")
    for q in wm_queries:
        try:
            r = requests.get(
                "https://commons.wikimedia.org/w/api.php",
                params={
                    "action": "query", "generator": "search",
                    "gsrsearch": q,
                    "gsrnamespace": 6, "gsrlimit": 20,
                    "prop": "imageinfo", "iiprop": "url|size",
                    "iiurlwidth": 1080, "format": "json"
                },
                headers=HEADERS, timeout=15
            )
            pages = r.json().get("query", {}).get("pages", {})
            print(f"Wikimedia '{q}': {len(pages)} files")
            for page in pages.values():
                title = page.get("title", "").lower()
                iinfo = page.get("imageinfo", [])
                if not iinfo: continue
                img_url = iinfo[0].get("thumburl") or iinfo[0].get("url", "")
                if not img_url: continue
                # Skip SVG and non-image files
                ext = img_url.lower().split("?")[0].split(".")[-1]
                if ext not in ["jpg", "jpeg", "png"]: continue
                # Skip logos, icons, maps, flags, coats of arms
                skip_words = ["logo", "icon", "map", "flag", "coat", "arms", "stamp", "badge", "symbol", "sign", "emblem",
                             "satellite", "nasa", "copernicus", "landsat", "sentinel", "aerial", "terrain",
                             "topograph", "radar", "infrared", "false.color", "false_color",
                             "editable", "a4", "plan", "base", "military", "schema", "chart",
                             "carta", "carte", "mapa", "karte", "kaart",
                             "eel", "fish", "migrat", "species", "specimen", "scienti",
                             "ncomms", "comms", "journal", "paper", "figure", "fig.",
                             ".pdf", ".svg", ".gif", ".tif"]
                # Skip satellite IDs in parentheses like (51255690084) but allow dates like 20100128
                import re as _re
                if _re.search(r'\(\d{9,}\)', title.lower()):
                    print(f"Skipping satellite ID: {title}")
                    continue
                # Skip archive.org files (ia catalog_id) and PDF files
                if _re.search(r'\(ia [a-z]', title.lower()) or title.lower().endswith('.pdf'):
                    print(f"Skipping archive/pdf: {title}")
                    continue
                if any(w in title for w in skip_words): continue
                # Prefer landscape/scenery files
                try:
                    img_resp = requests.get(img_url, headers=HEADERS, timeout=20)
                    if img_resp.status_code == 200 and len(img_resp.content) > 50000:
                        photo = Image.open(BytesIO(img_resp.content)).convert("RGB")
                        w, h = photo.size
                        # Prefer landscape orientation (wider than tall)
                        if w < h:
                            print(f"Skipping portrait: {title}")
                            continue
                        # Check if image looks like a natural landscape (not satellite/false-color)
                        arr = np.array(photo.resize((50, 50)))
                        r, g, b = arr[:,:,0].mean(), arr[:,:,1].mean(), arr[:,:,2].mean()
                        # Satellite false-color: very high R with low B, or extreme channel imbalance
                        max_ch = max(r, g, b)
                        min_ch = min(r, g, b)
                        imbalance = max_ch - min_ch
                        # Natural photos: imbalance < 80, no extreme red dominance
                        if imbalance > 80 and r > g + 40 and r > b + 40:
                            print(f"Skipping satellite-like image: {title} rgb=({r:.0f},{g:.0f},{b:.0f})")
                            continue
                        print(f"Wikimedia photo: {title} | {w}x{h}")
                        return photo
                except Exception as e2:
                    continue
        except Exception as e:
            print(f"Wikimedia error: {e}")
    print(f"No photo found for: {country_name}")
    return None

def search_commons_logo(query, operator_name):
    try:
        search_resp = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={"action":"query","list":"search","srsearch":query,
                    "srnamespace":"6","srlimit":"10","format":"json"},
            headers=HEADERS, timeout=15
        )
        if not search_resp.content or not search_resp.text.strip():
            return None
        results = search_resp.json().get("query",{}).get("search",[])
        operator_words = operator_name.lower().split()
        for result in results:
            title = result["title"]
            title_lower = title.lower()
            if not any(title_lower.endswith(ext) for ext in [".png",".jpg",".jpeg"]):
                continue
            if not any(w in title_lower for w in operator_words):
                continue
            file_name = title.replace("File:","").strip()
            direct_url = f"https://commons.wikimedia.org/wiki/Special:FilePath/{file_name}?width=400"
            try:
                img_resp = requests.get(direct_url, headers=HEADERS, timeout=15, allow_redirects=True)
                if img_resp.status_code == 200 and len(img_resp.content) > 500:
                    ctype = img_resp.headers.get('content-type','')
                    if 'image' in ctype or title_lower.endswith(('.jpg','.jpeg','.png')):
                        return Image.open(BytesIO(img_resp.content)).convert("RGBA")
            except:
                pass
    except Exception as e:
        print(f"Commons search error: {e}")
    return None


def get_operator_logo_fandom(operator_name, country_name):
    """Get operator logo using Fandom MediaWiki API - not blocked unlike HTML scraping"""
    FANDOM_API = "https://prepaid-data-sim-card.fandom.com/api.php"
    NOT_LOGO = ["flag", "map", "coat", "arms", "anthem", "location", "emblem",
                "seal", "banner", "geography", "region", "outline", ".mp3", ".ogg"]
    LOWERCASE_WORDS = {"and", "or", "of", "the", "in", "at", "by", "for", "with", "a", "an"}

    def country_to_slug(name):
        words = name.strip().split()
        result = [words[0]] if words else []
        for w in words[1:]:
            result.append(w.lower() if w.lower() in LOWERCASE_WORDS else w)
        return "_".join(result)

    slug1 = country_to_slug(country_name)
    slug2 = "_".join(country_name.strip().split())
    slugs = list(dict.fromkeys([slug1, slug2]))

    for slug in slugs:
        try:
            # Step 1: Get list of images on the country page via API
            r = requests.get(FANDOM_API, params={
                "action": "query", "titles": slug.replace("_", " "),
                "prop": "images", "format": "json"
            }, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                print(f"Fandom API: {r.status_code} for {slug}")
                continue
            pages = r.json().get("query", {}).get("pages", {})
            page = next(iter(pages.values()), {})
            if page.get("missing") is not None:
                print(f"Fandom page missing: {slug}")
                continue
            images = page.get("images", [])
            print(f"Fandom API found {len(images)} images for {slug}")

            # Filter: skip flags, maps, audio files
            op_words = operator_name.lower().split()
            logo_images = [i for i in images
                          if not any(bad in i['title'].lower() for bad in NOT_LOGO)
                          and not i['title'].lower().endswith(('.svg', '.mp3', '.ogg'))
                          and not (len(i['title'].replace('File:', '').replace('.png', '').replace('.jpg', '')) <= 3)]

            # Sort: exact operator match first, then others
            exact = [i for i in logo_images if any(w in i['title'].lower() for w in op_words)]
            fallback = [i for i in logo_images if not any(w in i['title'].lower() for w in op_words)]
            ordered = exact + fallback

            print(f"Fandom candidates: {[i['title'] for i in ordered[:5]]}")

            # Step 2: Get URL for each candidate via API
            for img_info in ordered[:5]:
                try:
                    r2 = requests.get(FANDOM_API, params={
                        "action": "query", "titles": img_info['title'],
                        "prop": "imageinfo", "iiprop": "url",
                        "iiurlwidth": "400", "format": "json"
                    }, headers=HEADERS, timeout=15)
                    if r2.status_code != 200:
                        continue
                    pages2 = r2.json().get("query", {}).get("pages", {})
                    page2 = next(iter(pages2.values()), {})
                    img_url = page2.get("imageinfo", [{}])[0].get("url", "")
                    if not img_url:
                        continue
                    print(f"Fandom logo URL: {img_info['title']} -> {img_url[:60]}")
                    img_resp = requests.get(img_url, headers=HEADERS, timeout=15)
                    if img_resp.status_code == 200 and len(img_resp.content) > 500:
                        return Image.open(BytesIO(img_resp.content)).convert("RGBA")
                except Exception as e2:
                    print(f"Fandom image error: {e2}")
            return None
        except Exception as e:
            print(f"Fandom API error for {slug}: {e}")
    return None

def get_operator_logo(operator_name, country_name=""):
    if not operator_name or operator_name.strip() == "":
        return None
    logo = get_operator_logo_fandom(operator_name, country_name)
    if logo:
        return logo
    print(f"Fandom failed, trying Wikimedia for: {operator_name}")
    queries = [
        f"{country_name} {operator_name} logo" if country_name else None,
        f"{operator_name} logo",
        f"{operator_name} telecom logo",
    ]
    for query in queries:
        if not query:
            continue
        print(f"Searching Commons: {query}")
        for attempt in range(3):
            img = search_commons_logo(query, operator_name)
            if img:
                return img
            if attempt < 2:
                print(f"Retrying logo search ({attempt+2}/3)...")
                time.sleep(1)
            else:
                break
    print(f"No logo found for: {operator_name} ({country_name})")
    return None

def generate_esim_image(country, country_photo, operator_logo, template_path):
    template = Image.open(template_path).convert("RGBA")
    template_arr = np.array(template).copy()
    template_arr[PHOTO_BOX["y"]:PHOTO_BOX["y"]+PHOTO_BOX["h"],
                 PHOTO_BOX["x"]:PHOTO_BOX["x"]+PHOTO_BOX["w"], 3] = 0
    template_no_photo = Image.fromarray(template_arr, "RGBA")
    canvas = Image.new("RGBA", template.size, (255, 255, 255, 255))
    if country_photo:
        photo = country_photo.resize((PHOTO_BOX["w"], PHOTO_BOX["h"]), Image.LANCZOS)
        canvas.paste(photo, (PHOTO_BOX["x"], PHOTO_BOX["y"]))
        print("Photo pasted")
    else:
        print("No photo - using white background")
    canvas.paste(template_no_photo, (0, 0), mask=template_no_photo)
    if operator_logo:
        lw, lh = OPERATOR_LOGO_BOX["w"], OPERATOR_LOGO_BOX["h"]
        logo = operator_logo.copy()
        scale = min(lw / logo.width, lh / logo.height)
        new_w = int(logo.width * scale)
        new_h = int(logo.height * scale)
        logo = logo.resize((new_w, new_h), Image.LANCZOS)
        x = OPERATOR_LOGO_BOX["x"] + (lw - new_w) // 2
        y = OPERATOR_LOGO_BOX["y"] + (lh - new_h) // 2
        if logo.mode == "RGBA":
            canvas.paste(logo, (x, y), logo)
        else:
            canvas.paste(logo, (x, y))
        print("Operator logo pasted")
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", COUNTRY_TEXT["font_size"])
    except Exception:
        font = ImageFont.load_default()
    for text, y in [("Your eSIM", COUNTRY_TEXT["y1"]), (f"for {country}", COUNTRY_TEXT["y2"])]:
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        draw.text((COUNTRY_TEXT["cx"] - w // 2, y), text, fill=TEXT_COLOR, font=font)
    return canvas.convert("RGB")

def upload_to_cloudinary(image):
    cloud_name = os.environ["CLOUDINARY_CLOUD_NAME"]
    api_key = os.environ["CLOUDINARY_API_KEY"]
    api_secret = os.environ["CLOUDINARY_API_SECRET"]
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    img_b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
    timestamp = str(int(time.time()))
    sig = hashlib.sha1(f"timestamp={timestamp}{api_secret}".encode()).hexdigest()
    resp = requests.post(
        f"https://api.cloudinary.com/v1_1/{cloud_name}/image/upload",
        data={"file": f"data:image/jpeg;base64,{img_b64}", "api_key": api_key,
              "timestamp": timestamp, "signature": sig},
        timeout=60
    )
    url = resp.json().get("secure_url", "")
    print(f"Uploaded to Cloudinary: {url}")
    return url

def post_to_ayrshare(image_url, caption):
    api_key = os.environ["AYRSHARE_API_KEY"]
    resp = requests.post(
        "https://api.ayrshare.com/api/post",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"post": caption, "platforms": ["instagram"], "mediaUrls": [image_url]},
        timeout=120
    )
    print(f"Ayrshare {resp.status_code}: {resp.text[:200]}")
    resp.raise_for_status()
    result = resp.json()
    post_data = result.get("postIds", [{}])[0]
    post_url = post_data.get("postUrl", "") or post_data.get("url", "")
    if not post_url:
        post_id = post_data.get("id", "")
        post_url = f"https://www.instagram.com/p/{post_id}/" if post_id else "posted"
    return post_url

def send_telegram_notification(country, operator, today, post_url):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    msg = (f"\u2705 *eSIMsData Instagram*\n\n"
           f"\ud83c\udff3\ufe0f {country} | {operator}\n"
           f"\ud83d\udcdd {today}\n"
           f"\ud83d\udd17 {post_url}")
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                  json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}, timeout=15)

def main():
    print("=== eSIMWay Instagram Bot ===")
    day_env = os.environ.get("DAY", "monday").lower()
    template_path = DAY_TEMPLATES.get(day_env, "Monday.png")
    print(f"Day: {day_env} | Template: {template_path}")

    client = get_sheets_client()
    sheet = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
    row_index, row_data = get_next_pending_row(sheet)

    if not row_data:
        print("No pending rows. Exiting.")
        return

    country  = row_data.get("\u0421\u0442\u0440\u0430\u043d\u0430", "")
    operator = row_data.get("\u041e\u043f\u0435\u0440\u0430\u0442\u043e\u0440 (Claude \u043d\u0430\u0445\u043e\u0434\u0438\u0442)", "")
    caption  = row_data.get("\u041f\u043e\u0434\u043f\u0438\u0441\u044c \u043a \u043f\u043e\u0441\u0442\u0443", "")
    hashtags = row_data.get("\u0425\u044d\u0448\u0442\u0435\u0433\u0438", "")
    post_num = row_data.get("#", "")

    print(f"Processing: {country} | Operator: {operator}")
    update_row_status(sheet, row_index, "\u0412 \u043e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0435")

    country_photo = get_country_photo(country)
    operator_logo = get_operator_logo(operator, country)
    image = generate_esim_image(country, country_photo, operator_logo, template_path)
    image_url = upload_to_cloudinary(image)

    full_caption = caption + "\n\n" + hashtags
    post_url = post_to_ayrshare(image_url, full_caption)

    today = datetime.now().strftime("%d.%m.%Y")
    update_row_status(sheet, row_index, "\u041e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u043d\u043e", post_url)
    send_telegram_notification(country, operator, today, post_url)
    print(f"Done! {country} -> {post_url}")

if __name__ == "__main__":
    main()
