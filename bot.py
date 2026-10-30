"""
Oyna / eshik kalkulyatori - Telegram bot
Imkoniyatlar: o'lcham -> panel bo'linishi -> ochilish turi -> brend/rang/oyna
-> chizma -> narx (tannarx, foyda) -> savat -> PDF.

Ishga tushirish:
    pip install -r requirements.txt
    export BOT_TOKEN="123:ABC"          # Windows: set BOT_TOKEN=...
    export ADMIN_IDS="12345678"         # vergul bilan bir nechta
    python bot.py
"""
import asyncio
import io
import json
import logging
import os
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BufferedInputFile, CallbackQuery,
                           InlineKeyboardButton, KeyboardButton, Message,
                           ReplyKeyboardMarkup)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from PIL import Image, ImageDraw, ImageFont

TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
DB_FILE = "bot.db"

# ----------------------------------------------------------------- narxlar
# Hammasini admin /narx orqali o'zgartiradi. Raqamlar - namuna!
DEFAULT_PRICES = {
    "usd_rate": 12700,        # 1$ necha so'm
    "waste_pct": 3,           # chiqindi uchun +%
    "markup_pct": 30,         # sotuv uchun ustama %
    "frame_plastik": 28000,   # so'm / metr  (ramka profili)
    "impost_plastik": 22000,  # so'm / metr  (o'rta profil)
    "sash_plastik": 30000,    # so'm / metr  (kanot profili)
    "frame_alumin": 45000,
    "impost_alumin": 38000,
    "sash_alumin": 48000,
    "glass_bir": 90000,       # so'm / m2
    "glass_ikki": 150000,
    "hw_sash": 250000,        # ochiladigan kanot furnitura (dona)
    "hw_door": 450000,        # eshik furnitura (dona)
    "brand_akfa": 1.0,        # brend koeffitsiyenti
    "brand_alutex": 1.05,
    "brand_retpen": 1.0,
    "brand_alubest": 1.08,
    "brand_ekopen": 0.97,
    "color_oq": 1.0,          # rang koeffitsiyenti
    "color_yogoch": 1.15,
    "color_antrasit": 1.2,
    "color_kulrang": 1.1,
}
BRANDS = {"akfa": "Akfa", "alutex": "Alutex", "retpen": "Retpen",
          "alubest": "Alubest", "ekopen": "Ekopen"}
COLORS = {"oq": ("Oq", (245, 245, 245)),
          "yogoch": ("Yog'och", (176, 96, 16)),
          "antrasit": ("Antrasit", (60, 63, 68)),
          "kulrang": ("Kulrang", (150, 150, 150))}
OPENINGS = {"F": "⬜ Sobit", "L": "◀ Chapga", "R": "▶ O'ngga",
            "T": "🔼 Tepaga (kipp)", "LT": "◀🔼 Chapga+kipp",
            "RT": "▶🔼 O'ngga+kipp"}


# ---------------------------------------------------------------- baza
def db():
    c = sqlite3.connect(DB_FILE)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS prices(key TEXT PRIMARY KEY, value REAL)")
        c.execute("""CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            spec TEXT, qty INTEGER, unit_price REAL, unit_cost REAL)""")
        for k, v in DEFAULT_PRICES.items():
            c.execute("INSERT OR IGNORE INTO prices VALUES(?,?)", (k, v))


def get_prices():
    with db() as c:
        return {r["key"]: r["value"] for r in c.execute("SELECT * FROM prices")}


def money(x):
    return f"{int(round(x)):,}".replace(",", " ") + " so'm"


# -------------------------------------------------------------- hisoblash
def compute(spec, P):
    W, H, top, n = spec["w"], spec["h"], spec["top"], spec["n"]
    mat = spec["material"]
    main_h = H - top
    pw = W / n
    opens = [o for o in spec["opens"] if o != "F"]
    mult = P[f"brand_{spec['brand']}"] * P[f"color_{spec['color']}"]

    frame_m = 2 * (W + H) / 1000
    impost_m = ((n - 1) * main_h + (W if top else 0)) / 1000
    sash_m = len(opens) * 2 * (pw + main_h) / 1000
    glass_m2 = W * H / 1e6
    hw_price = P["hw_door"] if spec["type"] == "eshik" else P["hw_sash"]

    parts = [
        ("Ramka profili", f"{frame_m:.2f} m", frame_m * P[f"frame_{mat}"] * mult),
        ("O'rta profil", f"{impost_m:.2f} m", impost_m * P[f"impost_{mat}"] * mult),
        ("Kanot profili", f"{sash_m:.2f} m", sash_m * P[f"sash_{mat}"] * mult),
        ("Oyna", f"{glass_m2:.2f} m2", glass_m2 * P[f"glass_{spec['glass']}"]),
    ]
    base = sum(p[2] for p in parts)
    waste = base * P["waste_pct"] / 100
    hw = len(opens) * hw_price
    parts.append((f"Chiqindi +{P['waste_pct']:g}%", "", waste))
    parts.append(("Furnitura", f"{len(opens)} dona", hw))
    cost = base + waste + hw
    price = cost * (1 + P["markup_pct"] / 100)
    return {"parts": parts, "cost": cost, "price": price,
            "area": glass_m2}


# ----------------------------------------------------------------- chizma
def font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "C:/Windows/Fonts/arial.ttf",
              "/System/Library/Fonts/Supplemental/Arial.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


def hdim(d, x0, x1, y, text, f):
    d.line([(x0, y), (x1, y)], fill="black", width=2)
    for x in (x0, x1):
        d.line([(x, y - 7), (x, y + 7)], fill="black", width=2)
    tw = d.textlength(text, font=f)
    xm = (x0 + x1) / 2
    d.rectangle([xm - tw / 2 - 3, y - 11, xm + tw / 2 + 3, y + 11], fill="white")
    d.text((xm - tw / 2, y - 10), text, fill="black", font=f)


def vdim(img, x, y0, y1, text, f):
    d = ImageDraw.Draw(img)
    d.line([(x, y0), (x, y1)], fill="black", width=2)
    for y in (y0, y1):
        d.line([(x - 7, y), (x + 7, y)], fill="black", width=2)
    tw = int(d.textlength(text, font=f)) + 8
    t = Image.new("RGBA", (tw, 24), (255, 255, 255, 255))
    ImageDraw.Draw(t).text((4, 2), text, fill="black", font=f)
    t = t.rotate(90, expand=True)
    ym = (y0 + y1) // 2
    img.paste(t, (int(x - 12), int(ym - tw / 2)))


def draw_symbol(d, box, kind):
    x0, y0, x1, y1 = box
    xm, ym = (x0 + x1) / 2, (y0 + y1) / 2
    col = (30, 70, 220)
    if "L" in kind:
        d.line([(x1, y0), (x0, ym), (x1, y1)], fill=col, width=2)
    if "R" in kind:
        d.line([(x0, y0), (x1, ym), (x0, y1)], fill=col, width=2)
    if "T" in kind:
        d.line([(x0, y0), (xm, y1), (x1, y0)], fill=col, width=2)


def draw_window(spec):
    W, H, top, n = spec["w"], spec["h"], spec["top"], spec["n"]
    s = min(520 / W, 520 / H)
    dw, dh = W * s, H * s
    ox, oy = 120, 40
    img = Image.new("RGB", (int(dw) + 170, int(dh) + 170), "white")
    d = ImageDraw.Draw(img)
    f = font(16)
    frame_col = COLORS[spec["color"]][1]
    glass_col = (150, 215, 240)
    t = max(5, int(50 * s))
    h2 = t // 2

    d.rectangle([ox, oy, ox + dw, oy + dh], fill=frame_col, outline="black")
    ty = oy + top * s                      # tepa oynacha chegarasi
    if top:
        d.rectangle([ox + t, oy + t, ox + dw - t, ty - h2], fill=glass_col)
    pw = dw / n
    for i in range(n):
        x0 = ox + i * pw + (t if i == 0 else h2)
        x1 = ox + (i + 1) * pw - (t if i == n - 1 else h2)
        y0 = ty + (h2 if top else t)
        y1 = oy + dh - t
        d.rectangle([x0, y0, x1, y1], fill=glass_col)
        draw_symbol(d, (x0, y0, x1, y1), spec["opens"][i])
    d.rectangle([ox, oy, ox + dw, oy + dh], outline="black", width=2)

    # o'lchamlar
    yb = oy + dh + 28
    pmm = W / n
    for i in range(n):
        hdim(d, ox + i * pw, ox + (i + 1) * pw, yb, f"{pmm:.0f}", f)
    hdim(d, ox, ox + dw, yb + 38, f"{W}", f)
    vdim(img, ox - 35, oy, oy + dh, f"{H}", f)
    if top:
        vdim(img, ox - 75, oy, ty, f"{top}", f)
    return img


def png_bytes(img):
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


# -------------------------------------------------------------------- PDF
def spec_lines(spec):
    return [
        f"Turi: {'Eshik' if spec['type'] == 'eshik' else 'Oyna'}   |   "
        f"Profil: {spec['material'].capitalize()}   |   Brend: {BRANDS[spec['brand']]}",
        f"Rang: {COLORS[spec['color']][0]}   |   "
        f"Oyna: {'Ikki qavat' if spec['glass'] == 'ikki' else 'Bir qavat'}",
        f"O'lcham: {spec['w']} x {spec['h']} mm"
        + (f"   |   Tepa oynacha: {spec['top']} mm" if spec["top"] else ""),
        "Panellar: " + ", ".join(OPENINGS[o].split(" ", 1)[1] for o in spec["opens"]),
    ]


def build_pdf(items, P):
    pages = []
    total = 0
    for idx, it in enumerate(items, 1):
        spec = it["spec"]
        page = Image.new("RGB", (1240, 1754), "white")
        d = ImageDraw.Draw(page)
        d.text((80, 60), "Narx taklifi", fill="black", font=font(44))
        d.text((80, 125), datetime.now().strftime("%d.%m.%Y %H:%M")
               + f"   |   Pozitsiya {idx}/{len(items)}", fill="gray", font=font(24))
        pic = draw_window(spec)
        r = min(1000 / pic.width, 780 / pic.height)
        pic = pic.resize((int(pic.width * r), int(pic.height * r)))
        page.paste(pic, ((1240 - pic.width) // 2, 190))
        y = 190 + pic.height + 40
        for line in spec_lines(spec):
            d.text((80, y), line, fill="black", font=font(26))
            y += 42
        y += 20
        line_total = it["unit_price"] * it["qty"]
        total += line_total
        d.rectangle([80, y, 1160, y + 150], fill=(225, 250, 215))
        d.text((100, y + 15), f"Soni: {it['qty']} dona", fill="black", font=font(28))
        d.text((100, y + 60), f"1 dona: {money(it['unit_price'])}", fill="black", font=font(28))
        d.text((100, y + 105), f"Jami: {money(line_total)}  (${line_total / P['usd_rate']:.2f})",
               fill="black", font=font(30))
        pages.append(page)
    if len(items) > 1:
        page = Image.new("RGB", (1240, 1754), "white")
        d = ImageDraw.Draw(page)
        d.text((80, 60), "Umumiy narx", fill="black", font=font(44))
        y = 160
        for idx, it in enumerate(items, 1):
            sp = it["spec"]
            d.text((80, y), f"{idx}. {sp['w']}x{sp['h']} mm  x{it['qty']}",
                   fill="black", font=font(28))
            t = money(it["unit_price"] * it["qty"])
            d.text((1160 - d.textlength(t, font=font(28)), y), t, fill="black", font=font(28))
            y += 50
        y += 30
        d.text((80, y), f"JAMI: {money(total)}  (${total / P['usd_rate']:.2f})",
               fill="black", font=font(36))
        pages.append(page)
    b = io.BytesIO()
    pages[0].save(b, "PDF", save_all=True, append_images=pages[1:], resolution=150)
    return b.getvalue()


# -------------------------------------------------------------------- bot
router = Router()


class Order(StatesGroup):
    width = State()
    height = State()
    top = State()
    panels = State()
    opening = State()
    material = State()
    brand = State()
    color = State()
    glass = State()
    qty = State()
    confirm = State()


MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="➕ Yangi buyurtma")],
              [KeyboardButton(text="🧾 Buyurtmalarim"), KeyboardButton(text="💲 Narxlar")]],
    resize_keyboard=True)


def ikb(pairs, prefix, width=2):
    b = InlineKeyboardBuilder()
    for label, data in pairs:
        b.button(text=label, callback_data=f"{prefix}:{data}")
    b.adjust(width)
    return b.as_markup()


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Salom! Oyna va eshik narxini hisoblaymiz.", reply_markup=MENU)


@router.message(F.text == "➕ Yangi buyurtma")
async def new_order(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Nimani hisoblaymiz?",
                   reply_markup=ikb([("🪟 Oyna", "oyna"), ("🚪 Eshik", "eshik")], "tp"))


@router.callback_query(F.data.startswith("tp:"))
async def pick_type(c: CallbackQuery, state: FSMContext):
    await state.update_data(type=c.data.split(":")[1])
    await state.set_state(Order.width)
    await c.message.answer("Eni necha mm? (masalan: 2000)")
    await c.answer()


async def num(m: Message, lo, hi):
    try:
        v = int((m.text or "").strip())
        if lo <= v <= hi:
            return v
    except ValueError:
        pass
    await m.answer(f"Iltimos, {lo} dan {hi} gacha butun son yozing.")
    return None


@router.message(Order.width)
async def got_w(m: Message, state: FSMContext):
    v = await num(m, 300, 6000)
    if v is None:
        return
    await state.update_data(w=v)
    await state.set_state(Order.height)
    await m.answer("Balandligi necha mm? (masalan: 1800)")


@router.message(Order.height)
async def got_h(m: Message, state: FSMContext):
    v = await num(m, 300, 4000)
    if v is None:
        return
    await state.update_data(h=v)
    await state.set_state(Order.top)
    await m.answer("Tepada oynacha (fortochka) balandligi necha mm?\nYo'q bo'lsa 0 yozing.")


@router.message(Order.top)
async def got_top(m: Message, state: FSMContext):
    d = await state.get_data()
    v = await num(m, 0, d["h"] // 2)
    if v is None:
        return
    await state.update_data(top=v, opens=[])
    await state.set_state(Order.panels)
    await m.answer("Pastki qism necha bo'lakdan iborat?",
                   reply_markup=ikb([(str(i), str(i)) for i in range(1, 6)], "np", 5))


async def ask_opening(msg: Message, i, n):
    await msg.answer(f"{i}-panel ({i} / {n}) qanday ochiladi?",
                     reply_markup=ikb([(v, k) for k, v in OPENINGS.items()], "op"))


@router.callback_query(Order.panels, F.data.startswith("np:"))
async def pick_panels(c: CallbackQuery, state: FSMContext):
    n = int(c.data.split(":")[1])
    await state.update_data(n=n, opens=[])
    await state.set_state(Order.opening)
    await ask_opening(c.message, 1, n)
    await c.answer()


@router.callback_query(Order.opening, F.data.startswith("op:"))
async def pick_opening(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    opens = d["opens"] + [c.data.split(":")[1]]
    await state.update_data(opens=opens)
    if len(opens) < d["n"]:
        await ask_opening(c.message, len(opens) + 1, d["n"])
    else:
        await state.set_state(Order.material)
        await c.message.answer("Profil turi:", reply_markup=ikb(
            [("Plastik", "plastik"), ("Alumin", "alumin")], "mt"))
    await c.answer()


@router.callback_query(Order.material, F.data.startswith("mt:"))
async def pick_mat(c: CallbackQuery, state: FSMContext):
    await state.update_data(material=c.data.split(":")[1])
    await state.set_state(Order.brand)
    await c.message.answer("Brend:", reply_markup=ikb(
        [(v, k) for k, v in BRANDS.items()], "br", 3))
    await c.answer()


@router.callback_query(Order.brand, F.data.startswith("br:"))
async def pick_brand(c: CallbackQuery, state: FSMContext):
    await state.update_data(brand=c.data.split(":")[1])
    await state.set_state(Order.color)
    await c.message.answer("Rang:", reply_markup=ikb(
        [(v[0], k) for k, v in COLORS.items()], "cl"))
    await c.answer()


@router.callback_query(Order.color, F.data.startswith("cl:"))
async def pick_color(c: CallbackQuery, state: FSMContext):
    await state.update_data(color=c.data.split(":")[1])
    await state.set_state(Order.glass)
    await c.message.answer("Oyna:", reply_markup=ikb(
        [("Bir qavatli", "bir"), ("Ikki qavatli", "ikki")], "gl"))
    await c.answer()


@router.callback_query(Order.glass, F.data.startswith("gl:"))
async def pick_glass(c: CallbackQuery, state: FSMContext):
    await state.update_data(glass=c.data.split(":")[1])
    await state.set_state(Order.qty)
    await c.message.answer("Nechta dona? (masalan: 1)")
    await c.answer()


@router.message(Order.qty)
async def got_qty(m: Message, state: FSMContext):
    q = await num(m, 1, 1000)
    if q is None:
        return
    d = await state.get_data()
    spec = {k: d[k] for k in ("type", "w", "h", "top", "n", "opens",
                              "material", "brand", "color", "glass")}
    P = get_prices()
    r = compute(spec, P)
    await state.update_data(spec=spec, qty=q, unit_price=r["price"], unit_cost=r["cost"])
    cap = "\n".join(spec_lines(spec)) + (
        f"\n\n1 dona: {money(r['price'])} (${r['price'] / P['usd_rate']:.2f})"
        f"\nSoni: {q}"
        f"\nJami: {money(r['price'] * q)} (${r['price'] * q / P['usd_rate']:.2f})")
    if m.from_user.id in ADMIN_IDS:
        cap += (f"\n\n🔒 Tannarx: {money(r['cost'] * q)}"
                f"\n🔒 Foyda: {money((r['price'] - r['cost']) * q)}")
    await state.set_state(Order.confirm)
    await m.answer_photo(BufferedInputFile(png_bytes(draw_window(spec)), "w.png"),
                         caption=cap[:1024],
                         reply_markup=ikb([("✅ Savatga qo'shish", "add"),
                                           ("❌ Bekor", "cancel")], "cf"))


@router.callback_query(Order.confirm, F.data.startswith("cf:"))
async def confirm(c: CallbackQuery, state: FSMContext):
    if c.data.endswith("add"):
        d = await state.get_data()
        with db() as con:
            con.execute("INSERT INTO orders(user_id,spec,qty,unit_price,unit_cost) VALUES(?,?,?,?,?)",
                        (c.from_user.id, json.dumps(d["spec"]), d["qty"],
                         d["unit_price"], d["unit_cost"]))
        await c.message.answer("Savatga qo'shildi ✅\n«🧾 Buyurtmalarim» orqali PDF olishingiz mumkin.")
    else:
        await c.message.answer("Bekor qilindi.")
    await state.clear()
    await c.answer()


# --------------------------------------------------------------- buyurtmalar
def load_items(uid):
    with db() as con:
        rows = con.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id", (uid,)).fetchall()
    return [{"id": r["id"], "spec": json.loads(r["spec"]), "qty": r["qty"],
             "unit_price": r["unit_price"], "unit_cost": r["unit_cost"]} for r in rows]


@router.message(F.text == "🧾 Buyurtmalarim")
async def my_orders(m: Message):
    items = load_items(m.from_user.id)
    if not items:
        await m.answer("Savat bo'sh.")
        return
    P = get_prices()
    total = 0
    lines = []
    b = InlineKeyboardBuilder()
    for i, it in enumerate(items, 1):
        s = it["spec"]
        t = it["unit_price"] * it["qty"]
        total += t
        lines.append(f"{i}. {'Eshik' if s['type'] == 'eshik' else 'Oyna'} {s['w']}x{s['h']} "
                     f"x{it['qty']} = {money(t)}")
        b.button(text=f"❌ {i}", callback_data=f"del:{it['id']}")
    b.adjust(5)
    b.row(*[InlineKeyboardButton(text="📄 PDF olish", callback_data="pdf:all"),
            InlineKeyboardButton(text="🗑 Tozalash", callback_data="clr:all")])
    await m.answer("\n".join(lines) + f"\n\nJami: {money(total)} (${total / P['usd_rate']:.2f})",
                   reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("del:"))
async def del_item(c: CallbackQuery):
    with db() as con:
        con.execute("DELETE FROM orders WHERE id=? AND user_id=?",
                    (int(c.data.split(":")[1]), c.from_user.id))
    await c.message.answer("O'chirildi. «🧾 Buyurtmalarim» ni qayta oching.")
    await c.answer()


@router.callback_query(F.data == "clr:all")
async def clear_all(c: CallbackQuery):
    with db() as con:
        con.execute("DELETE FROM orders WHERE user_id=?", (c.from_user.id,))
    await c.message.answer("Savat tozalandi.")
    await c.answer()


@router.callback_query(F.data == "pdf:all")
async def send_pdf(c: CallbackQuery):
    items = load_items(c.from_user.id)
    if not items:
        await c.answer("Savat bo'sh", show_alert=True)
        return
    await c.answer("PDF tayyorlanmoqda...")
    data = await asyncio.to_thread(build_pdf, items, get_prices())
    await c.message.answer_document(BufferedInputFile(data, "narx_taklifi.pdf"))


# -------------------------------------------------------------------- admin
LABELS = {"usd_rate": "1$ = so'm", "waste_pct": "Chiqindi %", "markup_pct": "Ustama %"}


@router.message(F.text == "💲 Narxlar")
@router.message(Command("narx"))
async def prices_cmd(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        await m.answer("Narxlarni faqat admin ko'ra oladi.")
        return
    parts = (m.text or "").split()
    P = get_prices()
    if len(parts) == 3 and parts[0].startswith("/narx"):
        key = parts[1]
        try:
            val = float(parts[2])
        except ValueError:
            await m.answer("Qiymat son bo'lishi kerak.")
            return
        if key not in P:
            await m.answer("Bunday kalit yo'q. /narx deb ro'yxatni ko'ring.")
            return
        with db() as con:
            con.execute("UPDATE prices SET value=? WHERE key=?", (val, key))
        await m.answer(f"{key} = {val:g} ✅")
        return
    txt = "\n".join(f"{k} = {v:g}" for k, v in P.items())
    await m.answer("Narxlar:\n" + txt + "\n\nO'zgartirish:\n/narx frame_plastik 30000")


async def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN o'rnatilmagan")
    logging.basicConfig(level=logging.INFO)
    init_db()
    bot = Bot(TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
