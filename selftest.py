"""
Автотест демо-бота без токена и без интернета.

Что делает: поднимает настоящий Dispatcher из bot.py и прогоняет сценарии
(заявка, запись на замер, кнопки мастера, /cancel) на поддельной сессии Telegram.
Ничего никуда не отправляет, токен не нужен, аккаунт не затрагивается.

Запуск:
    python selftest.py

Код возврата 0 - все проверки прошли, 1 - есть падения (список печатается).
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import CallbackQuery, Chat, Message, User

# Тест пишет в отдельную пустую базу, чтобы не трогать bot.db проекта.
os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="demo-bot-test-")) / "test.db")

import bot as demo  # noqa: E402 - переменная окружения должна быть задана до импорта


# ---------- Поддельная сессия Telegram ----------

class FakeSession(BaseSession):
    """Вместо запроса в api.telegram.org складывает метод в список CALLS."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict]] = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append((type(method).__name__, method.model_dump(exclude_none=True)))
        return True

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError("Файлы в тесте не отправляются")

    async def close(self) -> None:
        return None

    # --- удобные выборки ---

    def texts(self, method: str) -> list[str]:
        out = []
        for name, payload in self.calls:
            if name == method:
                out.append(payload.get("text") or payload.get("caption") or "")
        return out

    def last(self, method: str) -> dict:
        for name, payload in reversed(self.calls):
            if name == method:
                return payload
        return {}

    def clear(self) -> None:
        self.calls.clear()


BOT = Bot("42:TEST", session=FakeSession())
SESSION: FakeSession = BOT.session  # type: ignore[assignment]


# ---------- Поддельное состояние FSM ----------

class FakeState:
    def __init__(self) -> None:
        self.data: dict = {}
        self.state: str | None = None

    async def set_state(self, value) -> None:
        self.state = getattr(value, "state", value)

    async def get_state(self) -> str | None:
        return self.state

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)

    async def get_data(self) -> dict:
        return dict(self.data)

    async def clear(self) -> None:
        self.data.clear()
        self.state = None


# ---------- Фабрики событий ----------

def msg(text: str = "", user_id: int = 111) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Тест"),
        text=text,
    ).as_(BOT)


def callback(data: str, user_id: int = 111, source: Message | None = None) -> CallbackQuery:
    return CallbackQuery(
        id="cb-1",
        from_user=User(id=user_id, is_bot=False, first_name="Тест"),
        chat_instance="test",
        data=data,
        message=source or msg("/start", user_id),
    ).as_(BOT)


# ---------- Каркас проверок ----------

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((bool(ok), name, detail))


def reset() -> None:
    """Чистое состояние перед каждым сценарием."""
    demo.REQUESTS.clear()
    demo.USERS.clear()
    demo.TOTALS["requests"] = 0
    demo.BY_DAY.clear()
    demo.BOOKED.clear()
    SESSION.clear()


# ---------- Сценарии ----------

async def t_handlers() -> None:
    # Обработчики живут в роутере; диспетчер только подключает роутер.
    demo.build_dispatcher()
    messages = demo.router.message.handlers
    callbacks = demo.router.callback_query.handlers
    check("Обработчики сообщений зарегистрированы (>= 12)", len(messages) >= 12, f"их {len(messages)}")
    check("Обработчики кнопок зарегистрированы (2: слоты и решение мастера)", len(callbacks) == 2, f"их {len(callbacks)}")

    names = [h.callback.__name__ for h in messages]
    check("Дублей среди обработчиков нет", len(names) == len(set(names)), ", ".join(names))

    # Кнопки меню должны стоять раньше шагов формы, иначе текст кнопки попадёт в поле ввода.
    order = {h.callback.__name__: i for i, h in enumerate(messages)}
    check(
        "«Записаться на замер» объявлена раньше шагов формы заявки",
        order.get("booking_start", 10**6) < order.get("form_name", -1),
        f"booking_start={order.get('booking_start')}, form_name={order.get('form_name')}",
    )
    check(
        "«Оставить заявку» объявлена раньше шагов формы",
        order.get("form_start", 10**6) < order.get("form_name", -1),
        f"form_start={order.get('form_start')}, form_name={order.get('form_name')}",
    )


async def t_phone() -> None:
    good = ["+7 912 345-67-89", "89123456789", "+7 (900) 000-00-00"]
    bad = ["123", "телефон", "", "+7 912 345-67-89 доб. 5"]
    ok_good = all(demo.PHONE_RE.match(x) for x in good)
    ok_bad = not any(demo.PHONE_RE.match(x) for x in bad)
    check("Телефон: 3 нормальных номера проходят", ok_good, str(good))
    check("Телефон: 4 мусорных отклоняются", ok_bad, str(bad))


async def t_slots() -> None:
    all_slots = demo.slots_for_days(only_free=False)
    check("Слотов всего 9 (3 дня по 3 часа)", len(all_slots) == 9, f"их {len(all_slots)}")

    today = datetime.now().date()
    days = {value.split(" ")[0] for _, value in all_slots}
    expected = {(today + timedelta(days=i)).isoformat() for i in (1, 2, 3)}
    check("Слоты только на три следующих дня", days == expected, f"{sorted(days)}")

    label, value = all_slots[0]
    check("Подпись слота выглядит как «ДД.ММ в ЧЧ:00»", bool(re.fullmatch(r"\d{2}\.\d{2} в \d{2}:\d{2}", label)), label)

    demo.BOOKED.add(value)
    check("Занятый слот исчезает из списка", len(demo.slots_for_days()) == 8, f"осталось {len(demo.slots_for_days())}")
    demo.BOOKED.clear()


async def t_stats() -> None:
    reset()
    demo.register_request(1)
    demo.register_request(2)
    demo.register_request(1)
    text = demo.stats_text()
    check("Статистика: заявок сегодня 3", "Заявок сегодня: 3" in text, text.replace("\n", " | "))
    check("Статистика: всего заявок 3", "Всего заявок: 3" in text)
    check("Статистика: людей в боте 2", "Людей в боте: 2" in text)
    check("Статистика: свободных слотов 9", "Свободных слотов: 9" in text)


async def t_form_full() -> None:
    """Заявка целиком: короткое имя -> мусорный телефон -> нормальный -> задача."""
    reset()
    demo.ADMIN_ID = 900  # мастер настроен
    state = FakeState()

    await demo.form_start(msg("Оставить заявку"), state)
    check("Форма: первый вопрос про имя", "Как вас зовут" in SESSION.last("SendMessage")["text"])

    await demo.form_name(msg("И"), state)
    check("Форма: имя из одной буквы не принимается", state.state is not None and "слишком короткое" in SESSION.last("SendMessage")["text"])

    await demo.form_name(msg("Иван"), state)
    check("Форма: после имени спрашивают телефон", "Телефон" in SESSION.last("SendMessage")["text"])
    check("Форма: имя запомнено в состоянии", state.data.get("name") == "Иван", str(state.data))

    await demo.form_phone(msg("123"), state)
    check("Форма: мусорный телефон не пропускается", state.state is not None and "Не похоже на номер" in SESSION.last("SendMessage")["text"])

    await demo.form_phone(msg("+7 912 345-67-89"), state)
    check("Форма: после телефона спрашивают задачу", "Что нужно сделать" in SESSION.last("SendMessage")["text"])

    SESSION.clear()
    await demo.form_comment(msg("Повесить кондиционер в спальне"), state, BOT)

    to_admin = [t for t in SESSION.texts("SendMessage") if "Новая заявка" in t]
    check("Форма: мастеру ушло уведомление о заявке", len(to_admin) == 1, str(to_admin[:1]))
    if to_admin:
        card = to_admin[0]
        check(
            "Форма: в карточке мастера есть имя, телефон и задача",
            all(x in card for x in ("Иван", "+7 912 345-67-89", "Повесить кондиционер")),
            card.replace("\n", " | "),
        )
        check("Форма: у карточки мастера есть номер заявки", "#1" in card, card.split("\n")[0])
    check("Форма: клиенту пришло подтверждение", any("Записал, Иван" in t for t in SESSION.texts("SendMessage")))
    check("Форма: состояние очищено после отправки", state.state is None and state.data == {}, str(state.state))
    check("Форма: заявка сохранена в память", demo.REQUESTS.get(1, {}).get("name") == "Иван", str(demo.REQUESTS.get(1)))

    markup = SESSION.last("SendMessage").get("reply_markup") or {}
    buttons = [b["text"] for row in markup.get("keyboard", []) for b in row]
    check("Меню после заявки вернулось клиенту", buttons[:2] == ["Оставить заявку", "Записаться на замер"], str(buttons))


async def t_form_no_admin() -> None:
    """ADMIN_ID не задан: бот обязан честно сказать, что уведомление не ушло."""
    reset()
    demo.ADMIN_ID = 0
    state = FakeState()
    await demo.form_start(msg("Оставить заявку"), state)
    await demo.form_name(msg("Марина"), state)
    await demo.form_phone(msg("+7 900 111-22-33"), state)
    SESSION.clear()
    await demo.form_comment(msg("нет"), state, BOT)

    check("Без ADMIN_ID уведомление мастеру не отправляется", not any("Новая заявка" in t for t in SESSION.texts("SendMessage")))
    check(
        "Без ADMIN_ID клиент получает предупреждение, а не обещание",
        any("уведомление не отправлено" in t for t in SESSION.texts("SendMessage")),
        str(SESSION.texts("SendMessage")[:1]),
    )
    demo.ADMIN_ID = 900


async def t_booking() -> None:
    """Запись на замер: свободный слот -> имя -> телефон -> занято."""
    reset()
    demo.ADMIN_ID = 900
    state = FakeState()

    await demo.booking_start(msg("Записаться на замер"), state)
    markup = SESSION.last("SendMessage").get("reply_markup") or {}
    rows = markup.get("inline_keyboard", [])
    check("Запись: показано 9 кнопок со слотами", sum(len(r) for r in rows) == 9, f"кнопок {sum(len(r) for r in rows)}")

    slot = rows[0][0]["callback_data"].split(":", 1)[1]
    await demo.booking_slot(callback(f"slot:{slot}"), state)
    check("Запись: слот выбран и сохранён", state.data.get("slot") == slot, str(state.data))

    await demo.booking_name(msg("Алексей"), state)
    SESSION.clear()
    await demo.booking_phone(msg("+7 912 000-11-22"), state, BOT)

    check("Запись: слот помечен занятым", slot in demo.BOOKED, str(demo.BOOKED))
    check("Запись: мастеру ушла карточка записи", any("Запись на замер" in t for t in SESSION.texts("SendMessage")))
    check("Запись: клиент получил подтверждение", any("Готово, Алексей" in t for t in SESSION.texts("SendMessage")))

    # Второй человек пытается взять тот же час.
    state2 = FakeState()
    await demo.booking_start(msg("Записаться на замер", user_id=222), state2)
    rows2 = SESSION.last("SendMessage").get("reply_markup", {}).get("inline_keyboard", [])
    busy = any(btn["callback_data"] == f"slot:{slot}" for row in rows2 for btn in row)
    check("Запись: занятый слот больше не предлагается", not busy and state2.state == demo.Booking.slot.state)

    SESSION.clear()
    await demo.booking_slot(callback(f"slot:{slot}", user_id=222), FakeState())
    answer = SESSION.last("AnswerCallbackQuery")
    check("Запись: попытка взять занятый слот отбита предупреждением", "уже заняли" in (answer.get("text") or ""), str(answer.get("text")))


async def t_admin_buttons() -> None:
    """Кнопки «Принять» / «Отклонить» у мастера и запрет для посторонних."""
    reset()
    demo.ADMIN_ID = 900
    demo.REQUESTS[7] = {"name": "Иван", "phone": "+7 900 000-00-00", "slot": "", "user": 111}

    SESSION.clear()
    await demo.admin_decision(callback("take:7", user_id=900, source=msg("Новая заявка #7", 900)), BOT)
    check("Мастер: «Принять» отвечает клиенту", any("принял вашу заявку" in t for t in SESSION.texts("SendMessage")))
    check("Мастер: карточка помечена как принятая", "Принято" in SESSION.texts("EditMessageText")[-1])

    SESSION.clear()
    await demo.admin_decision(callback("skip:7", user_id=900, source=msg("Новая заявка #7", 900)), BOT)
    check("Мастер: «Отклонить» отвечает клиенту", any("не смогу взяться" in t for t in SESSION.texts("SendMessage")))

    SESSION.clear()
    await demo.admin_decision(callback("take:7", user_id=555, source=msg("Новая заявка #7", 555)), BOT)
    answer = SESSION.last("AnswerCallbackQuery")
    check("Мастер: постороннему кнопка не подчиняется", "только мастер" in (answer.get("text") or "") or "мастера" in (answer.get("text") or ""), str(answer.get("text")))
    check("Мастер: постороннему клиенту ничего не отправлено", not SESSION.texts("SendMessage"))

    SESSION.clear()
    await demo.admin_decision(callback("take:999", user_id=900, source=msg("x", 900)), BOT)
    check("Мастер: несуществующая заявка обрабатывается без падения", "не найдена" in (SESSION.last("AnswerCallbackQuery").get("text") or ""))


async def t_cancel_and_menu() -> None:
    reset()
    demo.ADMIN_ID = 900
    state = FakeState()
    await demo.form_start(msg("Оставить заявку"), state)
    check("Отмена: состояние формы выставлено", state.state == demo.Form.name.state)

    await demo.cancel(msg("/cancel"), state)
    check("Отмена: состояние очищено", state.state is None)
    check("Отмена: бот вернул меню", "вернул в меню" in SESSION.last("SendMessage")["text"])

    # Кнопка меню во время формы не должна попасть в поле «как вас зовут».
    state = FakeState()
    await demo.form_start(msg("Оставить заявку"), state)
    await demo.booking_start(msg("Записаться на замер"), state)
    check(
        "Меню: «Записаться на замер» во время формы переключает на запись",
        state.state == demo.Booking.slot.state,
        str(state.state),
    )


async def t_env_int() -> None:
    import os

    os.environ["ADMIN_ID"] = "не число"
    check("Мусор в ADMIN_ID не роняет чтение настроек", demo.env_int("ADMIN_ID", 0) == 0)
    os.environ["ADMIN_ID"] = "12345"
    check("Число в ADMIN_ID читается как число", demo.env_int("ADMIN_ID", 0) == 12345)
    os.environ.pop("ADMIN_ID", None)


async def t_storage() -> None:
    """Главная проверка после доработки: данные переживают перезапуск бота."""
    reset()
    check("Хранилище: файл базы создан при старте", Path(demo.DB_PATH).exists(), demo.DB_PATH)

    demo.store.save_request(
        101, kind="заявка", name="Пётр", phone="+7 900 000-00-01", comment="тест", user_id=5
    )
    demo.store.save_request(
        102, kind="запись", name="Анна", phone="+7 900 000-00-02", slot="2099-01-01 10:00", user_id=6
    )

    first = demo.store.book_slot("2099-01-01 10:00", 102)
    second = demo.store.book_slot("2099-01-01 10:00", 103)
    check("Хранилище: слот закрепляется за первым, второму отказ", first and not second, f"{first} / {second}")

    # Полная перезагрузка состояния - так бот поднимается после падения процесса.
    state = demo.store.load_state()
    check("Хранилище: заявка читается заново", state["requests"].get(101, {}).get("name") == "Пётр", str(state["requests"].get(101)))
    check("Хранилище: запись на замер читается заново", state["requests"].get(102, {}).get("slot") == "2099-01-01 10:00")
    check("Хранилище: занятый слот попадает в состояние", "2099-01-01 10:00" in state["booked"], str(state["booked"]))
    check("Хранилище: счётчик заявок берётся из базы", state["total"] >= 2, f"их {state['total']}")
    check("Хранилище: люди в боте считаются по базе", {5, 6} <= state["users"], str(sorted(state["users"])))

    # Повторное сохранение того же id не размножает заявки.
    demo.store.save_request(101, kind="заявка", name="Пётр", phone="+7 900 000-00-01", comment="тест", user_id=5)
    check("Хранилище: повторная запись не создаёт дубль", len(demo.store.load_state()["requests"]) == 2)


SCENARIOS = [
    ("Обработчики", t_handlers),
    ("Хранилище SQLite", t_storage),
    ("Настройки", t_env_int),
    ("Проверка телефона", t_phone),
    ("Слоты", t_slots),
    ("Статистика", t_stats),
    ("Заявка целиком", t_form_full),
    ("Заявка без ADMIN_ID", t_form_no_admin),
    ("Запись на замер", t_booking),
    ("Кнопки мастера", t_admin_buttons),
    ("Отмена и меню", t_cancel_and_menu),
]


async def main() -> int:
    print("=== Автотест demo-bot: без токена, без интернета ===")
    for title, scenario in SCENARIOS:
        before = len(RESULTS)
        try:
            await scenario()
        except Exception as exc:  # noqa: BLE001 - упавший сценарий это тоже результат
            check(f"{title}: сценарий упал с ошибкой", False, f"{type(exc).__name__}: {exc}")
        tail = RESULTS[before:]
        print(f"\n-- {title}: {sum(1 for ok, _, _ in tail if ok)}/{len(tail)}")

    failed = [(name, detail) for ok, name, detail in RESULTS if not ok]
    print()
    for i, (ok, name, detail) in enumerate(RESULTS, 1):
        mark = "OK  " if ok else "FAIL"
        line = f"{mark} {i:02d}. {name}"
        if not ok and detail:
            line += f"  [{detail}]"
        print(line)

    print()
    if failed:
        print(f"ИТОГ: провалено {len(failed)} из {len(RESULTS)}")
        for name, detail in failed:
            print(f"  - {name}: {detail}")
        return 1
    print(f"ИТОГ: пройдено {len(RESULTS)} из {len(RESULTS)} проверок, падений нет")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
