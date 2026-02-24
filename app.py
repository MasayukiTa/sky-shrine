"""
気象神社 - 丸沼高原スキー場専用気象ダッシュボード
Gemini API = 文章生成のみ。音声は全てgTTS(ローカル)。
全生成物はcontent.jsonに永続化。
"""
import os, json, time, random, math, hashlib, requests, threading, re
from collections import defaultdict
from datetime import datetime, timezone, timedelta, date as dt_date
from flask import Flask, render_template, redirect, request, render_template_string, jsonify, send_file
from dotenv import load_dotenv
import google.generativeai as genai
from gtts import gTTS
from prebuilt import BASE_OMAMORI, DETAIL_ADVICE, SAISEN_BY_WX, ANGER_FORTUNES

load_dotenv()
app = Flask(__name__)

METEOBLUE_KEY = os.getenv("METEOBLUE_API_KEY")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
LAT = float(os.getenv("LAT", "36.85"))
LON = float(os.getenv("LON", "139.22"))
ALT = int(os.getenv("ALT", "2001"))
LOCATION_NAME = os.getenv("LOCATION_NAME", "丸沼高原スキー場")
PORT = int(os.getenv("PORT", "5000"))
SKI_DATES = ["02/28", "03/01"]

# ===== 生成上限 =====
ORACLE_RATE = 5;     ORACLE_WINDOW = 21600   # 6h毎に5件
ORACLE_CAP = 30                               # 事前分除き30件上限
OMAMORI_RATE = 5;    OMAMORI_WINDOW = 43200  # 12h毎に5件
OMAMORI_CAP = 30                              # 各天候30件上限
SAISEN_RATE = 5;     SAISEN_WINDOW = 43200   # 12h毎に5件
SAISEN_CAP = 30                               # 初期10件除き各天候30件上限
ANGER_RATE = 5;      ANGER_WINDOW = 43200
ANGER_CAP = 30
OMAMORI_HIDE_BASE = 15

genai.configure(api_key=GEMINI_KEY)
gemini_model = genai.GenerativeModel("gemini-2.5-flash")

AI_PROVIDER = os.getenv("AI_PROVIDER", "gemini").lower()
if AI_PROVIDER == "nvidia":
    from openai import OpenAI
    # ===== NVIDIA NIM Clients 分離 =====
    
    # 1. Oracle用 (デフォルト)
    NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")
    nvidia_client = OpenAI(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=NVIDIA_API_KEY
    )
    
    # 2. Saisen (賽銭) 専用
    NVIDIA_API_KEY_SAISEN = os.getenv("NVIDIA_API_KEY_SAISEN")
    nvidia_client_saisen = OpenAI(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=NVIDIA_API_KEY_SAISEN
    )
    
    # 3. Omamori (お守り) 専用
    NVIDIA_API_KEY_OMAMORI = os.getenv("NVIDIA_API_KEY_OMAMORI")
    nvidia_client_omamori = OpenAI(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=NVIDIA_API_KEY_OMAMORI
    )
    
    # 4. Anger (スパム怒り) 専用
    NVIDIA_API_KEY_ANGER = os.getenv("NVIDIA_API_KEY_ANGER")
    nvidia_client_anger = OpenAI(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=NVIDIA_API_KEY_ANGER
    )

    # 5. Oracle パラレル用 (GPT-OSS-120B)
    NVIDIA_API_KEY_ORACLE_GPTOSS = os.getenv("NVIDIA_API_KEY_ORACLE_GPTOSS")
    nvidia_client_oracle_gptoss = OpenAI(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=NVIDIA_API_KEY_ORACLE_GPTOSS
    )

    # 6. Omamori パラレル用 (GPT-OSS-120B)
    NVIDIA_API_KEY_OMAMORI_GPTOSS = os.getenv("NVIDIA_API_KEY_OMAMORI_GPTOSS")
    nvidia_client_omamori_gptoss = OpenAI(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key=NVIDIA_API_KEY_OMAMORI_GPTOSS
    )

BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
AUDIO_DIR = os.path.join(BASE_DIR, "static", "audio")
IMAGES_DIR = os.path.join(BASE_DIR, "static", "images")
SAISEN_DIR = os.path.join(AUDIO_DIR, "saisen")
ANGER_DIR = os.path.join(AUDIO_DIR, "anger")

if AI_PROVIDER == "nvidia":
    CONTENT_FILE = os.path.join(DATA_DIR, "content_nvidia.json")
else:
    CONTENT_FILE = os.path.join(DATA_DIR, "content_gemini.json")

for d in [DATA_DIR, AUDIO_DIR, IMAGES_DIR, SAISEN_DIR, ANGER_DIR]:
    os.makedirs(d, exist_ok=True)

JST = timezone(timedelta(hours=9))
_lock = threading.RLock()

# ===== 永続化 =====
def load_content():
    with _lock:
        try:
            with open(CONTENT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # 古いモノリス型神託プールを破棄 (移行処理)
                if "oracles" in data:
                    del data["oracles"]
                    save_content(data)
                return data
        except: return {}

def save_content(data):
    with _lock:
        try:
            with open(CONTENT_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except: pass

def get_pool(key, window):
    with _lock:
        c = load_content()
        pool = c.get(key, {"items":[],"ts":0,"gen_count":0})
        now = time.time()
        if now - pool.get("ts",0) > window:
            pool = {"items":pool.get("items",[]),"ts":now,"gen_count":0}
            c[key] = pool; save_content(c)
        return pool

def add_item(key, item):
    with _lock:
        c = load_content()
        pool = c.get(key, {"items":[],"ts":time.time(),"gen_count":0})
        pool["items"].append(item)
        pool["gen_count"] = pool.get("gen_count",0) + 1
        c[key] = pool; save_content(c)

def can_gen(key, window, rate, cap):
    pool = get_pool(key, window)
    items = pool.get("items", [])
    if len(items) == 0:
        return True
    if AI_PROVIDER == "nvidia":
        # NVIDIAの場合は一切の制限・上限をかけずに無制限に生成・蓄積する
        return True
    return pool.get("gen_count",0) < rate and len(items) < cap

def generate_ai_text(prompt, client=None, model="deepseek-ai/deepseek-v3.2"):
    if AI_PROVIDER == "nvidia":
        try:
            c = client if client else nvidia_client
            res = c.chat.completions.create(
                model=model,
                messages=[{"role":"user","content":prompt}],
                temperature=0.8,
                top_p=0.95,
                max_tokens=4000
            )
            raw = res.choices[0].message.content.strip()
            # DeepSeek等の「思考トークン (<think>...</think>)」が混入した場合は除去
            if "<think>" in raw and "</think>" in raw:
                raw = raw.split("</think>")[-1].strip()
            return raw
        except Exception as e:
            print(f"NVIDIA API Error ({model}): {e}")
            raise e
    else:
        return gemini_model.generate_content(prompt).text.strip()

# ===== 音声ファイル取得/生成 =====
def ensure_audio(text, subdir="oracle"):
    """テキストのハッシュをファイル名にして保存。既存ならスキップ。"""
    if not text: return None
    h = hashlib.md5(text.encode()).hexdigest()[:12]
    d = os.path.join(AUDIO_DIR, subdir)
    os.makedirs(d, exist_ok=True)
    fname = f"{subdir}_{h}.mp3"
    fpath = os.path.join(d, fname)
    if os.path.exists(fpath):
        return f"/static/audio/{subdir}/{fname}"
    try:
        tts = gTTS(text=text, lang="ja", slow=False)
        tts.save(fpath)
        print(f"  [TTS] 生成完了: {subdir}/{fname}")
        return f"/static/audio/{subdir}/{fname}"
    except Exception as e:
        print(f"  [TTS] gTTSエラー({subdir}): {e}")
        return None

# 排他制御用（同時実行を防ぐ）
active_gens = set()
bg_lock = threading.Lock()

def safe_bg_start(func, key, *args):
    if AI_PROVIDER == "nvidia":
        # NVIDIAの場合は排他制御を無視してリクエストごとにスレッドを立ち上げる（バースト生成のため）
        threading.Thread(target=func, args=args, daemon=True).start()
        return

    with bg_lock:
        if key in active_gens: return
        active_gens.add(key)
    def wrapper():
        try:
            func(*args)
        finally:
            with bg_lock:
                active_gens.discard(key)
    threading.Thread(target=wrapper, daemon=True).start()

# ===== BG生成 (神託・御守り・賽銭) =====
ip_hits = defaultdict(list)
ip_banned = {}

TENBATSU_HTML = '''<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>天罰</title>
<link href="https://fonts.googleapis.com/css2?family=Shippori+Mincho:wght@700&display=swap" rel="stylesheet">
<style>body{margin:0;background:#000;color:#cc0000;display:flex;align-items:center;justify-content:center;
min-height:100vh;font-family:"Shippori Mincho",serif;text-align:center;}
.bolt{font-size:8rem;animation:f .3s infinite;}@keyframes f{0%,100%{opacity:1}50%{opacity:.2}}
h1{font-size:2rem;margin:1rem 0;letter-spacing:.3em;animation:s .1s infinite;}
@keyframes s{0%,100%{transform:translate(0)}25%{transform:translate(-3px,3px)}50%{transform:translate(3px,-3px)}}
p{color:#880000;font-size:.9rem;margin:.5rem 0;}
</style></head><body><div><div class="bolt">⚡</div><h1>天 罰</h1>
<p>神殿への過剰な参拝が検知されました</p>
<p>{{minutes}}分間の参拝禁止</p>
<p style="font-size:.75rem;color:#550000;margin-top:2rem;">反省し、心を落ち着けてから再度参拝されよ</p>
</div></body></html>'''

def check_rate(path):
    if path not in ("/", "/otugekagain"): return None
    ip = request.remote_addr or "unknown"
    now = time.time()
    if ip in ip_banned:
        if now < ip_banned[ip]:
            return render_template_string(TENBATSU_HTML, minutes=int((ip_banned[ip]-now)/60)+1), 429
        del ip_banned[ip]
    ip_hits[ip] = [t for t in ip_hits[ip] if now-t<60]
    ip_hits[ip].append(now)
    if len(ip_hits[ip]) > 15:
        ip_banned[ip] = now + 600; ip_hits[ip] = []
        return render_template_string(TENBATSU_HTML, minutes=10), 429
    return None

# ===== 天文計算 =====
def calc_sun(lat, lon, date_obj):
    n = date_obj.timetuple().tm_yday; B = math.radians(lat)
    d = math.radians(23.45 * math.sin(math.radians(360/365*(n-81))))
    ha = math.degrees(math.acos(max(-1,min(1,-math.tan(B)*math.tan(d)))))
    noon = 12 - lon/15; sr = noon - ha/15 + 9; ss = noon + ha/15 + 9
    return {"sunrise_h":int(sr),"sunrise_m":int((sr%1)*60),"sunset_h":int(ss),"sunset_m":int((ss%1)*60)}

# ===== 天候コード =====
PICTO = {1:"☀️ 快晴",2:"🌤 晴れ",3:"⛅ 曇り時々晴れ",4:"🌥 曇り",5:"🌫 霧",
    6:"🌦 小雨",7:"🌧 雨",8:"⛈ 雷雨",9:"🌧 大雨",10:"🌨 みぞれ",11:"❄️ 雪",12:"🌨 大雪",
    13:"🌩 雷",14:"🌦 雨後曇り",15:"🌨 雪後曇り",16:"🌧 にわか雨",
    17:"🌙 晴れ",18:"🌙 晴れ",19:"⛅ 曇り時々晴れ",20:"🌥 曇り",21:"🌫 霧",22:"🌥 曇り",
    23:"🌧 雨",24:"🌨 雪",25:"🌧 大雨",26:"🌨 みぞれ",27:"❄️ 雪",28:"🌨 大雪",
    29:"🌩 雷",30:"🌧 雨後曇り",31:"🌨 雪後曇り",32:"🌧 にわか雨",33:"🌧 雨",34:"🌨 雪"}
PICTO_CAT = {1:"sun",2:"sun",3:"sun",4:"cloud",5:"cloud",6:"rain",7:"rain",8:"rain",9:"rain",
    10:"snow",11:"snow",12:"snow",13:"rain",14:"rain",15:"snow",16:"rain",
    17:"sun",18:"sun",19:"sun",20:"cloud",21:"cloud",22:"cloud",23:"rain",24:"snow",25:"rain",
    26:"snow",27:"snow",28:"snow",29:"rain",30:"rain",31:"snow",32:"rain",33:"rain",34:"snow"}

ALERT_TEMPLATES = {
    "temp_drop":["寒冷前線通過。急激な温度降下に警戒","気温急降下。防寒装備の再確認を",
        "前線通過で気温急変。低体温症注意","寒気団南下。体感温度に要注意"],
    "extreme_cold":["厳寒警報。凍傷リスク極大。露出部を覆え","極寒。金属への素肌接触で凍傷危険",
        "厳寒。フェイスマスク着用は義務","体感-20℃以下。休憩を頻繁に"],
    "rain":["雨天。防水必須。ゴーグル曇り止め忘れるな","降雨。グローブ予備携帯",
        "雨天。視界悪化注意。速度控えめ","雨天。滑走性低下。ワックスを信じよ"],
    "strong_wind":["強風警報。リフト運休の可能性大","暴風注意。樹林帯コース推奨",
        "強風。体感温度10℃以上低下","風速15m/s超。ゴンドラ運休リスク"],
    "heavy_snow":["大雪警報。視界不良注意","豪雪。整備コース優先",
        "大量降雪。パウダー期待だが視界確保優先","降雪量多。新雪下の地形変化注意"],
    "night_freeze":["夜間道路完全凍結。チェーン推奨","夜間凍結。日没前下山推奨",
        "路面凍結警報。車間距離3倍","帰路凍結に備えよ"],
    "stable":["安定した気象条件。絶好のスキー日和","穏やかな天候。存分に楽しめ",
        "気象条件良好。全コース滑走可能","好天。朝一の圧雪を逃すな",
        "天候安定。上級コースに挑戦も良し","晴天。山頂から絶景堪能せよ"],
    "melt_freeze":["日中融解→夜間再凍結。午後アイスバーン注意","寒暖差大。朝凍結、昼緩む",
        "融解再凍結。午後エッジ効き悪化注意","気温変動大。午前午後で異なる雪面"],
}

weather_cache = {"data":None,"last_updated":None,"error":None,"last_refresh":0}

def get_meteoblue_urls():
    loc = f"{LAT}N{LON}E"
    base = f"https://www.meteoblue.com/ja/weather"
    return {
        "ecmwf": f"{base}/week/{loc}",
        "gfs": f"{base}/14-days/{loc}",
        "meteoblue": f"{base}/meteogramweb/{loc}"
        # meteogram画像はAPIキー保護のためサーバープロキシ(/api/meteogram)を経由する
    }

def fetch_weather():
    params = {"apikey":METEOBLUE_KEY,"lat":LAT,"lon":LON,"asl":ALT,"format":"json","temperature":"C","windspeed":"ms-1","precipitationamount":"mm"}
    r = requests.get("https://my.meteoblue.com/packages/basic-1h", params=params, timeout=15)
    r.raise_for_status(); return r.json()

def wind_dir_text(deg):
    dirs=["北","北北東","北東","東北東","東","東南東","南東","南南東","南","南南西","南西","西南西","西","西北西","北西","北北西"]
    return dirs[round(deg/22.5)%16]

def parse_weather(raw):
    d1h = raw.get("data_1h",{})
    times=d1h.get("time",[]); temps=d1h.get("temperature",[])
    precip=d1h.get("precipitation",[]); ws=d1h.get("windspeed",[])
    wd=d1h.get("winddirection",[]); pc=d1h.get("pictocode",[])
    sun_info = {ds: calc_sun(LAT,LON,dt_date(2026,int(ds.split("/")[0]),int(ds.split("/")[1]))) for ds in SKI_DATES}
    all_h = []
    for i,t in enumerate(times):
        try: dt = datetime.fromisoformat(t).replace(tzinfo=timezone.utc).astimezone(JST)
        except: continue
        p=pc[i] if i<len(pc) else 1; hr=int(dt.strftime("%H")); dk=dt.strftime("%m/%d")
        si=sun_info.get(dk,{"sunrise_h":6,"sunset_h":17})
        sr_h=si["sunrise_h"]; ss_h=si["sunset_h"]
        day_h=ss_h-sr_h; third=day_h//3; m_end=sr_h+third; a_start=ss_h-third
        if hr<sr_h: period="night_before"
        elif hr<m_end: period="morning"
        elif hr<a_start: period="midday"
        elif hr<ss_h: period="afternoon"
        else: period="night_after"
        t_val = round(temps[i], 1) if i < len(temps) else 0
        w_str = PICTO.get(p,"曇り")
        # -4度などで「にわか雨」になるMeteoblueの仕様対策: 0度以下の「雨」は「雪」に強制的におきかえる
        if t_val <= 0 and "雨" in w_str:
            w_str = w_str.replace("雨", "雪")
        all_h.append({"datetime":dt.strftime("%m/%d %H:%M"),"date":dk,"hour":hr,"period":period,
            "temp":round(temps[i],1) if i<len(temps) else None,"precip":round(precip[i],1) if i<len(precip) else 0,
            "wind":round(ws[i],1) if i<len(ws) else None,"wind_dir":wind_dir_text(wd[i]) if i<len(wd) else "不明",
            "weather":w_str,"weather_cat":PICTO_CAT.get(p,"cloud")})
    by_date={}
    for h in all_h: by_date.setdefault(h["date"],[]).append(h)
    ski_hourly={d:by_date.get(d,[]) for d in SKI_DATES}
    ski_detail={}
    for d in SKI_DATES:
        hours=ski_hourly.get(d,[])
        if not hours: continue
        si=sun_info.get(d,{"sunrise_h":6,"sunset_h":17}); sr_h=si["sunrise_h"]; ss_h=si["sunset_h"]
        day_h=ss_h-sr_h; third=day_h//3; m_end=sr_h+third; a_start=ss_h-third
        morning=[h for h in hours if sr_h<=h["hour"]<m_end]
        midday=[h for h in hours if m_end<=h["hour"]<a_start]
        afternoon=[h for h in hours if a_start<=h["hour"]<ss_h]
        def ps(hs,label):
            if not hs: return None
            ts=[h["temp"] for h in hs if h["temp"] is not None]
            if not ts: return None
            weathers=[h["weather"] for h in hs]; dirs=[h["wind_dir"] for h in hs]
            return {"label":label,"temp":round(sum(ts)/len(ts),0),
                "weather":max(set(weathers),key=weathers.count),
                "wind":round(max((h["wind"] or 0) for h in hs),0),
                "wind_dir":max(set(dirs),key=dirs.count)}
        at=[h["temp"] for h in hours if h["temp"] is not None]; tp=sum(h["precip"] for h in hours)
        avg=sum(at)/max(len(at),1); mi=min(at) if at else 0; mx=max(at) if at else 0
        surface=[]
        if tp>5 and avg<0: surface.append("新雪")
        if tp>15 and avg<-2: surface.append("パウダー")
        if mi<-3 and mx>2: surface.append("アイスバーン")
        if avg>3: surface.append("シャーベット")
        if tp<2 and -3<avg<1: surface.append("圧雪")
        if not surface: surface.append("圧雪")
        alerts=[]
        td=at[0]-at[-1] if len(at)>1 else 0
        if td>8: alerts.append(random.choice(ALERT_TEMPLATES["temp_drop"]))
        if mi<-8: alerts.append(random.choice(ALERT_TEMPLATES["extreme_cold"]))
        if mx>5 and tp>0: alerts.append(random.choice(ALERT_TEMPLATES["rain"]))
        if max((h["wind"] or 0) for h in hours)>15: alerts.append(random.choice(ALERT_TEMPLATES["strong_wind"]))
        if tp>10 and avg<0: alerts.append(random.choice(ALERT_TEMPLATES["heavy_snow"]))
        nh=[h for h in hours if h["hour"]>=18]
        if nh and any(h["temp"] is not None and h["temp"]<-5 for h in nh): alerts.append(random.choice(ALERT_TEMPLATES["night_freeze"]))
        if mi<-3 and mx>2 and tp<5: alerts.append(random.choice(ALERT_TEMPLATES["melt_freeze"]))
        if not alerts: alerts.append(random.choice(ALERT_TEMPLATES["stable"]))
        ski_detail[d]={"periods":[x for x in [ps(morning,"朝"),ps(midday,"昼"),ps(afternoon,"夕")] if x],
            "surface":"＋".join(surface),"alert":"。".join(alerts[:2]),"total_precip":round(tp,1),
            "sunrise":f"{si['sunrise_h']}:{si['sunrise_m']:02d}","sunset":f"{si['sunset_h']}:{si['sunset_m']:02d}"}
    probs={}
    for d in SKI_DATES:
        hours=ski_hourly.get(d,[]); n=len(hours)
        if not n: continue
        day=[h for h in hours if 7<=h["hour"]<=17]; nd=max(len(day),1)
        cats=[h["weather_cat"] for h in hours]
        rr=sum(1 for h in day if h["precip"]>0.5 and (h["temp"] or 0)>2)
        at2=[h["temp"] for h in hours if h["temp"] is not None]
        melt=any(t>1 for t in at2[:12]); freeze=any(t<-2 for t in at2[12:])
        ice=65 if(melt and freeze) else(30 if at2 and min(at2)<-3 and max(at2)>0 else 10)
        ls=sum(1 for h in day if(h["wind"] or 0)>15)
        gh=sum(1 for h in day if h["precip"]<3 and(h["wind"] or 0)<12)
        pf=sum(1 for h in day if h["precip"]==0 and(h["wind"] or 0)<3 and -5<=(h["temp"] or 99)<=0)
        probs[d]={"fx_snow":int(cats.count("snow")/n*100),"fx_rain":int(cats.count("rain")/n*100),
            "fx_sun":int(cats.count("sun")/n*100),"fx_cloud":int(cats.count("cloud")/n*100),
            "rain":min(int(rr/nd*100),100),"ice":ice,"lift_stop":min(int(ls/nd*100),100),
            "skiable":min(int(gh/nd*100),100),"clear":min(int(pf/nd*100),100)}
    all_cats=[h["weather_cat"] for d in SKI_DATES for h in ski_hourly.get(d,[])]
    wx_type=max(set(all_cats),key=all_cats.count) if all_cats else "cloud"
    all_temps=[h["temp"] for d in SKI_DATES for h in ski_hourly.get(d,[]) if h["temp"] is not None]
    return {"ski_hourly":ski_hourly,"ski_detail":ski_detail,"probabilities":probs,"sun_info":sun_info,
        "wx_type":wx_type,"avg_temp":sum(all_temps)/max(len(all_temps),1),
        "max_wind":max((h["wind"] or 0) for d in SKI_DATES for h in ski_hourly.get(d,[])),
        "location":LOCATION_NAME}

# ===== 日別判定 (API不使用) =====
def calc_verdict_for_date(data, date):
    hours = [h for h in data.get("ski_hourly",{}).get(date,[]) if 7<=h["hour"]<=17]
    all_t = [h["temp"] for h in hours if h["temp"] is not None]
    all_p = [h["precip"] for h in hours]
    all_w = [h["wind"] for h in hours if h["wind"] is not None]
    if not all_t: return "末吉","すえきち","データ不足"
    at=sum(all_t)/len(all_t); tp=sum(all_p); mw=max(all_w) if all_w else 0
    score=max(0,1-abs(at-(-3))/5)*30+(1-min(tp/30,1))*25+(.2 if tp>5 and at<-1 else 0)*15+(1-min(mw/20,1))*30
    r=[]
    if -5<=at<=-1: r.append(f"気温{at:.0f}℃は粉雪帯")
    elif at<-8: r.append(f"厳寒{at:.0f}℃")
    elif at>3: r.append(f"気温{at:.0f}℃、シャーベット注意")
    if mw>15: r.append(f"風速{mw:.0f}m/s、運休リスク")
    if tp>15 and at>2: r.append("雨天")
    elif tp>10 and at<0: r.append("大雪、パウダー期待")
    reason="。".join(r) if r else "安定した気象条件"
    if score>=75: return "大吉","だいきち",reason
    elif score>=60: return "吉","きち",reason
    elif score>=45: return "半吉","はんきち",reason
    elif score>=30: return "末吉","すえきち",reason
    elif score>=15: return "凶","きょう",reason
    else: return "大凶","だいきょう",reason

def calc_verdict(data):
    """総合判定"""
    all_t,all_p,all_w=[],[],[]
    for d in SKI_DATES:
        for h in [h for h in data.get("ski_hourly",{}).get(d,[]) if 7<=h["hour"]<=17]:
            if h["temp"] is not None: all_t.append(h["temp"])
            all_p.append(h["precip"])
            if h["wind"] is not None: all_w.append(h["wind"])
    if not all_t: return "末吉","すえきち","データ不足"
    at=sum(all_t)/len(all_t); tp=sum(all_p); mw=max(all_w) if all_w else 0
    score=max(0,1-abs(at-(-3))/5)*30+(1-min(tp/30,1))*25+(.2 if tp>5 and at<-1 else 0)*15+(1-min(mw/20,1))*30
    r=[]
    if -5<=at<=-1: r.append(f"気温{at:.0f}℃は粉雪帯")
    elif at<-8: r.append(f"厳寒{at:.0f}℃")
    elif at>3: r.append(f"気温{at:.0f}℃、シャーベット注意")
    if mw>15: r.append(f"風速{mw:.0f}m/s、運休リスク")
    if tp>15 and at>2: r.append("雨天")
    elif tp>10 and at<0: r.append("大雪、パウダー期待")
    reason="。".join(r) if r else "安定した気象条件"
    if score>=75: return "大吉","だいきち",reason
    elif score>=60: return "吉","きち",reason
    elif score>=45: return "半吉","はんきち",reason
    elif score>=30: return "末吉","すえきち",reason
    elif score>=15: return "凶","きょう",reason
    else: return "大凶","だいきょう",reason

# ===== 天候キー =====
def get_wx_keys(data):
    wx=data.get("wx_type","cloud"); at=data.get("avg_temp",0); mw=data.get("max_wind",0)
    keys=["general"]
    if wx=="snow": keys.append("snow")
    if wx=="rain": keys.append("rain")
    if wx=="sun": keys.append("sun")
    if at<-5: keys.append("cold")
    if mw>12: keys.append("wind")
    return keys

def get_detail_key(data):
    wx=data.get("wx_type","cloud"); at=data.get("avg_temp",0); mw=data.get("max_wind",0)
    if wx=="snow": return "snow"
    if wx=="rain": return "rain"
    if wx=="sun": return "sun"
    if at<-5: return "cold"
    if mw>12: return "wind"
    return "stable"

# ===== 御守り選択 =====
def select_omamori(data):
    key = get_detail_key(data)
    base_key = key if key in BASE_OMAMORI else "general"
    base_items = BASE_OMAMORI.get(base_key, [])
    pool_key = f"omamori_{key}"
    gen_pool = get_pool(pool_key, OMAMORI_WINDOW)
    gen_items = gen_pool.get("items", [])
    
    # ユーザー指定の計算ロジック:
    # 生成件数(G)が最大30件まで。不足分を初期ベースから補って、最低15件表示を担保する。
    # Base使用数 = min(10, max(0, 15 - G)) ※初期ベースは通常10件前後
    
    g_count = min(len(gen_items), 30)
    b_count = min(len(base_items), max(0, 15 - g_count))
    
    used_gen = random.sample(gen_items, g_count) if len(gen_items) > g_count else list(gen_items)
    used_base = base_items[:b_count]
    
    all_items = used_base + used_gen
    
    # 重複除去
    seen = set()
    unique = []
    for om in all_items:
        n = om.get("name", "") if isinstance(om, dict) else ""
        if n and n not in seen:
            seen.add(n)
            unique.append(om)
            
    print(f"  [御守り] key={key} base({base_key})={len(used_base)} gen={len(used_gen)} total={len(unique)}")
    return unique

# ===== 詳細アドバイス =====
def get_detail_advice(data):
    key = get_detail_key(data)
    pool = DETAIL_ADVICE.get(key, DETAIL_ADVICE["stable"])
    items = random.sample(pool, min(3, len(pool)))
    return "①" + items[0] + ("②" + items[1] if len(items)>1 else "") + ("③" + items[2] if len(items)>2 else "")

# ===== バックグラウンド生成 =====
def bg_gen_oracle_verdict(verdict, reason):
    pool_key = f"oracle_verdict_{verdict}"
    if not can_gen(pool_key, ORACLE_WINDOW, ORACLE_RATE, ORACLE_CAP): return
    
    def _do_gen(client, model, label):
        try:
            nl = chr(10)
            personas = ["古語を操る厳格で威厳ある神", "威厳があるが少しお茶目でユーモアのある神", "現代人に寄り添う優しく親しみやすい神", "寡黙だが的確な助言をくれる職人肌の神"]
            persona = random.choice(personas)
            prompt = f"気象神社の神主として神託の【前半部分】を1つ生成。3〜4文の文語体。人格・口調は「{persona}」とする。運勢の宣告と、それに基づくスキーヤーへの心構えのみ。場所:{LOCATION_NAME}(標高{ALT}m) 運勢:{verdict}({reason}){nl}テキストのみ返せ。マークダウン不要。※注意：絶対に行動対象は「スキー・スノボ」とし「登山・登攀」の話題は出さないこと。"
            text = generate_ai_text(prompt, client=client, model=model)
            speech = f"神のお告げ。{verdict}にございます。{text}"
            audio = ensure_audio(speech, "oracle")
            add_item(pool_key, {"text":text, "audio":audio})
            print(f"  [BG] 神託(運勢)追加: {verdict} ({label})")
        except Exception as e:
            print(f"  [BG] 神託(運勢)エラー ({label}): {e}")

    if AI_PROVIDER == "nvidia":
        threading.Thread(target=_do_gen, args=(nvidia_client, "deepseek-ai/deepseek-v3.2", "DeepSeek"), daemon=True).start()
        threading.Thread(target=_do_gen, args=(nvidia_client_oracle_gptoss, "openai/gpt-oss-120b", "GPT-OSS"), daemon=True).start()
    else:
        _do_gen(None, "gemini-2.5-flash", "Gemini")

def bg_gen_oracle_weather(data):
    key = get_detail_key(data)
    pool_key = f"oracle_weather_{key}"
    if not can_gen(pool_key, ORACLE_WINDOW, ORACLE_RATE, ORACLE_CAP): return
    
    detail = data.get("ski_detail",{})
    lines = []
    for d,v in detail.items():
        parts = " / ".join(p["label"]+str(int(p["temp"]))+"℃"+p["weather"] for p in v["periods"])
        lines.append(f"{d}: {parts} 雪面:{v['surface']}")
        
    def _do_gen(client, model, label):
        try:
            nl = chr(10)
            personas = ["古語を操る厳格で威厳ある神", "威厳があるが少しお茶目でユーモアのある神", "現代人に寄り添う優しく親しみやすい神", "寡黙だが的確な助言をくれる職人肌の神"]
            persona = random.choice(personas)
            prompt = f"気象神社の神主として神託の【後半部分】を1つ生成。3〜5文の文語体。人格・口調は「{persona}」とする。具体的な気象とスキー場コース状況の解説、それに応じたスキー・スノボ向けの具体的な装備・行動アドバイスのみ。場所:{LOCATION_NAME}(標高{ALT}m) 天候キー:{key}{nl}{nl.join(lines)}{nl}テキストのみ返せ。マークダウン不要。※注意：絶対に行動対象は「スキー・スノボ」とし「登山・登攀・アイゼン」の話題は出さないこと。"
            
            text = generate_ai_text(prompt, client=client, model=model)
            audio = ensure_audio(text, "oracle")
            add_item(pool_key, {"text":text, "audio":audio})
            print(f"  [BG] 神託(天候)追加: {key} ({label})")
        except Exception as e:
            print(f"  [BG] 神託(天候)エラー ({label}): {e}")

    if AI_PROVIDER == "nvidia":
        threading.Thread(target=_do_gen, args=(nvidia_client, "deepseek-ai/deepseek-v3.2", "DeepSeek"), daemon=True).start()
        threading.Thread(target=_do_gen, args=(nvidia_client_oracle_gptoss, "openai/gpt-oss-120b", "GPT-OSS"), daemon=True).start()
    else:
        _do_gen(None, "gemini-2.5-flash", "Gemini")

def bg_gen_omamori(data):
    key = get_detail_key(data); pool_key = f"omamori_{key}"
    if not can_gen(pool_key, OMAMORI_WINDOW, OMAMORI_RATE, OMAMORI_CAP): return
    
    detail = data.get("ski_detail",{})
    wx = []
    for d,v in detail.items():
        parts = " / ".join(p["label"]+str(int(p["temp"]))+"℃"+p["weather"] for p in v["periods"])
        wx.append(f"{d}: {parts} 雪面:{v['surface']}")
        
    def _do_gen(client, model, label):
        try:
            prompt = f"スキー場の気象神社として日々の天候に合わせた「ゲレンデ安全祈願の携帯御守り」を1つ生成。場所:{LOCATION_NAME}(標高{ALT}m) 天候:{'; '.join(wx)} 周辺:日光白根山ロープウェイ、座禅温泉(望郷の湯)、老神温泉、沼田IC50分 JSON1個:{{\"name\":\"3-5字\",\"icon\":\"絵文字\",\"advice\":\"20-40字\",\"detail\":\"15-25字\"}} テキストのみ。※注意：絶対に行動対象は「スキー・スノボ」とし「登山」の話題は出さないこと。"
            
            text = generate_ai_text(prompt, client=client, model=model)
            if text.startswith("```"): text = text.split("```")[1].lstrip("json\n")
            
            try:
                om = json.loads(text)
            except json.JSONDecodeError:
                # 正規表現でJSON部分だけを抜き出すフォールバック
                match = re.search(r'\{.*?\}', text, re.DOTALL)
                if match:
                    om = json.loads(match.group(0))
                else:
                    raise ValueError(f"No JSON found in text: {text}")

            if isinstance(om, list): om = om[0]
            add_item(pool_key, om)
            print(f"  [BG] 御守り追加({key}) ({label})")
        except Exception as e:
            print(f"  [BG] 御守りエラー ({label}): {e}")

    if AI_PROVIDER == "nvidia":
        threading.Thread(target=_do_gen, args=(nvidia_client_omamori, "deepseek-ai/deepseek-v3.2", "DeepSeek"), daemon=True).start()
        threading.Thread(target=_do_gen, args=(nvidia_client_omamori_gptoss, "openai/gpt-oss-120b", "GPT-OSS"), daemon=True).start()
    else:
        _do_gen(None, "gemini-2.5-flash", "Gemini")

def bg_gen_saisen_text(data):
    key = get_detail_key(data); pool_key = f"saisen_text_{key}"
    if not can_gen(pool_key, SAISEN_WINDOW, SAISEN_RATE, SAISEN_CAP): return
    prompt = f"スキー場の賽銭箱に投げ銭した時の短いお告げを1つ生成。20文字以内。文語体で面白く。天候:{key} テキストのみ。"
    
    try:
        c = nvidia_client_saisen if AI_PROVIDER == "nvidia" else None
        # 「これだけモデル変える」というご要望に応じて、必要ならモデル名を変更可能
        text = generate_ai_text(prompt, client=c, model="nvidia/llama-3.3-nemotron-super-49b-v1.5").strip('"').strip("「」")
        audio = ensure_audio(text, "saisen_gen")
        add_item(pool_key, {"text":text, "audio":audio})
        print(f"  [BG] 賽銭文言追加({key})")
    except Exception as e:
        print(f"  [BG] 賽銭エラー: {e}")

def bg_gen_anger():
    pool_key = "saisen_anger"
    if not can_gen(pool_key, ANGER_WINDOW, ANGER_RATE, ANGER_CAP): return
    prompt = f"スキー場の気象神社にて、賽銭を何度も連続で投げ込むスパム行為をする不届き者への「面白く怒る神託」を生成。文語体で50文字〜70文字程度。短すぎず長すぎず。『連打によるサーバー負荷』等のメタなシステム事情への嘆きやツッコミを神様目線の言い回し（〜じゃ、〜でおじゃる、〜であるぞ等）で入れて面白くして。テキストのみ。"
    
    try:
        c = nvidia_client_anger if AI_PROVIDER == "nvidia" else None
        text = generate_ai_text(prompt, client=c, model="openai/gpt-oss-120b").strip('"').strip("「」")
        audio = ensure_audio(text, "anger")
        add_item(pool_key, {"text":text, "audio":audio})
        print(f"  [BG] 怒り文言追加")
    except Exception as e:
        print(f"  [BG] 怒り文言エラー: {e}")

# ===== 起動時事前生成 =====
def startup_gen_audio():
    """お賽銭+怒り音声を事前生成(gTTS, Gemini不使用)"""
    print("  お賽銭音声生成中...")
    for wx_key, fortunes in SAISEN_BY_WX.items():
        for text in fortunes:
            ensure_audio(text, f"saisen_{wx_key}")
    print("  お賽銭音声完了")
    print("  怒り音声生成中...")
    for i, text in enumerate(ANGER_FORTUNES):
        ensure_audio(text, "anger")
    print("  怒り音声完了")

def update_weather():
    try:
        print(f"[{datetime.now(JST).strftime('%H:%M:%S')}] 天候取得中...")
        raw = fetch_weather(); parsed = parse_weather(raw)
        weather_cache["data"] = parsed
        weather_cache["last_updated"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
        weather_cache["error"] = None
    except Exception as e:
        weather_cache["error"] = str(e); print(f"  エラー: {e}")

def bg_updater():
    while True:
        now=datetime.now(JST); mins=now.minute
        wait=((30-mins) if mins<30 else(60-mins))*60-now.second
        time.sleep(max(wait,60)); update_weather()

@app.before_request
def before_req():
    ban = check_rate(request.path)
    if ban: return ban

@app.route("/")
def index():
    data = weather_cache.get("data")
    if not data:
        return render_template_string('''<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="5">
<title>気象神社</title><style>body{background:#1a0505;color:#E8D5A3;display:flex;align-items:center;
justify-content:center;min-height:100vh;font-family:serif;text-align:center;}
.s{font-size:4rem;animation:r 2s linear infinite;}@keyframes r{to{transform:rotate(360deg)}}
</style></head><body><div><div class="s">⛩</div><p style="margin-top:1rem;letter-spacing:.3em;">神殿を準備中…</p></div></body></html>''')

    v, r2, reason = calc_verdict(data)
    # 日別判定
    verdicts_by_date = {}
    for d in SKI_DATES:
        vd, rd, rsn = calc_verdict_for_date(data, d)
        verdicts_by_date[d] = {"verdict":vd,"reading":rd,"reason":rsn}

    # 神託取得 (表示中のもの+音声)
    oracle_text = "天の声をお待ちください…次回アクセス時にお届けします"
    oracle_audio_verdict = None
    oracle_audio_weather = None
    
    wx_key = get_detail_key(data)
    pool_v = get_pool(f"oracle_verdict_{v}", ORACLE_WINDOW)
    pool_w = get_pool(f"oracle_weather_{wx_key}", ORACLE_WINDOW)
    items_v = pool_v.get("items",[])
    items_w = pool_w.get("items",[])
    
    if items_v and items_w:
        cv = random.choice(items_v)
        cw = random.choice(items_w)
        tv = cv["text"] if isinstance(cv, dict) else cv
        tw = cw["text"] if isinstance(cw, dict) else cw
        oracle_text = f"{tv} {tw}"
        oracle_audio_verdict = cv.get("audio") if isinstance(cv, dict) else None
        oracle_audio_weather = cw.get("audio") if isinstance(cw, dict) else None

    omamori = select_omamori(data)
    omamori_count = len(omamori)
    detail_advice = get_detail_advice(data)

    # 次回分BG生成 (排他制御かつ条件クリア時のみスレッド起動)
    if can_gen(f"oracle_verdict_{v}", ORACLE_WINDOW, ORACLE_RATE, ORACLE_CAP):
        safe_bg_start(bg_gen_oracle_verdict, f"oracle_verdict_{v}", v, reason)
    if can_gen(f"oracle_weather_{wx_key}", ORACLE_WINDOW, ORACLE_RATE, ORACLE_CAP):
        safe_bg_start(bg_gen_oracle_weather, f"oracle_weather_{wx_key}", data)
    
    om_key = wx_key
    if can_gen(f"omamori_{om_key}", OMAMORI_WINDOW, OMAMORI_RATE, OMAMORI_CAP):
        safe_bg_start(bg_gen_omamori, f"omamori_{om_key}", data)

    return render_template("index.html",
        location=LOCATION_NAME, data=data,
        oracle={"verdict":v,"verdict_reading":r2,"reason":reason},
        verdicts_by_date=verdicts_by_date,
        oracle_text=oracle_text,
        oracle_audio_verdict=oracle_audio_verdict,
        oracle_audio_weather=oracle_audio_weather,
        detail_advice=detail_advice,
        probabilities=data.get("probabilities",{}),
        omamori=omamori, omamori_count=omamori_count,
        last_updated=weather_cache["last_updated"],
        error=weather_cache["error"], ski_dates=SKI_DATES,
        meteoblue_urls=get_meteoblue_urls())

@app.route("/otugekagain")
def refresh():
    return redirect("/")

last_played_saisen = None

@app.route("/api/saisen", methods=["POST"])
def saisen():
    global last_played_saisen
    data = weather_cache.get("data",{})
    key = get_detail_key(data) if data else "general"
    # 事前生成分
    base = SAISEN_BY_WX.get(key, SAISEN_BY_WX["general"])
    gen_pool = get_pool(f"saisen_text_{key}", SAISEN_WINDOW)
    gen_items = gen_pool.get("items",[])
    
    # 直前のテキストを除外するフィルタ関数
    def get_text(item): return item["text"] if isinstance(item, dict) else item
    
    # 候補のフィルタリング
    valid_gen = [x for x in gen_items if get_text(x) != last_played_saisen] if gen_items else []
    valid_base = [x for x in base if x != last_played_saisen]
    
    # もし除外によって空になった場合は、除外前のリストを使う（回避策）
    if gen_items and not valid_gen: valid_gen = gen_items
    if not valid_base: valid_base = base

    # ランダム選択
    if valid_gen and random.random() < 0.4:
        item = random.choice(valid_gen)
        text = get_text(item)
        audio = item.get("audio") if isinstance(item,dict) else None
    else:
        text = random.choice(valid_base)
        audio = ensure_audio(text, f"saisen_{key}")
        
    last_played_saisen = text
        
    # BG追加生成
    if data and can_gen(f"saisen_text_{key}", SAISEN_WINDOW, SAISEN_RATE, SAISEN_CAP):
        safe_bg_start(bg_gen_saisen_text, f"saisen_text_{key}", data)
        
    return jsonify({"text":text,"audio":audio})

@app.route("/api/meteogram/<type_id>")
def proxy_meteogram(type_id):
    if type_id not in ("7d", "14d"): return "Invalid Request", 400
    fname = f"meteogram_{type_id}.png"
    fpath = os.path.join(IMAGES_DIR, fname)
    fetch_needed = True
    if os.path.exists(fpath):
        mtime = os.path.getmtime(fpath)
        if time.time() - mtime < 21600:
            fetch_needed = False
    
    if fetch_needed:
        t_param = "meteogram_web_hd" if type_id == "7d" else "meteogram_14day_hd"
        url = f"https://my.meteoblue.com/visimage/{t_param}?apikey={METEOBLUE_KEY}&lat={LAT}&lon={LON}&asl={ALT}&temperature_unit=C&windspeed_unit=km%252Fh&lang=ja"
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            with open(fpath, "wb") as f:
                f.write(r.content)
        except Exception as e:
            print(f"Meteogram fetch error: {e}")
            if not os.path.exists(fpath):
                from flask import abort
                abort(404)
    return send_file(fpath, mimetype="image/png")

@app.route("/api/saisen_anger", methods=["POST"])
def saisen_anger():
    # 生成済み怒りボイスのプール取得
    pool_key = "saisen_anger"
    gen_pool = get_pool(pool_key, ANGER_WINDOW)
    gen_items = gen_pool.get("items", [])
    
    if gen_items and random.random() < 0.7:
        item = random.choice(gen_items)
        text = item["text"] if isinstance(item, dict) else item
        audio = item.get("audio") if isinstance(item, dict) else None
    else:
        text = random.choice(ANGER_FORTUNES)
        audio = ensure_audio(text, "anger")
        
    # BG追加生成
    if can_gen(pool_key, ANGER_WINDOW, ANGER_RATE, ANGER_CAP):
        safe_bg_start(bg_gen_anger, pool_key)
        
    return jsonify({"text":text,"audio":audio})

@app.route("/health")
def health():
    return {"status":"ok","last_updated":weather_cache["last_updated"]}

if __name__ == "__main__":
    print("⛩ 気象神社 起動中...")
    print(f"対象: {LOCATION_NAME} ({LAT},{LON})")
    threading.Thread(target=startup_gen_audio, daemon=True).start()
    update_weather()
    threading.Thread(target=bg_updater, daemon=True).start()
    print(f"⛩ http://localhost:{PORT} で参拝受付中")
    app.run(host="0.0.0.0", port=PORT, debug=False)
