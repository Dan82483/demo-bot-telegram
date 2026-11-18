"""
Демо-бот для kwork: заявки + запись на замер.

Запуск:
    pip install -r requirements.txt
    переименовать .env.example в .env, вписать BOT_TOKEN и ADMIN_ID
    python bot.py

Проверка без токена (ничего не отправляет в Telegram):
    python selftest.py

Проверка токена без запуска бота:
    python bot.py --check

Что умеет:
- меню с кнопками
- заявки и брони в SQLite (store.py): перезапуск не стирает данные
- приём заявки: имя, телефон, комментарий, отправка админу с кнопками
- запись на замер: свободные слоты на три дня, занятый слот забронировать нельзя дважды
- /stats для владельца, /cancel для выхода из формы
- необязательная запись в Google Sheets (если задан GSHEET_ID и есть credentials.json)
"""

import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import store

try:  # .env рядом с bot.py, чтобы токен не держать в коде
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # без python-dotenv работаем на переменных окружения
    pass

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("demo-bot")


def env_int(name: str, default: int = 0) -> int:
    """Число из .env. Мусор в переменной (например, ADMIN_ID=@nickname)
    не должен ронять запуск: пишем предупреждение и берём значение по умолчанию."""
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("%s должно быть числом, получено %r - использую %s", name, raw, default)
        return default


BOT_TOKEN = (os.getenv("BOT_TOKEN", "") or "").strip()
ADMIN_ID = env_int("ADMIN_ID", 0)
GSHEET_ID = (os.getenv("GSHEET_ID", "") or "").strip()

router = Router()
# Заявки живут в SQLite (store.py) и поднимаются в память при старте:
# перезапуск бота больше не стирает принятые заявки и брони.
REQUESTS: dict[int, dict] = {}
# Счётчики для команды /stats: сколько заявок всего, за сегодня и сколько людей пришло.
USERS: set[int] = set()
TOTALS: dict[str, int] = {"requests": 0}
BY_DAY: dict[str, int] = {}
# Занятые слоты: один и тот же час нельзя забронировать дважды.
BOOKED: set[str] = set()

DB_PATH = (os.getenv("DB_PATH", "") or "").strip() or str(Path(__file__).with_name("bot.db"))
store.DB_PATH = DB_PATH
store.init()
_saved = store.load_state()
REQUESTS.update(_saved["requests"])
BOOKED.update(_saved["booked"])
TOTALS["requests"] = _saved["total"]
BY_DAY.update(_saved["by_day"])
USERS.update(_saved["users"])
log.info("Хранилище: %s (заявок %s, занятых слотов %s)", DB_PATH, len(REQUESTS), len(BOOKED))

PHONE_RE = re.compile(r"^\+?[0-9\s\-()]{10,18}$")

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Оставить заявку")],
        [KeyboardButton(text="Записаться на замер")],
        [KeyboardButton(text="Цены"), KeyboardButton(text="Связаться")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите пункт или напишите вопрос",
)


class Form(StatesGroup):
    name = State()
    phone = State()
    comment = State()


class Booking(StatesGroup):
    slot = State()
    name = State()
    phone = State()


# ---------- Google Sheets (необязательно) ----------

def sheet_append(row: list) -> None:
    """Пишет строку в таблицу, если настроены GSHEET_ID и credentials.json.
    Молча ничего не делает, если интеграция не настроена."""
    if not GSHEET_ID:
        return
    try:
        import gspread
        gc = gspread.service_account(filename="credentials.json")
        ws = gc.open_by_key(GSHEET_ID).sheet1
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as exc:  # noqa: BLE001 - демо не должно падать из-за таблиц
        log.warning("Google Sheets недоступен: %s", exc)


# ---------- Хелперы ----------

def slots_for_days(days: int = 3, only_free: bool = True) -> list[tuple[str, str]]:
    """Ближайшие слоты: 3 дня по три времени. Формат (подпись, значение).
    only_free=True убирает уже занятые часы, чтобы их не предлагали повторно."""
    result = []
    times = ["10:00", "13:00", "17:00"]
    for i in range(1, days + 1):
        day = datetime.now() + timedelta(days=i)
        label = day.strftime("%d.%m")
        for t in times:
            value = f"{day.strftime('%Y-%m-%d')} {t}"
            if only_free and value in BOOKED:
                continue
            result.append((f"{label} в {t}", value))
    return result


def slots_kb() -> InlineKeyboardMarkup:
    rows = []
    line = []
    for label, value in slots_for_days():
        line.append(InlineKeyboardButton(text=label, callback_data=f"slot:{value}"))
        if len(line) == 3:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def notify_admin(bot: Bot, text: str, request_id: int) -> None:
    if not ADMIN_ID:
        log.warning("ADMIN_ID не задан, заявка #%s не отправлена", request_id)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Принять", callback_data=f"take:{request_id}"),
            InlineKeyboardButton(text="Отклонить", callback_data=f"skip:{request_id}"),
        ]]
    )
    await bot.send_message(ADMIN_ID, text, reply_markup=kb)


# ---------- Счётчики ----------

def register_request(user_id: int | None = None) -> None:
    """Считает заявки для /stats. В памяти процесса: после перезапуска обнуляется."""
    TOTALS["requests"] += 1
    day = datetime.now().strftime("%d.%m.%Y")
    BY_DAY[day] = BY_DAY.get(day, 0) + 1
    if user_id:
        USERS.add(user_id)


def stats_text() -> str:
    day = datetime.now().strftime("%d.%m.%Y")
    return (
        f"Заявок сегодня: {BY_DAY.get(day, 0)}\n"
        f"Всего заявок: {TOTALS['requests']}\n"
        f"Людей в боте: {len(USERS)}\n"
        f"Свободных слотов: {len(slots_for_days())}"
    )


# ---------- Меню ----------

@router.message(CommandStart())
async def start(message: Message) -> None:
    USERS.add(message.from_user.id)
    await message.answer(
        f"Здравствуйте, {message.from_user.first_name}!\n\n"
        "Это демо-бот мастера: он принимает заявки и записывает на замер.\n"
        "Выберите пункт в меню ниже - всё занимает меньше минуты.",
        reply_markup=MAIN_KB,
    )


@router.message(Command("help"))
async def help_cmd(message: Message) -> None:
    await message.answer(
        "Что умею:\n"
        "1. Принять заявку: имя, телефон, комментарий - и отправить мастеру.\n"
        "2. Записать на замер: покажу свободные часы на три дня вперёд.\n"
        "3. Показать цены.\n"
        "4. /cancel - выйти из заполнения и вернуться в меню.\n\n"
        "Данные никуда не передаю, кроме мастера.",
        reply_markup=MAIN_KB,
    )


@router.message(Command("stats"))
async def stats(message: Message) -> None:
    if ADMIN_ID and message.from_user.id != ADMIN_ID:
        await message.answer("Статистика доступна только владельцу бота.")
        return
    await message.answer("Статистика:\n" + stats_text())


@router.message(F.text == "Цены")
async def prices(message: Message) -> None:
    await message.answer(
        "Ориентиры по ценам:\n"
        "• Замер и консультация - бесплатно\n"
        "• Монтаж кондиционера - от 9 500 ₽\n"
        "• Перенос точки - от 4 000 ₽\n"
        "• Выезд за город - +500 ₽ к работе\n\n"
        "Точную сумму назову после замера, до начала работ.",
        reply_markup=MAIN_KB,
    )


@router.message(F.text == "Связаться")
async def contact(message: Message) -> None:
    await message.answer(
        "Телефон: +7 900 000-00-00\n"
        "Работаю ежедневно с 9:00 до 20:00.\n"
        "Если удобнее текстом - оставьте заявку, отвечу в течение 15 минут.",
         reply_markup=MAIN_KB,
     )


@router.message(F.text == "Записаться на замер")
async def booking_start(message: Message, state: FSMContext) -> None:
    """Кнопки меню объявлены до шагов формы: иначе «Записаться на замер»,
    нажатая посреди заявки, попала бы в поле «как вас зовут»."""
    free = slots_for_days()
    if not free:
        await state.clear()
        await message.answer(
            "Свободных часов на ближайшие три дня нет. Напишите удобный день - предложу время.",
            reply_markup=MAIN_KB,
        )
        return
    await state.set_state(Booking.slot)
    await message.answer("Выберите удобное время. Свободные часы на три дня вперёд:", reply_markup=slots_kb())


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    was_busy = await state.get_state() is not None
    await state.clear()
    await message.answer(
        "Отменил, вернул в меню." if was_busy else "Вы и так в меню - выберите пункт ниже.",
        reply_markup=MAIN_KB,
    )


# ---------- Заявка ----------

@router.message(F.text == "Оставить заявку")
async def form_start(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.name)
    await message.answer("Как вас зовут? Напишите имя - так будет понятнее, как к вам обращаться.")


@router.message(Form.name)
async def form_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Имя слишком короткое. Напишите, пожалуйста, ещё раз.")
        return
    await state.update_data(name=name)
    await state.set_state(Form.phone)
    await message.answer(
        "Телефон для связи? Например, +7 900 000-00-00.\n"
        "Нужен только чтобы согласовать время, звонков рекламного характера не делаю."
    )


@router.message(Form.phone, F.text)
async def form_phone(message: Message, state: FSMContext) -> None:
    phone = (message.text or "").strip()
    if not PHONE_RE.match(phone):
        await message.answer(
            "Не похоже на номер. Пришлите в формате +7 900 000-00-00 "
            "или нажмите кнопку ниже, чтобы отправить свой номер.",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="Отправить мой номер", request_contact=True)]],
                resize_keyboard=True,
            ),
        )
        return
    await state.update_data(phone=phone)
    await state.set_state(Form.comment)
    await message.answer(
        "Что нужно сделать? Опишите в двух словах - например, "
        "«повесить кондиционер в спальне, панельный дом».\n"
        "Если нечего добавить, напишите «нет»."
    )


@router.message(Form.phone, F.contact)
async def form_phone_contact(message: Message, state: FSMContext) -> None:
    await state.update_data(phone=message.contact.phone_number)
    await state.set_state(Form.comment)
    await message.answer("Номер получил. Что нужно сделать? Опишите в двух словах.")


@router.message(Form.comment)
async def form_comment(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    comment = (message.text or "").strip()
    request_id = len(REQUESTS) + 1
    register_request(message.from_user.id)
    REQUESTS[request_id] = {
        "name": data.get("name"),
        "phone": data.get("phone"),
        "comment": comment,
        "user": message.from_user.id,
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    # Сначала на диск, потом ответ клиенту: если процесс упадёт, заявка уже сохранена.
    store.save_request(
        request_id,
        kind="заявка",
        name=data.get("name") or "",
        phone=data.get("phone") or "",
        comment=comment,
        user_id=message.from_user.id,
    )
    await state.clear()

    sheet_append([
        datetime.now().strftime("%d.%m.%Y %H:%M"),
        data.get("name", ""),
        data.get("phone", ""),
        comment,
        "заявка",
    ])

    await notify_admin(
        bot,
        f"Новая заявка #{request_id}\n"
        f"Имя: {data.get('name')}\n"
        f"Телефон: {data.get('phone')}\n"
        f"Задача: {comment}\n"
        f"@{message.from_user.username or message.from_user.id}",
        request_id,
    )

    if ADMIN_ID:
        text = (
            f"Записал, {data.get('name')}.\n"
            "Мастер увидел заявку и свяжется в течение 15 минут в рабочее время.\n"
            "Если планы изменятся - просто напишите здесь."
        )
    else:
        # Честно: без ADMIN_ID уведомление уйти не может, обещать обратное нельзя.
        text = (
            f"Заявка сохранена, {data.get('name')}.\n"
            "Внимание: владелец бота не настроен (ADMIN_ID), поэтому уведомление не отправлено. "
            "Так бывает только в тестовом запуске - на рабочем боте мастер получает заявку сразу."
        )
    await message.answer(text, reply_markup=MAIN_KB)


# ---------- Запись на замер ----------

@router.callback_query(F.data.startswith("slot:"))
async def booking_slot(call: CallbackQuery, state: FSMContext) -> None:
    slot = call.data.split(":", 1)[1]
    if slot in BOOKED:
        await call.answer("Это время уже заняли. Выберите другое.", show_alert=True)
        return
    await state.update_data(slot=slot)
    await state.set_state(Booking.name)
    await call.message.edit_text(f"Выбрано: {slot}.\n\nКак вас зовут?")
    await call.answer()


@router.message(Booking.name)
async def booking_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Напишите имя, пожалуйста.")
        return
    await state.update_data(name=name)
    await state.set_state(Booking.phone)
    await message.answer("Телефон для подтверждения?")


@router.message(Booking.phone)
async def booking_phone(message: Message, state: FSMContext, bot: Bot) -> None:
    phone = (message.text or "").strip()
    if not PHONE_RE.match(phone):
        await message.answer("Проверьте номер: нужен формат +7 900 000-00-00.")
        return

    data = await state.get_data()
    slot = data.get("slot") or ""
    await state.clear()

    # Пока человек вводил телефон, этот час мог занять кто-то другой.
    if slot in BOOKED:
        await message.answer(
            f"Это время ({slot}) только что заняли. Откройте «Записаться на замер» - покажу свободные.",
            reply_markup=MAIN_KB,
        )
        return
    request_id = len(REQUESTS) + 1
    # Бронь ставим в базе первой: INSERT ... IGNORE сам решает, чей слот.
    if not store.book_slot(slot, request_id):
        await message.answer(
            f"Это время ({slot}) только что заняли. Откройте «Записаться на замер» - покажу свободные.",
            reply_markup=MAIN_KB,
        )
        return
    BOOKED.add(slot)
    register_request(message.from_user.id)
    REQUESTS[request_id] = {
        "name": data.get("name"),
        "phone": phone,
        "slot": slot,
        "user": message.from_user.id,
    }
    store.save_request(
        request_id,
        kind="запись",
        name=data.get("name") or "",
        phone=phone,
        slot=slot,
        user_id=message.from_user.id,
    )

    sheet_append([
        datetime.now().strftime("%d.%m.%Y %H:%M"),
        data.get("name", ""),
        phone,
        f"замер: {data.get('slot')}",
        "запись",
    ])

    await notify_admin(
        bot,
        f"Запись на замер #{request_id}\n"
        f"Имя: {data.get('name')}\n"
        f"Телефон: {phone}\n"
        f"Время: {data.get('slot')}",
        request_id,
    )

    await message.answer(
        f"Готово, {data.get('name')}: {data.get('slot')}.\n"
        "Подтвержу в течение 15 минут. Если время перестанет подходить - напишите здесь.",
        reply_markup=MAIN_KB,
    )


# ---------- Кнопки у админа ----------

@router.callback_query(F.data.startswith(("take:", "skip:")))
async def admin_decision(call: CallbackQuery, bot: Bot) -> None:
    action, raw_id = call.data.split(":", 1)
    request_id = int(raw_id)
    request = REQUESTS.get(request_id)

    if call.from_user.id != ADMIN_ID:
        await call.answer("Это кнопка мастера.", show_alert=True)
        return

    if not request:
        await call.answer("Заявка не найдена.", show_alert=True)
        return

    if action == "take":
        await call.message.edit_text(call.message.text + "\n\n✅ Принято в работу")
        await bot.send_message(
            request["user"],
            "Мастер принял вашу заявку и свяжется с вами в ближайшее время.",
        )
    else:
        await call.message.edit_text(call.message.text + "\n\n❌ Отклонено")
        await bot.send_message(
            request["user"],
            "К сожалению, по этим датам не смогу взяться. "
            "Напишите желаемое время - предложу альтернативу.",
        )
    await call.answer()


@router.message()
async def fallback(message: Message) -> None:
    await message.answer(
        "Не понял сообщение. Выберите пункт в меню или напишите «Помощь».",
        reply_markup=MAIN_KB,
    )


def build_dispatcher() -> Dispatcher:
    """Собирает диспетчер отдельно от запуска: так selftest.py проверяет,
    что все обработчики регистрируются, не поднимая соединение с Telegram."""
    dp = Dispatcher()
    dp.include_router(router)
    return dp


async def check_token() -> None:
    """python bot.py --check: проверяет токен через getMe и выходит.
    Ничего не отправляет и не забирает сообщения."""
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN: скопируйте .env.example в .env и впишите токен от @BotFather.")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        me = await bot.get_me()
    finally:
        await bot.session.close()
    print(f"Токен рабочий. Бот: @{me.username} (id {me.id}, имя «{me.first_name}»)")
    if ADMIN_ID:
        print(f"ADMIN_ID: {ADMIN_ID} - заявки пойдут туда.")
    else:
        print("ADMIN_ID не задан: заявки будут сохраняться, но уведомление мастеру не уйдёт.")


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Скопируйте .env.example в .env и впишите токен от @BotFather.")
    if not ADMIN_ID:
        log.warning("ADMIN_ID не задан: уведомления мастеру отправляться не будут.")

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher()
    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        if "--check" in sys.argv:
            asyncio.run(check_token())
        else:
            asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as exc:
        log.info("Остановка: %s", exc)
