#!/usr/bin/env python3
"""
بوت تيليغرام - أخبار الصحة اليومية بالعربية
--------------------------------------------
يجلب أحدث المقالات الصحية من عدة مصادر RSS عربية،
يحتفظ بمقالات الساعات الأخيرة، ويرسلها عبر تيليغرام.

مصمم للعمل بدون خادم عبر GitHub Actions (جدول يومي)،
لكنه يعمل أيضًا محليًا.

الاستخدام:
    python health_bot.py          # إرسال أخبار اليوم
    python health_bot.py getid    # عرض chat_id الخاص بك (الإعداد الأولي)

متغيرات البيئة:
    TELEGRAM_TOKEN     (إلزامي)   رمز البوت من @BotFather
    TELEGRAM_CHAT_ID   (إلزامي)   معرف المحادثة أو القناة
    MAX_AGE_HOURS      (اختياري)  نافذة المقالات الحديثة، الافتراضي 28
    MAX_ARTICLES       (اختياري)  الحد الأقصى للمقالات المرسلة، الافتراضي 12
"""

import os
import sys
import html
import time
import calendar
import logging
from datetime import datetime, timezone, timedelta

import requests
import feedparser
from groq import Groq

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("health-bot")

# ─────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────

# مصادر RSS للصحة باللغة العربية. يمكنك التعديل بحرية: إضافة، حذف، إعادة ترتيب.
# أي مصدر غير متاح أو معطوب يُتجاهل تلقائيًا (لا يوقف البوت).
FEEDS = [
    ("RT عربي — صحة",          "https://arabic.rt.com/rss/health/"),
    ("CNN عربي — صحة",         "https://arabic.cnn.com/health/rss"),
    ("Google أخبار صحية",      "https://news.google.com/rss/search?q=صحة+طب+علاج&hl=ar&gl=AR&ceid=AR:ar&tbs=qdr:d"),
    ("Google أخبار طبية",      "https://news.google.com/rss/search?q=لقاح+مرض+دراسة+طبية&hl=ar&gl=MA&ceid=MA:ar&tbs=qdr:d"),
]

# كلمات مفتاحية للتحقق من أن المقال يتعلق فعلًا بالصحة
HEALTH_KEYWORDS = {
    "صحة", "طب", "طبي", "مرض", "علاج", "دواء", "أدوية", "مستشفى", "طبيب",
    "وباء", "فيروس", "لقاح", "تطعيم", "جراحة", "سرطان", "قلب", "سكري",
    "ضغط", "تغذية", "حمية", "رياضة", "نفسي", "عقلي", "ذهني", "صيدلية",
    "بكتيريا", "عدوى", "التهاب", "أعراض", "تشخيص", "وقاية", "صحي",
    "كورونا", "كوفيد", "إيدز", "سمنة", "نحافة", "ضغط الدم",
    "كوليسترول", "أنيميا", "الزهايمر", "باركنسون", "ربو", "حساسية",
    "جسم", "دماغ", "عضلات", "عظام", "كبد", "كلى", "رئة", "معدة",
    "نوم", "توتر", "قلق", "اكتئاب", "سمع", "بصر", "أسنان", "جلد",
    "حمل", "ولادة", "رضاعة", "طفل", "شيخوخة", "وزن", "بروتين", "فيتامين",
    "سعرات", "غذاء", "أكل", "شرب", "مياه", "خضروات", "فاكهة",
    "مضاد", "حيوي", "مقاومة", "مناعة", "هرمون", "ضغط دم", "كشف مبكر",
    "حر", "برد", "إجهاد", "حادث", "إسعاف", "طوارئ",
}

MAX_AGE_HOURS   = int(os.getenv("MAX_AGE_HOURS", "28"))
MAX_ARTICLES    = int(os.getenv("MAX_ARTICLES", "8"))
INCLUDE_UNDATED = False

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
USER_AGENT   = "Mozilla/5.0 (compatible; health-telegram-bot/1.0)"

# ─────────────────────────────────────────────────────────────
# RÉCUPÉRATION DES ARTICLES
# ─────────────────────────────────────────────────────────────

def parse_date(entry):
    """Renvoie un datetime UTC à partir de l'entrée RSS, ou None."""
    for attr in ("published_parsed", "updated_parsed"):
        st = entry.get(attr)
        if st:
            # *_parsed est en UTC ; calendar.timegm évite le décalage local
            return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
    return None


def is_health_related(title, summary="", trusted_source=False):
    """يتحقق أن المقال يحتوي على كلمة مفتاحية صحية. يتجاوز الفلتر للمصادر الموثوقة."""
    if trusted_source:
        return True
    text = (title + " " + summary).lower()
    return any(kw in text for kw in HEALTH_KEYWORDS)


def collect_articles():
    """يقرأ جميع المصادر، يصفّي حسب الحداثة والموضوع، يزيل التكرار، يرتّب ويقلّص."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    seen = set()
    articles = []

    for source, url in FEEDS:
        try:
            log.info("قراءة المصدر: %s", source)
            feed = feedparser.parse(url, agent=USER_AGENT)

            if feed.bozo and not feed.entries:
                log.warning("مصدر غير قابل للقراءة (%s): %s", source,
                            getattr(feed, "bozo_exception", "سبب غير معروف"))
                continue

            for e in feed.entries:
                title   = (e.get("title") or "").strip()
                link    = (e.get("link") or "").strip()
                summary = (e.get("summary") or e.get("description") or "").strip()
                if not title or not link:
                    continue

                key = link or title.lower()
                if key in seen:
                    continue

                dt = parse_date(e)
                if dt is None:
                    if not INCLUDE_UNDATED:
                        continue
                elif dt < cutoff:
                    continue

                if not is_health_related(title, summary):
                    log.debug("مقال محذوف (غير صحي): %s", title)
                    continue

                seen.add(key)
                articles.append({"source": source, "title": title, "link": link, "dt": dt, "summary": summary})

        except Exception as ex:  # un flux ne doit jamais faire planter tout le bot
            log.warning("Erreur sur le flux %s : %s", source, ex)
            continue

    # tri par date décroissante (articles sans date relégués en bas)
    floor = datetime.min.replace(tzinfo=timezone.utc)
    articles.sort(key=lambda a: a["dt"] or floor, reverse=True)
    return articles[:MAX_ARTICLES]

# ─────────────────────────────────────────────────────────────
# MISE EN FORME DU MESSAGE
# ─────────────────────────────────────────────────────────────

def summarize_article(title, summary, source):
    """يستخدم Groq لتلخيص المقال في ٢-٣ جمل بالعربية."""
    if not GROQ_API_KEY:
        return None
    try:
        client = Groq(api_key=GROQ_API_KEY)
        prompt = f"""أنت محرر صحي محترف. لخّص هذا المقال الصحي في جملتين أو ثلاث جمل بالعربية الفصحى البسيطة.
اجعل الملخص مفيدًا ومباشرًا. لا تبدأ بـ "الملخص:" أو أي مقدمة.

العنوان: {title}
المصدر: {source}
المحتوى: {summary[:800] if summary else 'غير متوفر'}"""

        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content.strip()
    except Exception as ex:
        log.warning("خطأ في التلخيص: %s", ex)
        return None


def split_message(text, limit=4000):
    """Découpe le texte en blocs <= limite (Telegram plafonne à 4096)."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for block in text.split("\n"):
        if len(current) + len(block) + 1 > limit:
            chunks.append(current)
            current = ""
        current += block + "\n"
    if current.strip():
        chunks.append(current)
    return chunks


def build_messages(articles):
    today = datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y")
    header = f"🩺 <b>أخبار الصحة اليومية — {today}</b>\n"

    if not articles:
        return [header + f"\nلا توجد مقالات جديدة في آخر {MAX_AGE_HOURS} ساعة."]

    lines = [header]
    for i, a in enumerate(articles, 1):
        t = html.escape(a["title"])
        s = html.escape(a["source"])

        summary = summarize_article(a["title"], a.get("summary", ""), a["source"])

        lines.append(f'\n{i}. <a href="{a["link"]}"><b>{t}</b></a>')
        if summary:
            lines.append(f'\n{html.escape(summary)}')
        lines.append(f'\n   — <i>{s}</i>\n')

    return split_message("".join(lines))

# ─────────────────────────────────────────────────────────────
# ENVOI TELEGRAM
# ─────────────────────────────────────────────────────────────

def telegram_call(method, payload, http="post"):
    url = TELEGRAM_API.format(token=TELEGRAM_TOKEN, method=method)
    fn = requests.post if http == "post" else requests.get
    r = fn(url, json=payload if http == "post" else None,
           params=None if http == "post" else payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram a refusé : {data}")
    return data


def send_messages(messages):
    for msg in messages:
        telegram_call("sendMessage", {
            "chat_id": TELEGRAM_CHAT,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        time.sleep(1)  # petit délai pour rester sous les limites d'envoi

# ─────────────────────────────────────────────────────────────
# OUTIL : récupérer son chat_id
# ─────────────────────────────────────────────────────────────

def print_chat_ids():
    data = telegram_call("getUpdates", {}, http="get")
    found = {}
    for u in data.get("result", []):
        chat = (u.get("message") or u.get("channel_post") or {}).get("chat", {})
        if chat:
            label = chat.get("title") or chat.get("username") or chat.get("first_name") or "?"
            found[chat.get("id")] = f"{label} ({chat.get('type')})"
    if found:
        print("\nالمحادثات المكتشفة:")
        for cid, label in found.items():
            print(f"   TELEGRAM_CHAT_ID = {cid}    →  {label}")
    else:
        print("\nلم يُعثر على أي محادثة.")
        print("أرسل أولًا رسالة إلى البوت (أو أضفه إلى المجموعة/القناة)،")
        print("ثم أعد التشغيل: python health_bot.py getid")

# ─────────────────────────────────────────────────────────────
# POINT D'ENTRÉE
# ─────────────────────────────────────────────────────────────

def main():
    if not TELEGRAM_TOKEN:
        log.error("TELEGRAM_TOKEN مفقود. يرجى تعريف متغير البيئة.")
        sys.exit(1)

    if len(sys.argv) > 1 and sys.argv[1] == "getid":
        print_chat_ids()
        return

    if not TELEGRAM_CHAT:
        log.error("TELEGRAM_CHAT_ID مفقود. شغّل أولًا: python health_bot.py getid")
        sys.exit(1)

    articles = collect_articles()
    log.info("%d article(s) retenu(s).", len(articles))
    messages = build_messages(articles)
    send_messages(messages)
    log.info("Envoi terminé (%d message(s)).", len(messages))


if __name__ == "__main__":
    main()
