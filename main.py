import asyncio
import html
import logging

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# --------------------------------------------------------------------------- #
#                                  НАСТРОЙКИ                                  #
# --------------------------------------------------------------------------- #


# Введите ваш токен
BOT_TOKEN = ""

BASE_URL = "https://lk-test.xn--80aadcsl2bh1ai.xn--p1ai"
API_URL = f"{BASE_URL}/api/Material/getList"


LOGIN_URL = f"{BASE_URL}/api/login"

LOGIN_EMAIL = "test@example.com"
LOGIN_PASSWORD = "qwerty123"

# Таймаут запроса к API (секунды)
REQUEST_TIMEOUT = 15

# Максимальная длина одного сообщения Telegram (лимит 4096, берём с запасом)
MAX_MESSAGE_LENGTH = 3500

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

router = Router()

# Кэш токена авторизации в памяти процесса.
# Простой подход: один токен на весь бот (не на пользователя Telegram),
# т.к. логинимся под одним тестовым аккаунтом API.
_auth_token: str | None = None


# --------------------------------------------------------------------------- #
#                                КЛАВИАТУРЫ                                  #
# --------------------------------------------------------------------------- #


def get_start_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура с кнопкой запроса данных по материалам."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📦 Получить данные по материалам",
                    callback_data="get_materials",
                )
            ]
        ]
    )


# --------------------------------------------------------------------------- #
#                          РАБОТА С ВНЕШНИМ API                              #
# --------------------------------------------------------------------------- #


async def login(session: aiohttp.ClientSession) -> str:
    """
    Логинится на API по email/password и возвращает токен авторизации.
    Бросает исключение, если логин не удался.
    """
    payload = {"email": LOGIN_EMAIL, "password": LOGIN_PASSWORD}

    async with session.post(LOGIN_URL, json=payload) as response:
        body_text = await response.text()
        if response.status >= 400:
            logger.error(
                "Login failed: %s. Response body: %s", response.status, body_text[:2000]
            )
            response.raise_for_status()

        data = await response.json(content_type=None)
        token = extract_token(data)
        if not token:
            logger.error("Не удалось найти токен в ответе логина: %s", data)
            raise RuntimeError(
                "Логин прошёл успешно, но токен не найден в ответе. "
                "Проверь структуру ответа и поправь extract_token()."
            )
        return token


def extract_token(data: dict) -> str | None:
    """
    Пытается найти токен авторизации в ответе сервера.

    Реальный формат ответа этого API:
        {"res": {"api_token": "...", "id": ..., "email": ..., ...}}
    Поэтому основной вариант — res.api_token. Остальные варианты
    оставлены как запас на случай, если формат API поменяется.
    """
    if not isinstance(data, dict):
        return None

    # Основной вариант для этого API
    res = data.get("res")
    if isinstance(res, dict) and res.get("api_token"):
        return res["api_token"]

    # Частые варианты на верхнем уровне (на случай изменений API)
    for key in ("api_token", "token", "access_token", "accessToken"):
        if key in data and data[key]:
            return data[key]

    # Часто токен кладут внутрь "data": {...}
    nested = data.get("data")
    if isinstance(nested, dict):
        for key in ("api_token", "token", "access_token", "accessToken"):
            if key in nested and nested[key]:
                return nested[key]

    return None


async def get_token(session: aiohttp.ClientSession, force_refresh: bool = False) -> str:
    """Возвращает закэшированный токен или логинится заново."""
    global _auth_token
    if _auth_token is None or force_refresh:
        logger.info("Авторизация на API (логин: %s)...", LOGIN_EMAIL)
        _auth_token = await login(session)
        logger.info("Токен успешно получен.")
    return _auth_token


async def fetch_materials() -> dict:
    """
    Делает POST-запрос к API и возвращает разобранный JSON.
    Если токена ещё нет — сначала логинится. Если приходит 401
    (токен истёк/невалиден) — логинится повторно и делает запрос ещё раз.

    Бросает исключения aiohttp.ClientError / asyncio.TimeoutError
    при сетевых проблемах — их обрабатывает вызывающий код.
    """
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Referer": f"{BASE_URL}/app/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }

    # Пустой JSON-объект в теле — минимально достаточное тело для
    # большинства ASP.NET/Laravel контроллеров, ожидающих модель/фильтр.
    payload: dict = {}

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        token = await get_token(session)

        async def call_api(bearer_token: str) -> aiohttp.ClientResponse:
            return await session.post(
                API_URL,
                json=payload,
                headers={"Authorization": f"Bearer {bearer_token}"},
            )

        response = await call_api(token)
        try:
            if response.status == 401:
                # Токен протух — логинимся заново и пробуем один раз ещё.
                logger.warning("Получен 401, обновляю токен и повторяю запрос...")
                response.close()
                token = await get_token(session, force_refresh=True)
                response = await call_api(token)

            if response.status >= 400:
                body_text = await response.text()
                logger.error(
                    "API returned %s. Response body: %s",
                    response.status,
                    body_text[:2000],
                )
            response.raise_for_status()
            return await response.json()
        finally:
            response.release()


# --------------------------------------------------------------------------- #
#                          ФОРМАТИРОВАНИЕ ВЫВОДА                             #
# --------------------------------------------------------------------------- #


def _fmt_price(price_buy, price_sell) -> str:
    """Собирает строку с ценами закупки/продажи, если они есть."""
    parts = []
    if price_buy is not None:
        parts.append(f"закупка {price_buy} ₽")
    if price_sell is not None:
        parts.append(f"продажа {price_sell} ₽")
    return ", ".join(parts) if parts else "цена не указана"


def format_material(material: dict) -> str:
    """Формирует красивый блок текста для одного материала."""
    title = html.escape(str(material.get("title", "—")))
    mat_id = material.get("id", "—")
    mat_type = html.escape(str(material.get("@type", "—")))

    default_unit = material.get("default_unit") or {}
    unit_title = html.escape(str(default_unit.get("title", "—")))
    unit_short = html.escape(str(default_unit.get("title_short", "")))
    unit_str = f"{unit_title} ({unit_short})" if unit_short else unit_title

    lines = [
        f"📦 <b>Материал:</b> {title}",
        f"🆔 <b>ID:</b> {mat_id}",
        f"🏷 <b>Тип:</b> {mat_type}",
        f"📏 <b>Ед. изм.:</b> {unit_str}",
    ]

    prices = material.get("supplier_prices") or []
    if prices:
        lines.append("💰 <b>Цены поставщиков:</b>")
        for price in prices:
            org = html.escape(
                str((price.get("supplier_organization") or {}).get("title", "—"))
            )
            unit_short_p = html.escape(
                str((price.get("unit") or {}).get("title_short", ""))
            )
            price_str = _fmt_price(price.get("price_buy"), price.get("price_sell"))
            unit_suffix = f" / {unit_short_p}" if unit_short_p else ""
            lines.append(f"   • {org}: {price_str}{unit_suffix}")
    else:
        lines.append("💰 <b>Цены поставщиков:</b> нет данных")

    lines.append("➖➖➖➖➖➖➖➖")
    return "\n".join(lines)


def split_into_messages(materials: list[dict]) -> list[str]:
    """
    Собирает отформатированные блоки материалов в несколько
    сообщений так, чтобы каждое не превышало MAX_MESSAGE_LENGTH символов.
    """
    chunks: list[str] = []
    current = ""

    for material in materials:
        block = format_material(material) + "\n"

        # Если один блок сам по себе длиннее лимита — отправляем его отдельно
        if len(block) > MAX_MESSAGE_LENGTH:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(block)
            continue

        if len(current) + len(block) > MAX_MESSAGE_LENGTH:
            chunks.append(current)
            current = block
        else:
            current += block

    if current:
        chunks.append(current)

    return chunks


# --------------------------------------------------------------------------- #
#                               ХЕНДЛЕРЫ                                      #
# --------------------------------------------------------------------------- #


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Обработка команды /start."""
    await message.answer(
        "Привет! 👋\n\n"
        "Я бот для получения данных по материалам из справочника.\n"
        "Нажми кнопку ниже, чтобы загрузить актуальный список.",
        reply_markup=get_start_keyboard(),
    )


@router.callback_query(F.data == "get_materials")
async def handle_get_materials(callback: CallbackQuery) -> None:
    """Обработка нажатия на кнопку «Получить данные по материалам»."""
    # Снимаем "часики" с кнопки
    await callback.answer("Запрашиваю данные...")

    status_message = await callback.message.answer(
        "⏳ Получаю данные по материалам, подождите..."
    )

    # --- Запрос к API с обработкой ошибок --- #
    try:
        data = await fetch_materials()
    except asyncio.TimeoutError:
        await status_message.edit_text(
            "❌ Сервер не ответил за отведённое время (таймаут). "
            "Попробуйте повторить запрос позже."
        )
        return
    except aiohttp.ClientResponseError as e:
        logger.error("HTTP error from API: %s", e)
        await status_message.edit_text(
            f"❌ Сервер вернул ошибку (код {e.status}). Попробуйте позже."
        )
        return
    except aiohttp.ClientError as e:
        logger.error("Network error while requesting API: %s", e)
        await status_message.edit_text(
            "❌ Не удалось подключиться к серверу. "
            "Проверьте соединение и попробуйте позже."
        )
        return
    except Exception:
        logger.exception("Unexpected error while fetching materials")
        await status_message.edit_text(
            "❌ Произошла непредвиденная ошибка при получении данных."
        )
        return

    # --- Разбор JSON-ответа --- #
    try:
        materials = data["res"]["list"]
        total = data["res"].get("total", len(materials))
    except (KeyError, TypeError):
        logger.error("Unexpected JSON structure: %s", data)
        await status_message.edit_text(
            "❌ Не удалось разобрать ответ сервера: неожиданный формат данных."
        )
        return

    if not materials:
        await status_message.edit_text("Список материалов пуст.")
        return

    await status_message.edit_text(
        f"✅ Найдено материалов: {total}\nОтправляю список..."
    )

    # --- Отправка списка материалов кусками, чтобы не превышать лимит Telegram --- #
    for chunk in split_into_messages(materials):
        await callback.message.answer(chunk)
        await asyncio.sleep(0.3)  # небольшая пауза, чтобы не попасть под flood-control

    # Предлагаем обновить данные снова
    await callback.message.answer(
        "Готово! Хотите обновить список?",
        reply_markup=get_start_keyboard(),
    )


# --------------------------------------------------------------------------- #
#                                  ЗАПУСК                                     #
# --------------------------------------------------------------------------- #


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не задан токен бота. Установите переменную окружения BOT_TOKEN "
            "(см. README.md)."
        )

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    logger.info("Бот запускается...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
