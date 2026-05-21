"""
칼로리 트래킹 + 다이어트 식단 텔레그램 봇
"""

import os
import json
import base64
import sqlite3
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes
)
import anthropic

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
DAILY_CALORIE_GOAL = 1400
TARGET_DEFICIT_KCAL = 7700

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def init_db():
    conn = sqlite3.connect("calorie_log.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER,
            date      TEXT,
            meal_name TEXT,
            calories  REAL,
            source    TEXT,
            created   TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_meal(user_id, meal_name, calories, source="text"):
    conn = sqlite3.connect("calorie_log.db")
    conn.execute(
        "INSERT INTO logs (user_id, date, meal_name, calories, source, created) VALUES (?,?,?,?,?,?)",
        (user_id, datetime.now().strftime("%Y-%m-%d"), meal_name, calories, source, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def get_daily_total(user_id, date):
    conn = sqlite3.connect("calorie_log.db")
    cur = conn.execute("SELECT COALESCE(SUM(calories),0) FROM logs WHERE user_id=? AND date=?", (user_id, date))
    total = cur.fetchone()[0]
    conn.close()
    return total

def get_today_meals(user_id):
    conn = sqlite3.connect("calorie_log.db")
    cur = conn.execute(
        "SELECT meal_name, calories, source, created FROM logs WHERE user_id=? AND date=? ORDER BY created",
        (user_id, datetime.now().strftime("%Y-%m-%d"))
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def get_average(user_id, days):
    dates = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    conn = sqlite3.connect("calorie_log.db")
    cur = conn.execute(
        f"SELECT COALESCE(AVG(daily),0) FROM (SELECT date, SUM(calories) as daily FROM logs WHERE user_id=? AND date IN ({','.join('?'*len(dates))}) GROUP BY date)",
        [user_id] + dates
    )
    avg = cur.fetchone()[0]
    conn.close()
    return avg

def get_total_deficit(user_id):
    conn = sqlite3.connect("calorie_log.db")
    cur = conn.execute("SELECT date, SUM(calories) FROM logs WHERE user_id=? GROUP BY date", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return sum(max(0, DAILY_CALORIE_GOAL - row[1]) for row in rows)


def analyze_text(text):
    resp = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": (
                f"다음 음식의 칼로리를 분석해줘. "
                f"반드시 JSON 형식으로만 답해줘 (다른 텍스트 없이):\n"
                f'{{"meal_name": "음식명", "calories": 숫자, "note": "간단한 설명"}}\n\n'
                f"음식: {text}"
            )
        }]
    )
    raw = resp.content[0].text.strip().replace("```json","").replace("```","").strip()
    return json.loads(raw)

def analyze_image(image_bytes, mime="image/jpeg"):
    b64 = base64.standard_b64encode(image_bytes).decode()
    resp = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": '이 음식 사진을 보고 칼로리를 분석해줘. 반드시 JSON 형식으로만 답해줘 (다른 텍스트 없이):\n{"meal_name": "음식명", "calories": 숫자, "note": "간단한 설명"}'}
            ]
        }]
    )
    raw = resp.content[0].text.strip().replace("```json","").replace("```","").strip()
    return json.loads(raw)

def suggest_diet(ingredients):
    resp = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": (
                f"냉장고에 있는 재료: {ingredients}\n\n"
                f"이 재료들로 만들 수 있는 다이어트 식단 2~3가지를 제안해줘.\n"
                f"각 레시피마다 음식 이름, 예상 칼로리, 간단한 조리법 3단계, 단백질/탄수화물/지방 비율을 알려줘. 한국어로."
            )
        }]
    )
    return resp.content[0].text


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🥗 *칼로리 트래킹 봇*에 오신 걸 환영해요!\n\n"
        "📸 *사진 전송* → 음식 칼로리 자동 분석\n"
        "📝 *텍스트* → 음식명으로 칼로리 계산\n"
        "🥬 `재료: 달걀, 닭가슴살, 브로콜리` → 식단 제안\n\n"
        "📊 `/today` → 오늘 섭취 현황\n"
        "📈 `/stats` → 3일/1주 평균 + 적자 트래킹"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    today = datetime.now().strftime("%Y-%m-%d")
    total = get_daily_total(uid, today)
    meals = get_today_meals(uid)
    diff = total - DAILY_CALORIE_GOAL
    deficit_total = get_total_deficit(uid)
    pct = min(100, deficit_total / TARGET_DEFICIT_KCAL * 100)

    lines = [f"📅 *오늘 ({today}) 섭취 현황*\n"]
    if meals:
        for name, kcal, src, created in meals:
            icon = "📸" if src == "photo" else "📝"
            time = created[11:16]
            lines.append(f"{icon} {time} {name}: *{kcal:.0f} kcal*")
    else:
        lines.append("아직 기록이 없어요.")

    lines.append(f"\n💰 *총 섭취:* {total:.0f} kcal")
    lines.append(f"🎯 *목표:* {DAILY_CALORIE_GOAL} kcal")
    if diff > 0:
        lines.append(f"⚠️ *초과:* +{diff:.0f} kcal")
    else:
        lines.append(f"✅ *적자:* {abs(diff):.0f} kcal 👍")
    lines.append(f"\n🔥 *1kg 감량 진행률:* {pct:.1f}%")
    lines.append(f"누적 적자 {deficit_total:.0f} / 7,700 kcal")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    avg3 = get_average(uid, 3)
    avg7 = get_average(uid, 7)
    deficit = get_total_deficit(uid)
    pct = deficit / TARGET_DEFICIT_KCAL * 100
    days_left = (TARGET_DEFICIT_KCAL - deficit) / max(1, DAILY_CALORIE_GOAL - avg7)

    msg = (
        f"📈 *칼로리 통계*\n\n"
        f"3일 평균: *{avg3:.0f} kcal*\n"
        f"1주 평균: *{avg7:.0f} kcal*\n"
        f"일일 목표: {DAILY_CALORIE_GOAL} kcal\n\n"
        f"🔥 *1kg 감량 진행률: {pct:.1f}%*\n"
        f"누적 적자: {deficit:.0f} kcal\n"
        f"목표까지: {max(0, TARGET_DEFICIT_KCAL - deficit):.0f} kcal\n"
        f"예상 달성: 약 {max(0, days_left):.0f}일 후"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text("📸 사진 분석 중...")
    photo = update.message.photo[-1]
    file = await ctx.bot.get_file(photo.file_id)
    img = await file.download_as_bytearray()
    try:
        result = analyze_image(bytes(img))
        name = result["meal_name"]
        kcal = float(result["calories"])
        note = result.get("note", "")
        save_meal(uid, name, kcal, source="photo")
        today_total = get_daily_total(uid, datetime.now().strftime("%Y-%m-%d"))
        msg = (f"📸 *{name}*\n칼로리: *{kcal:.0f} kcal*\n_{note}_\n\n📊 오늘 누계: *{today_total:.0f} / {DAILY_CALORIE_GOAL} kcal*")
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"분석 실패: {e}")

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    if text.startswith("재료:") or text.startswith("재료 :"):
        ingredients = text.split(":", 1)[1].strip()
        await update.message.reply_text("🥗 식단 제안 생성 중...")
        suggestion = suggest_diet(ingredients)
        await update.message.reply_text(suggestion)
        return
    await update.message.reply_text("📝 칼로리 분석 중...")
    try:
        result = analyze_text(text)
        name = result["meal_name"]
        kcal = float(result["calories"])
        note = result.get("note", "")
        save_meal(uid, name, kcal, source="text")
        today_total = get_daily_total(uid, datetime.now().strftime("%Y-%m-%d"))
        msg = (f"📝 *{name}*\n칼로리: *{kcal:.0f} kcal*\n_{note}_\n\n📊 오늘 누계: *{today_total:.0f} / {DAILY_CALORIE_GOAL} kcal*")
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"분석 실패: {e}\n음식 이름을 더 구체적으로 적어보세요.")


def main():
    init_db()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("🤖 칼로리 트래킹 봇 시작!")
    app.run_polling()

if __name__ == "__main__":
    main()
