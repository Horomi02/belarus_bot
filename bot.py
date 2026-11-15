import asyncio
import math
import os
import re
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from geo_by import init_db, find_belarus_city, calc_distance_km
from tariffs_by import (
    TOP_CITIES,
    PACKAGING_OPTIONS,
    VOLUMETRIC_DENSITY,
    MIN_DELIVERY_PRICE,
    BASE_TERMINAL_FEE,
    RATE_PER_KG_100KM,
    INSURANCE_RATE,
    MIN_INSURANCE_FEE,
    DOOR_SERVICE_PRICE,
)

# 1. Вставьте сюда токен от @BotFather
BOT_TOKEN = os.getenv("BOT_TOKEN", "9020135969:AAFRkInsAmdEm_Z5cKNsQt3nBh8gnpw5igg")

# 2. (Необязательно) Ваш Telegram ID или ID группы менеджеров, куда будут падать заявки.
# Узнать свой ID можно в боте @userinfobot
MANAGER_CHAT_ID = os.getenv("MANAGER_CHAT_ID", "1073832555")

router = Router()


# Шаги по очереди
class OrderSteps(StatesGroup):
    origin = State()
    destination = State()
    weight = State()
    volume = State()
    packaging = State()
    insurance = State()
    door = State()
    phone = State()


# --- КЛАВИАТУРЫ ---
def cities_kb(prefix: str, include_back: bool = False):
    kb = InlineKeyboardBuilder()
    for city in TOP_CITIES:
        kb.button(text=city, callback_data=f"{prefix}:{city}")
    kb.adjust(2)
    if include_back:
        kb.row()
        kb.button(text="⬅️ Назад", callback_data="back:origin")
    return kb.as_markup()


def weight_kb():
    kb = InlineKeyboardBuilder()
    for w in [2, 5, 10, 25, 50, 100]:
        kb.button(text=f"до {w} кг", callback_data=f"w:{w}")
    kb.adjust(3)
    kb.row()
    kb.button(text="⬅️ Назад", callback_data="back:destination")
    return kb.as_markup()


def volume_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="Пакет / мал. короб (0.03 м³)", callback_data="v:0.03")
    kb.button(text="Средняя коробка (0.1 м³)", callback_data="v:0.1")
    kb.button(text="Большая коробка (0.3 м³)", callback_data="v:0.3")
    kb.button(text="Европаллета (1.2 м³)", callback_data="v:1.2")
    kb.adjust(1)
    kb.row()
    kb.button(text="⬅️ Назад", callback_data="back:weight")
    return kb.as_markup()


def packaging_kb():
    kb = InlineKeyboardBuilder()
    for code, opt in PACKAGING_OPTIONS.items():
        price_hint = "0 BYN" if opt.min_price == 0 else f"от {opt.min_price:.0f} BYN"
        kb.button(text=f"{opt.title} ({price_hint})", callback_data=f"p:{code}")
    kb.adjust(1)
    kb.row()
    kb.button(text="⬅️ Назад", callback_data="back:volume")
    return kb.as_markup()


def insurance_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="Без страховки (0 BYN)", callback_data="i:0")
    kb.button(text="Оценка 500 BYN", callback_data="i:500")
    kb.button(text="Оценка 2 000 BYN", callback_data="i:2000")
    kb.button(text="Оценка 5 000 BYN", callback_data="i:5000")
    kb.adjust(2)
    kb.row()
    kb.button(text="⬅️ Назад", callback_data="back:packaging")
    return kb.as_markup()


def door_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🏢 Склад → Склад (дешевле всего)", callback_data="d:0:0")
    kb.button(text="🚚 От двери → На склад", callback_data="d:1:0")
    kb.button(text="🚚 Со склада → До двери", callback_data="d:0:1")
    kb.button(text="🏠 От двери → До двери", callback_data="d:1:1")
    kb.adjust(1)
    kb.row()
    kb.button(text="⬅️ Назад", callback_data="back:insurance")
    return kb.as_markup()


# --- МАТЕМАТИКА РАСЧЁТА ДЛЯ БЕЛАРУСИ ---
def calculate_belarus_quote(data: dict) -> dict:
    pack = PACKAGING_OPTIONS[data["packaging_code"]]
    distance_km = data["distance_km"]
    weight_kg = data["weight_kg"]
    volume_m3 = data["volume_m3"]
    declared_value = data["declared_value"]

    # 1. Объём с учётом упаковки
    effective_vol = round(volume_m3 * pack.volume_multiplier, 3)

    # 2. Расчётный вес: максимум из физического и объёмного
    chargeable_weight = round(max(weight_kg, effective_vol * VOLUMETRIC_DENSITY), 1)

    # 3. Базовая перевозка
    dist_units = max(0.5, distance_km / 100.0)
    discount = 0.85 if chargeable_weight >= 300 else (0.93 if chargeable_weight >= 80 else 1.0)
    raw_delivery = BASE_TERMINAL_FEE + (chargeable_weight * dist_units * RATE_PER_KG_100KM * discount)
    delivery_price = round(max(MIN_DELIVERY_PRICE, raw_delivery), 2)

    # 4. Упаковка
    if pack.code == "none":
        packaging_price = 0.0
    else:
        packaging_price = round(max(pack.min_price, volume_m3 * pack.price_per_m3), 2)

    # 5. Страховка
    if declared_value > 0:
        insurance_price = round(max(MIN_INSURANCE_FEE, declared_value * INSURANCE_RATE), 2)
    else:
        insurance_price = 0.0

    # 6. Забор и доставка по городу
    door_price = (DOOR_SERVICE_PRICE if data["door_pickup"] else 0.0) + (
        DOOR_SERVICE_PRICE if data["door_delivery"] else 0.0
    )

    # 7. Сроки по Беларуси (обычно 1-3 рабочих дня)
    if distance_km <= 80:
        days_str = "1 рабочий день"
    elif distance_km <= 350:
        days_str = "1–2 рабочих дня"
    else:
        days_str = "2–3 рабочих дня"

    total = round(delivery_price + packaging_price + insurance_price + door_price, 2)

    return {
        "pack_title": pack.title,
        "chargeable_weight": chargeable_weight,
        "delivery_price": delivery_price,
        "packaging_price": packaging_price,
        "insurance_price": insurance_price,
        "door_price": door_price,
        "total": total,
        "days_str": days_str,
    }


# --- СТАРТ И ШАГ 1: ОТКУДА ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(OrderSteps.origin)
    await message.answer(
        "👋 Здравствуйте! Я помогу рассчитать стоимость доставки по <b>Беларуси</b>.\n\n"
        "<b>Шаг 1 из 7.</b> Выберите <b>город отправления</b> или напишите его вручную (например: <i>Полоцк</i>):",
        reply_markup=cities_kb("orig"),
    )


@router.callback_query(OrderSteps.origin, F.data.startswith("orig:"))
async def step_origin_cb(callback: CallbackQuery, state: FSMContext):
    city_name = callback.data.split(":", 1)[1]
    found = await find_belarus_city(city_name)
    await state.update_data(origin=found[0], orig_coords=found[1])
    await state.set_state(OrderSteps.destination)
    await callback.message.edit_text(
        f"✅ Отправление: <b>{found[0]}</b>\n\n"
        f"<b>Шаг 2 из 7.</b> Выберите или напишите <b>город получения</b>:",
        reply_markup=cities_kb("dest", include_back=True),
    )
    await callback.answer()


@router.message(OrderSteps.origin, F.text)
async def step_origin_msg(message: Message, state: FSMContext):
    found = await find_belarus_city(message.text)
    if not found:
        await message.answer("Не нашёл такой населённый пункт в Беларуси. Попробуйте написать точнее (например: <i>Лида</i>):")
        return
    await state.update_data(origin=found[0], orig_coords=found[1])
    await state.set_state(OrderSteps.destination)
    await message.answer(
        f"✅ Отправление: <b>{found[0]}</b>\n\n"
        f"<b>Шаг 2 из 7.</b> Выберите или напишите <b>город получения</b>:",
        reply_markup=cities_kb("dest", include_back=True),
    )


# --- ШАГ 2: КУДА ---
@router.callback_query(OrderSteps.destination, F.data.startswith("dest:"))
async def step_dest_cb(callback: CallbackQuery, state: FSMContext):
    city_name = callback.data.split(":", 1)[1]
    await handle_destination(callback.message, state, city_name, edit=True)
    await callback.answer()


@router.message(OrderSteps.destination, F.text)
async def step_dest_msg(message: Message, state: FSMContext):
    await handle_destination(message, state, message.text, edit=False)


async def handle_destination(msg: Message, state: FSMContext, city_raw: str, edit: bool):
    found = await find_belarus_city(city_raw)
    if not found:
        await msg.answer("Не нашёл такой город в Беларуси. Напишите, пожалуйста, ещё раз:")
        return

    data = await state.get_data()
    dist = await calc_distance_km(data["orig_coords"], found[1])
    await state.update_data(destination=found[0], distance_km=dist)
    await state.set_state(OrderSteps.weight)

    text = (
        f"✅ Маршрут: <b>{data['origin']} → {found[0]}</b> (~{dist} км)\n\n"
        f"<b>Шаг 3 из 7.</b> Выберите <b>вес груза</b> или введите точное число в кг (например: <code>14.5</code>):"
    )
    if edit:
        await msg.edit_text(text, reply_markup=weight_kb())
    else:
        await msg.answer(text, reply_markup=weight_kb())


# --- ШАГ 3: ВЕС ---
@router.callback_query(OrderSteps.weight, F.data.startswith("w:"))
async def step_weight_cb(callback: CallbackQuery, state: FSMContext):
    w = float(callback.data.split(":")[1])
    await state.update_data(weight_kg=w)
    await state.set_state(OrderSteps.volume)
    await callback.message.edit_text(
        f"✅ Вес: <b>{w} кг</b>\n\n"
        f"<b>Шаг 4 из 7.</b> Выберите размер груза или напишите <b>габариты в см</b> (например: <code>50х40х30</code>):",
        reply_markup=volume_kb(),
    )
    await callback.answer()


@router.message(OrderSteps.weight, F.text)
async def step_weight_msg(message: Message, state: FSMContext):
    try:
        w = float(message.text.lower().replace(",", ".").replace("кг", "").strip())
        if w <= 0 or w > 20000:
            raise ValueError
    except ValueError:
        await message.answer("Введите вес числом от 0.1 до 20000 (например: <code>12</code>):")
        return

    await state.update_data(weight_kg=w)
    await state.set_state(OrderSteps.volume)
    await message.answer(
        f"✅ Вес: <b>{w} кг</b>\n\n"
        f"<b>Шаг 4 из 7.</b> Выберите размер груза или напишите <b>габариты в см</b> (например: <code>50х40х30</code>):",
        reply_markup=volume_kb(),
    )


# --- ШАГ 4: ГАБАРИТЫ / ОБЪЁМ ---
@router.callback_query(OrderSteps.volume, F.data.startswith("v:"))
async def step_volume_cb(callback: CallbackQuery, state: FSMContext):
    vol = float(callback.data.split(":")[1])
    await state.update_data(volume_m3=vol)
    await state.set_state(OrderSteps.packaging)
    await callback.message.edit_text(
        f"✅ Объём: <b>{vol} м³</b>\n\n"
        f"<b>Шаг 5 из 7.</b> Выберите тип <b>упаковки</b>:",
        reply_markup=packaging_kb(),
    )
    await callback.answer()


@router.message(OrderSteps.volume, F.text)
async def step_volume_msg(message: Message, state: FSMContext):
    text = message.text.lower().replace(",", ".")
    dims = re.search(r"(\d+(?:\.\d+)?)\s*[xх*×]\s*(\d+(?:\.\d+)?)\s*[xх*×]\s*(\d+(?:\.\d+)?)", text)
    if dims:
        l, w, h = map(float, dims.groups())
        vol = round((l * w * h) / 1_000_000, 3)
    else:
        try:
            vol = float(re.sub(r"[^\d.]", "", text))
        except ValueError:
            await message.answer("Пример ввода: <code>60х40х30</code> (в сантиметрах) или <code>0.15</code> (в м³).")
            return

    if vol <= 0:
        await message.answer("Объём должен быть больше нуля.")
        return

    await state.update_data(volume_m3=vol)
    await state.set_state(OrderSteps.packaging)
    await message.answer(
        f"✅ Объём: <b>{vol} м³</b>\n\n"
        f"<b>Шаг 5 из 7.</b> Выберите тип <b>упаковки</b>:",
        reply_markup=packaging_kb(),
    )


# --- ШАГ 5: УПАКОВКА ---
@router.callback_query(OrderSteps.packaging, F.data.startswith("p:"))
async def step_packaging_cb(callback: CallbackQuery, state: FSMContext):
    code = callback.data.split(":")[1]
    await state.update_data(packaging_code=code)
    await state.set_state(OrderSteps.insurance)
    await callback.message.edit_text(
        f"✅ Упаковка: <b>{PACKAGING_OPTIONS[code].title}</b>\n\n"
        f"<b>Шаг 6 из 7.</b> Нужна ли <b>страховка</b>? Выберите ценность груза или напишите сумму в BYN:",
        reply_markup=insurance_kb(),
    )
    await callback.answer()


# --- ШАГ 6: СТРАХОВКА ---
@router.callback_query(OrderSteps.insurance, F.data.startswith("i:"))
async def step_insurance_cb(callback: CallbackQuery, state: FSMContext):
    val = float(callback.data.split(":")[1])
    await state.update_data(declared_value=val)
    await state.set_state(OrderSteps.door)
    await callback.message.edit_text(
        "<b>Шаг 7 из 7.</b> Выберите удобную <b>схему доставки</b>:",
        reply_markup=door_kb(),
    )
    await callback.answer()


@router.message(OrderSteps.insurance, F.text)
async def step_insurance_msg(message: Message, state: FSMContext):
    try:
        val = float(re.sub(r"[^\d.]", "", message.text.replace(",", ".")))
    except ValueError:
        await message.answer("Введите сумму в BYN числом (например: <code>1500</code>) или <code>0</code>.")
        return

    await state.update_data(declared_value=val)
    await state.set_state(OrderSteps.door)
    await message.answer(
        "<b>Шаг 7 из 7.</b> Выберите удобную <b>схему доставки</b>:",
        reply_markup=door_kb(),
    )


# --- ШАГ 7: СХЕМА ДОСТАВКИ И ИТОГОВЫЙ РАСЧЁТ ---
@router.callback_query(OrderSteps.door, F.data.startswith("d:"))
async def step_door_cb(callback: CallbackQuery, state: FSMContext):
    _, pickup, delivery = callback.data.split(":")
    await state.update_data(door_pickup=(pickup == "1"), door_delivery=(delivery == "1"))

    data = await state.get_data()
    q = calculate_belarus_quote(data)

    door_line = f"\n🚚 Забор / доставка до двери: <b>{q['door_price']:.2f} BYN</b>" if q["door_price"] > 0 else ""

    result_text = (
        f"📦 <b>Предварительный расчёт доставки (Беларусь)</b>\n\n"
        f"📍 Маршрут: <b>{data['origin']} → {data['destination']}</b> (~{data['distance_km']} км)\n"
        f"⚖️ Вес: {data['weight_kg']} кг | Объём: {data['volume_m3']} м³\n"
        f"📐 Расчётный вес: <b>{q['chargeable_weight']} кг</b>\n\n"
        f"💰 <b>Стоимость:</b>\n"
        f"• Перевозка: <b>{q['delivery_price']:.2f} BYN</b>\n"
        f"• Упаковка ({q['pack_title']}): <b>{q['packaging_price']:.2f} BYN</b>\n"
        f"• Страховка: <b>{q['insurance_price']:.2f} BYN</b>"
        f"{door_line}\n\n"
        f"💵 <b>Итого: {q['total']:.2f} BYN</b>\n"
        f"⏱ Срок доставки: <b>{q['days_str']}</b>"
    )

    await state.update_data(last_quote=result_text)

    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Оформить заявку", callback_data="order:start")
    kb.button(text="🔄 Рассчитать другой груз", callback_data="restart")
    kb.adjust(1)

    await callback.message.edit_text(result_text, reply_markup=kb.as_markup())
    await callback.answer()


# --- КНОПКА "НАЗАД" И "ЗАНОВО" ---
@router.callback_query(F.data == "restart")
async def cb_restart(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(OrderSteps.origin)
    await callback.message.edit_text(
        "<b>Шаг 1 из 7.</b> Выберите <b>город отправления</b> или напишите его вручную:",
        reply_markup=cities_kb("orig"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("back:"))
async def cb_back(callback: CallbackQuery, state: FSMContext):
    target = callback.data.split(":")[1]
    if target == "origin":
        await state.set_state(OrderSteps.origin)
        await callback.message.edit_text(
            "<b>Шаг 1 из 7.</b> Выберите <b>город отправления</b>:",
            reply_markup=cities_kb("orig"),
        )
    elif target == "destination":
        await state.set_state(OrderSteps.destination)
        await callback.message.edit_text(
            "<b>Шаг 2 из 7.</b> Выберите <b>город получения</b>:",
            reply_markup=cities_kb("dest", include_back=True),
        )
    elif target == "weight":
        await state.set_state(OrderSteps.weight)
        await callback.message.edit_text(
            "<b>Шаг 3 из 7.</b> Выберите или введите <b>вес груза</b> (кг):",
            reply_markup=weight_kb(),
        )
    elif target == "volume":
        await state.set_state(OrderSteps.volume)
        await callback.message.edit_text(
            "<b>Шаг 4 из 7.</b> Выберите размер или введите <b>габариты в см</b>:",
            reply_markup=volume_kb(),
        )
    elif target == "packaging":
        await state.set_state(OrderSteps.packaging)
        await callback.message.edit_text(
            "<b>Шаг 5 из 7.</b> Выберите тип <b>упаковки</b>:",
            reply_markup=packaging_kb(),
        )
    elif target == "insurance":
        await state.set_state(OrderSteps.insurance)
        await callback.message.edit_text(
            "<b>Шаг 6 из 7.</b> Выберите сумму <b>страховки</b>:",
            reply_markup=insurance_kb(),
        )
    await callback.answer()


# --- ОФОРМЛЕНИЕ ЗАЯВКИ (ТЕЛЕФОН В 1 КЛИК) ---
@router.callback_query(F.data == "order:start")
async def cb_order_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OrderSteps.phone)
    phone_kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить мой номер телефона", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await callback.message.answer(
        "Нажмите кнопку ниже, чтобы отправить ваш номер телефона, или напишите его вручную (например: <code>+375 29 123-45-67</code>):",
        reply_markup=phone_kb,
    )
    await callback.answer()


@router.message(OrderSteps.phone, F.contact | F.text)
async def step_phone_received(message: Message, state: FSMContext, bot: Bot):
    phone = message.contact.phone_number if message.contact else message.text.strip()
    data = await state.get_data()
    quote_text = data.get("last_quote", "")

    if MANAGER_CHAT_ID:
        user = message.from_user
        lead = (
            f"🔔 <b>Новая заявка на доставку!</b>\n"
            f"👤 Клиент: {user.full_name} (@{user.username or 'без username'})\n"
            f"📞 Телефон: <b>{phone}</b>\n\n"
            f"{quote_text}"
        )
        await bot.send_message(chat_id=int(MANAGER_CHAT_ID), text=lead)

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Сделать новый расчёт", callback_data="restart")

    await message.answer(
        f"✅ Спасибо! Заявка принята (номер: <b>{phone}</b>).\nМенеджер свяжется с вами в ближайшее время.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await message.answer("Хотите рассчитать ещё одну посылку?", reply_markup=kb.as_markup())
    await state.clear()


# --- ЗАПУСК (ПОДДЕРЖИВАЕТ И КОМПЬЮТЕР, И БЕСПЛАТНЫЙ ХОСТИНГ RENDER) ---
async def on_startup_webhook(bot: Bot):
    webhook_url = os.getenv("RENDER_EXTERNAL_URL")
    await bot.set_webhook(f"{webhook_url}/webhook")


async def main():
    await init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    webhook_host = os.getenv("RENDER_EXTERNAL_URL")
    if webhook_host:
        # Режим для бесплатного хостинга Render.com
        dp.startup.register(on_startup_webhook)
        app = web.Application()
        app.router.add_get("/", lambda request: web.Response(text="Bot is alive"))
        SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
        setup_application(app, dp, bot=bot)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080)))
        await site.start()
        await asyncio.Event().wait()
    else:
        # Обычный запуск у вас на компьютере
        await bot.delete_webhook(drop_pending_updates=True)
        print("Бот успешно запущен! Откройте Telegram и напишите ему /start")
        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())