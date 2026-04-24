import os, json, requests, gspread, time, re, unicodedata
import numpy as np
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from google.oauth2.service_account import Credentials
from datetime import datetime

SITE = "esimsdata.com"
SHEET_NAME = "eSIMData.com"
TEXT_COLOR = (255, 255, 255)
DAY_TEMPLATES = {"monday":"Monday.png","wednesday":"Wednesday.png","thursday":"Thursday.png","friday":"Friday.png"}
PHOTO_BOX = {"x":246,"y":236,"w":557,"h":557}
OPERATOR_BOX = {"x":255,"y":140,"w":300,"h":95}
COUNTRY_TEXT = {"cx":525,"y1":800,"font_size":38}
HEADERS = {"User-Agent":"ESIMBot/1.0"}

def get_sheets_client():
    creds = Credentials.from_service_account_info(json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"])
    return gspread.authorize(creds)

def get_next_pending_row(sheet):
    for i, row in enumerate(sheet.get_all_records()):
        if str(row.get("\u0421\u0442\u0430\u0442\u0443\u0441","")).strip() == "\u041e\u0436\u0438\u0434\u0430\u0435\u0442":
            return i+2, row
    return None, None

def update_row_status(sheet, idx, status, url=""):
    sheet.update_cell(idx, 6, status)
    if url:
        sheet.update_cell(idx, 7, datetime.now().strftime("%d.%m.%Y"))
        sheet.update_cell(idx, 8, url)

def normalize_name(s):
    s = ''.join(c for c in unicodedata.normalize('NFD',s) if unicodedata.category(c)!='Mn')
    return re.sub(r'([aeiouAEIOU])\\1+',r'\\1',s)

def get_country_photo(name):
    key = os.environ.get("UNSPLASH_ACCESS_KEY","")
    if not key: return None
    queries = [name, normalize_name(name)]
    if ' ' in name: queries.append(normalize_name(name.split()[0])+" country")
    for q in queries:
        try:
            r = requests.get("https://api.unsplash.com/search/photos",
                params={"query":f"{q} travel landscape","per_page":3,"orientation":"landscape"},
                headers={"Authorization":f"Client-ID {key}"},timeout=15)
            res = r.json().get("results",[])
            print(f"Unsplash '{q}': {len(res)}")
            if res:
                ir = requests.get(res[0]["urls"]["regular"],timeout=20)
                return Image.open(BytesIO(ir.content)).convert("RGB")
        except Exception as e:
            print(f"Unsplash error: {e}")
    return None

def get_operator_logo(operator, country):
    for q in [f"{operator} logo",f"{operator} {country} telecom"]:
        try:
            r = requests.get("https://commons.wikimedia.org/w/api.php",
                params={"action":"query","generator":"search","gsrsearch":f"File:{q}","gsrnamespace":6,
                        "prop":"imageinfo","iiprop":"url","format":"json"},
                headers=HEADERS,timeout=15)
            for p in r.json().get("query",{}).get("pages",{}).values():
                url = p.get("imageinfo",[{}])[0].get("url","")
                if url and any(url.lower().endswith(x) for x in [".png",".jpg",".svg"]):
                    ir = requests.get(url,timeout=20)
                    return Image.open(BytesIO(ir.content)).convert("RGBA")
        except Exception as e:
            print(f"Logo error: {e}")
    return None

def generate_image(country, photo, logo, template_path):
    tmpl = Image.open(template_path).convert("RGBA")
    res = tmpl.copy()
    pb = PHOTO_BOX
    if photo:
        res.paste(photo.resize((pb["w"],pb["h"]),Image.LANCZOS).convert("RGBA"),(pb["x"],pb["y"]))
    else:
        ImageDraw.Draw(res).rectangle([pb["x"],pb["y"],pb["x"]+pb["w"],pb["y"]+pb["h"]],fill=(240,240,240,255))
    ob = OPERATOR_BOX
    if logo:
        ol = logo.resize((ob["w"],ob["h"]),Image.LANCZOS)
        res.paste(ol,(ob["x"],ob["y"]),ol if ol.mode=="RGBA" else None)
    draw = ImageDraw.Draw(res)
    ct = COUNTRY_TEXT
    try: font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",ct["font_size"])
    except: font = ImageFont.load_default()
    bb = draw.textbbox((0,0),country,font=font)
    draw.text((ct["cx"]-(bb[2]-bb[0])//2,ct["y1"]),country,font=font,fill=TEXT_COLOR)
    return res.convert("RGB")

def upload_to_cloudinary(image):
    import hashlib
    cn,ck,cs = os.environ["CLOUDINARY_CLOUD_NAME"],os.environ["CLOUDINARY_API_KEY"],os.environ["CLOUDINARY_API_SECRET"]
    ts = str(int(time.time()))
    sig = hashlib.sha1(f"timestamp={ts}{cs}".encode()).hexdigest()
    buf = BytesIO(); image.save(buf,format="JPEG",quality=95); buf.seek(0)
    r = requests.post(f"https://api.cloudinary.com/v1_1/{cn}/image/upload",
        data={"api_key":ck,"timestamp":ts,"signature":sig},
        files={"file":("post.jpg",buf,"image/jpeg")},timeout=60)
    url = r.json().get("secure_url","")
    print(f"Cloudinary: {url}")
    return url

def post_to_ayrshare(image_url, caption):
    r = requests.post("https://api.ayrshare.com/api/post",
        headers={"Authorization":f"Bearer {os.environ['AYRSHARE_API_KEY']}","Content-Type":"application/json"},
        json={"post":caption,"platforms":["instagram"],"mediaUrls":[image_url]},timeout=120)
    r.raise_for_status()
    pd = r.json().get("postIds",[{}])[0]
    url = pd.get("postUrl","") or pd.get("url","") or f"https://www.instagram.com/p/{pd.get('id','')}/"
    print(f"Posted: {url}")
    return url

def send_telegram(msg):
    t,c = os.environ.get("TELEGRAM_BOT_TOKEN",""),os.environ.get("TELEGRAM_CHAT_ID","")
    if t and c:
        requests.post(f"https://api.telegram.org/bot{t}/sendMessage",
            json={"chat_id":c,"text":msg,"parse_mode":"Markdown"},timeout=15)

def main():
    day = os.environ.get("DAY","monday").lower()
    tmpl = DAY_TEMPLATES.get(day,"Monday.png")
    print(f"=== {SITE} | {day} | {tmpl} ===")
    client = get_sheets_client()
    sheet = client.open_by_key(os.environ["SPREADSHEET_ID"]).worksheet(SHEET_NAME)
    idx, row = get_next_pending_row(sheet)
    if not row: print("No pending rows."); return
    country  = str(row.get("\u0421\u0442\u0440\u0430\u043d\u0430","")).strip()
    operator = str(row.get("\u041e\u043f\u0435\u0440\u0430\u0442\u043e\u0440 (Claude \u043d\u0430\u0445\u043e\u0434\u0438\u0442)","")).strip()
    caption  = str(row.get("\u041f\u043e\u0434\u043f\u0438\u0441\u044c \u043a \u043f\u043e\u0441\u0442\u0443","")).strip()
    hashtags = str(row.get("\u0425\u044d\u0448\u0442\u0435\u0433\u0438","")).strip()
    num = row.get("#","")
    print(f"#{num}: {country} | {operator}")
    update_row_status(sheet, idx, "\u0412 \u043e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0435")
    photo = get_country_photo(country)
    logo = get_operator_logo(operator, country)
    image = generate_image(country, photo, logo, tmpl)
    img_url = upload_to_cloudinary(image)
    post_url = post_to_ayrshare(img_url, caption+"\n\n"+hashtags)
    update_row_status(sheet, idx, "\u041e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u043d\u043e", post_url)
    send_telegram(f"\u2705 *{SITE} Instagram*\n\n\ud83c\udff3\ufe0f {country} | {operator}\n\ud83d\udcdd #{num}\n\ud83d\udd17 {post_url}")
    print("Done!")

if __name__ == "__main__":
    main()
