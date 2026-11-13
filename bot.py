"""
Livecheck-бот для Minecraft-сервера (discord.py 2.x).

Бот каждые N секунд опрашивает публичный API mcsrvstat.us, формирует
красивый Embed со статусом сервера и редактирует одно и то же сообщение
в нужном канале — без «простыни» новых сообщений в чате.

Документация API: https://api.mcsrvstat.us/
Запуск:  python bot.py
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import struct
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from palette_png import render_palette_png

# На Windows консоль по умолчанию работает в cp1251/cp866 и ломается на эмодзи.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - экзотические терминалы
            pass

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

log = logging.getLogger("livecheck")

#: Цвет полоски слева, когда сервер доступен.
COLOR_ONLINE = 0x2ECC71
#: Цвет полоски слева, когда сервер недоступен.
COLOR_OFFLINE = 0xE74C3C
#: Имя файла-вложения, в котором хранится иконка сервера.
ICON_FILENAME = "icon.png"


class ConfigError(RuntimeError):
    """Некорректная или неполная конфигурация."""


def env_str(key: str, default: str = "") -> str:
    """Строка из .env без окружающих пробелов."""
    return os.getenv(key, default).strip()


def env_int(key: str, default: int) -> int:
    """Целое число из .env; при ошибке берётся значение по умолчанию."""
    raw = env_str(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Переменная %s имеет неверное значение %r, беру %s", key, raw, default)
        return default


def env_bool(key: str, default: bool) -> bool:
    """Булево значение из .env (1/0, true/false, yes/no, on/off)."""
    raw = env_str(key).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on", "да"}:
        return True
    if raw in {"0", "false", "no", "off", "нет"}:
        return False
    log.warning("Переменная %s имеет неверное значение %r, беру %s", key, raw, default)
    return default


@dataclass
class Config:
    """Все настройки бота, прочитанные из .env.

    Не frozen: часть настроек меняется из Discord командами
    (например, адрес Minecraft-сервера), и изменения сохраняются
    обратно в .env методом save().
    """

    token: str
    channel_id: int
    guild_id: int
    server_ip: str
    project_name: str
    description: str
    update_interval: int
    api_base_url: str
    icon_base_url: str
    icon_url: str
    use_emoji: bool
    use_motd: bool
    show_version: bool
    show_player_list: bool
    player_list_limit: int
    request_timeout: int
    log_level: str
    state_file: Path
    theme_file: Path
    env_file: Path = None  # type: ignore[assignment]
    #: Цвет полоски, заданный через переменную окружения (для хостингов).
    accent_color: Optional[int] = None

    @property
    def host(self) -> str:
        """Адрес сервера без порта."""
        return self.server_ip.split(":", 1)[0]

    @property
    def port(self) -> int:
        """Порт сервера (по умолчанию 25565)."""
        _, sep, port = self.server_ip.partition(":")
        return int(port) if sep and port.isdigit() else 25565

    @property
    def footer_text(self) -> str:
        """Текст нижнего колонтитула."""
        return f"{self.project_name} Livecheck"

    @property
    def resolved_icon_url(self) -> str:
        """Ссылка на иконку: своя из .env либо автоматическая с mcsrvstat.us."""
        if self.icon_url:
            return self.icon_url
        return f"{self.icon_base_url}/{self.server_ip}"

    def palette_url(self) -> str:
        """Ссылка на картинку-палитру со всеми готовыми цветами."""
        return "attachment://palette.png"

    def save(self) -> None:
        """Сохраняет изменённые настройки обратно в .env.

        Так изменения, сделанные командами в Discord, переживают
        перезапуск бота. Переписываются только нужные строки,
        остальные (включая комментарии) остаются нетронутыми.
        """
        if self.env_file is None:
            return

        updates = {
            "SERVER_IP": self.server_ip,
            "CHANNEL_ID": str(self.channel_id),
            "GUILD_ID": str(self.guild_id),
        }

        try:
            lines = self.env_file.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            log.warning("Не удалось прочитать .env: %s", exc)
            return

        seen: set[str] = set()
        result: list[str] = []
        for line in lines:
            key = line.split("=", 1)[0].strip() if "=" in line else ""
            if key in updates:
                result.append(f"{key}={updates[key]}")
                seen.add(key)
            else:
                result.append(line)

        for key, value in updates.items():
            if key not in seen:
                result.append(f"{key}={value}")

        try:
            self.env_file.write_text("\n".join(result) + "\n", encoding="utf-8")
            log.info("Настройки сохранены в .env")
        except OSError as exc:
            log.warning("Не удалось сохранить .env: %s", exc)

    @classmethod
    def from_env(cls) -> "Config":
        """Собирает конфигурацию и проверяет обязательные поля.

        Настройки читаются из переменных окружения, а .env (если он есть)
        их дополняет. Это позволяет запускать бота на хостинге, где
        секреты задаются прямо в панели, без файла на диске.
        """
        token = env_str("BOT_TOKEN")
        channel_raw = env_str("CHANNEL_ID")
        guild_raw = env_str("GUILD_ID")
        server_ip = env_str("SERVER_IP")

        problems: list[str] = []
        if not token:
            problems.append("BOT_TOKEN не указан (токен бота из Developer Portal)")
        if not channel_raw.isdigit():
            problems.append("CHANNEL_ID должен быть числовым ID канала")
        if not guild_raw.isdigit():
            problems.append("GUILD_ID должен быть числовым ID сервера")
        if not server_ip:
            problems.append("SERVER_IP не указан (например 1.2.3.4 или mc.example.com:25565)")

        if problems:
            raise ConfigError(
                "Не удалось прочитать настройки:\n  - " + "\n  - ".join(problems)
            )

        # Файл настроек нужен только для сохранения правок из Discord.
        # Если файла нет (например, на хостинге) - сохранять просто некуда.
        env_file = BASE_DIR / ".env"

        return cls(
            token=token,
            channel_id=int(channel_raw),
            guild_id=int(guild_raw),
            server_ip=server_ip,
            project_name=env_str("PROJECT_NAME", "Minecraft"),
            description=env_str(
                "DESCRIPTION",
                "Привет! Это живой статус Minecraft-сервера. "
                "Сообщение обновляется автоматически.",
            ),
            update_interval=max(15, env_int("UPDATE_INTERVAL", 60)),
            api_base_url=env_str("API_BASE_URL", "https://api.mcsrvstat.us/3").rstrip("/"),
            icon_base_url=env_str("ICON_BASE_URL", "https://api.mcsrvstat.us/icon").rstrip("/"),
            icon_url=env_str("ICON_URL"),
            use_emoji=env_bool("USE_EMOJI", True),
            use_motd=env_bool("USE_MOTD", True),
            show_version=env_bool("SHOW_VERSION", True),
            show_player_list=env_bool("SHOW_PLAYER_LIST", True),
            player_list_limit=env_int("PLAYER_LIST_LIMIT", 12),
            request_timeout=env_int("REQUEST_TIMEOUT", 15),
            log_level=env_str("LOG_LEVEL", "INFO").upper(),
            state_file=BASE_DIR / env_str("STATE_FILE", ".state.json"),
            theme_file=BASE_DIR / env_str("THEME_FILE", ".theme.json"),
            env_file=env_file if env_file.exists() else None,
            accent_color=parse_hex_color(env_str("ACCENT_COLOR")) if env_str("ACCENT_COLOR") else None,
        )


# ---------------------------------------------------------------------------
# Тема оформления (цвет полоски слева)
# ---------------------------------------------------------------------------


@dataclass
class Theme:
    """
    Настройки внешнего вида embed.

    Пользователю не нравится, когда полоска слева меняет цвет вместе со
    статусом сервера. Поэтому есть режим ``follow_status`` (по умолчанию,
    как в ТЗ: зелёный/красный) и режим ``fixed`` — один цвет на все случаи.
    """

    #: "follow_status" — зелёный/красный по статусу, "fixed" — всегда accent.
    mode: str = "follow_status"
    #: Цвет в режиме fixed.
    accent: int = 0x5865F2
    #: Цвета в режиме follow_status.
    online: int = COLOR_ONLINE
    offline: int = COLOR_OFFLINE

    def color_for(self, online: bool) -> int:
        """Возвращает цвет полоски для текущего статуса сервера."""
        if self.mode == "fixed":
            return self.accent
        return self.online if online else self.offline

    def to_dict(self) -> dict[str, Any]:
        """Сериализует тему для сохранения в файл."""
        return {
            "mode": self.mode,
            "accent": self.accent,
            "online": self.online,
            "offline": self.offline,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Theme":
        """Восстанавливает тему из файла, игнорируя битые значения."""
        theme = cls()
        if not isinstance(data, dict):
            return theme
        mode = str(data.get("mode", theme.mode)).strip().lower()
        if mode in ("follow_status", "fixed"):
            theme.mode = mode
        for key in ("accent", "online", "offline"):
            value = data.get(key)
            if isinstance(value, int) and 0 <= value <= 0xFFFFFF:
                setattr(theme, key, value)
        return theme

    @classmethod
    def load(cls, path: Path) -> "Theme":
        """Читает тему из файла; при ошибке возвращает тему по умолчанию."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        return cls.from_dict(data)

    def save(self, path: Path) -> None:
        """Сохраняет тему в файл."""
        try:
            path.write_text(
                json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("Не удалось сохранить тему: %s", exc)


def parse_hex_color(value: str) -> Optional[int]:
    """
    Разбирает цвет из строки: ``#2ECC71``, ``2ecc71`` или ``0x2ECC71``.

    Также понимает названия из палитры: «зелёный», «green», «фиолетовый».
    Возвращает None, если это не похоже на цвет.
    """
    text = value.strip()

    # По названию из палитры.
    named = PALETTE_BY_NAME.get(text.casefold())
    if named is not None:
        return named

    text = text.lstrip("#")
    if text.lower().startswith("0x"):
        text = text[2:]
    if len(text) != 6:
        return None
    try:
        color = int(text, 16)
    except ValueError:
        return None
    return color if 0 <= color <= 0xFFFFFF else None


#: Minecraft-коды форматирования: § + буква/цифра.
MINECRAFT_CODES = re.compile(r"§[0-9A-FK-ORa-fk-or]")


def strip_minecraft_codes(text: str) -> str:
    """Убирает коды форматирования Minecraft (§c, §l, §r и т.п.)."""
    return MINECRAFT_CODES.sub("", text)


# ---------------------------------------------------------------------------
# Палитра цветов
# ---------------------------------------------------------------------------

def _closest_emoji(color: int) -> str:
    """Подбирает эмодзи-кружок, ближайший к цвету.

    Discord-эмодзи ограничены палитрой, поэтому берём тот, чей цвет
    ближе всего к нужному.
    """
    import colorsys

    r, g, b = (color >> 16) & 255, (color >> 8) & 255, color & 255
    hue, _, _ = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)

    best = PALETTE_EMOJI[0][1]
    best_distance = 999.0
    for emoji, emoji_hex in PALETTE_EMOJI:
        er, eg, eb = (emoji_hex >> 16) & 255, (emoji_hex >> 8) & 255, emoji_hex & 255
        ehue, _, _ = colorsys.rgb_to_hls(er / 255, eg / 255, eb / 255)
        # Разница по тону (круговая) плюс штраф за разную светлоту.
        dh = abs(hue - ehue)
        dh = min(dh, 1 - dh) * 2
        dl = abs((r + g + b) / 3 - (er + eg + eb) / 3) / 255
        distance = dh + dl
        if distance < best_distance:
            best_distance = distance
            best = emoji
    return best


#: Базовые Discord-эмодзи кружков с их «эталонными» цветами.
PALETTE_EMOJI: list[tuple[str, int]] = [
    ("\U0001F534", 0xE74C3C),  # красный
    ("\U0001F7E0", 0xF39C12),  # оранжевый
    ("\U0001F7E1", 0xF1C40F),  # жёлтый
    ("\U0001F7E2", 0x2ECC71),  # зелёный
    ("\U0001F535", 0x3498DB),  # синий
    ("\U0001F7E3", 0x9B59B6),  # фиолетовый
    ("\U0001F7E4", 0x8B4513),  # коричневый
    ("⬛", 0x2C2F33),  # чёрный
    ("⬜", 0xFFFFFF),  # белый
]


def _build_palette() -> list[tuple[int, str, str]]:
    """Собирает палитру: цвет, название и подходящий эмодзи-кружок."""
    result: list[tuple[int, str, str]] = []
    for color, name in _PALETTE_BASE:
        result.append((color, name, _closest_emoji(color)))
    return result


#: Базовый список цветов: (hex, название).
_PALETTE_BASE: list[tuple[int, str]] = [
    (0x2ECC71, "Зелёный"),
    (0x27AE60, "Тёмно-зелёный"),
    (0x1ABC9C, "Бирюзовый"),
    (0x3498DB, "Голубой"),
    (0x9B59B6, "Фиолетовый"),
    (0xE91E63, "Розовый"),
    (0xE74C3C, "Красный"),
    (0xF39C12, "Оранжевый"),
    (0xF1C40F, "Жёлтый"),
    (0xE67E22, "Тёмно-оранжевый"),
    (0x34495E, "Тёмно-синий"),
    (0x95A5A6, "Серый"),
    (0x2C2F33, "Почти чёрный"),
    (0xFFFFFF, "Белый"),
    (0x5865F2, "Discord blurple"),
    (0xEB459E, "Discord фуксия"),
    (0x57F287, "Discord зелёный"),
    (0xFEE75C, "Discord жёлтый"),
    (0xED4245, "Discord красный"),
    (0x7B00B3, "Пурпур"),
    (0x00A8FC, "Голубой светлый"),
    (0xF47B67, "Лососевый"),
    (0x3BA55D, "Зелёный тёмный"),
    (0xFF7F50, "Коралловый"),
    (0x8B4513, "Коричневый"),
]

#: Готовые цвета для полоски слева: (hex, название, эмодзи-образец).
#: Порядок соответствует строкам в палитре-картинке.
PALETTE: list[tuple[int, str, str]] = _build_palette()

#: Поиск цвета по названию (латиница и кириллица).
PALETTE_BY_NAME: dict[str, int] = {}
for _hex, _name, _emoji in PALETTE:
    PALETTE_BY_NAME[_name.casefold()] = _hex

#: Английские названия для привычных цветов.
for _english, _hex in {
    "green": 0x2ECC71,
    "dark green": 0x27AE60,
    "teal": 0x1ABC9C,
    "blue": 0x3498DB,
    "purple": 0x9B59B6,
    "violet": 0x9B59B6,
    "pink": 0xE91E63,
    "magenta": 0xE91E63,
    "red": 0xE74C3C,
    "orange": 0xF39C12,
    "yellow": 0xF1C40F,
    "dark orange": 0xE67E22,
    "navy": 0x34495E,
    "gray": 0x95A5A6,
    "grey": 0x95A5A6,
    "black": 0x2C2F33,
    "white": 0xFFFFFF,
    "blurple": 0x5865F2,
    "cyan": 0x00A8FC,
    "brown": 0x8B4513,
    "coral": 0xFF7F50,
    "lime": 0x3BA55D,
    "salmon": 0xF47B67,
    "lemon": 0xFEE75C,
}.items():
    PALETTE_BY_NAME.setdefault(_english, _hex)

#: Кириллические синонимы.
for _russian, _english in {
    "зелёный": "green",
    "зеленый": "green",
    "тёмно-зелёный": "dark green",
    "темно-зеленый": "dark green",
    "бирюзовый": "teal",
    "голубой": "blue",
    "синий": "blue",
    "фиолетовый": "purple",
    "розовый": "pink",
    "пурпурный": "magenta",
    "красный": "red",
    "оранжевый": "orange",
    "жёлтый": "yellow",
    "желтый": "yellow",
    "тёмно-оранжевый": "dark orange",
    "темно-оранжевый": "dark orange",
    "тёмно-синий": "navy",
    "темно-синий": "navy",
    "серый": "gray",
    "чёрный": "black",
    "черный": "black",
    "белый": "white",
    "голубой светлый": "cyan",
    "коралловый": "coral",
    "лаймовый": "lime",
    "коричневый": "brown",
}.items():
    PALETTE_BY_NAME[_russian] = PALETTE_BY_NAME[_english]


# ---------------------------------------------------------------------------
# Палитра: кнопки-пипетки
# ---------------------------------------------------------------------------


class PaletteView(discord.ui.View):
    """
    Ряд кнопок с готовыми цветами.

    Нажатие на кнопку сразу применяет цвет к витрине - работает как
    пипетка: выбрал квадратик, полоска слева перекрасилась.
    """

    def __init__(self, bot: "LivecheckBot") -> None:
        super().__init__(timeout=None)
        self.bot = bot

        for index, (color, name, emoji) in enumerate(PALETTE):
            button = discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label=name,
                emoji=emoji,
                custom_id=f"palette:{color:06X}",
            )
            button.callback = self._make_callback(color, name)  # type: ignore[method-assign]
            self.add_item(button)

    def _make_callback(self, color: int, name: str):
        """Создаёт обработчик нажатия для конкретного цвета."""

        async def callback(interaction: discord.Interaction) -> None:
            if not await self.bot._require_admin(interaction):
                return

            self.bot.theme.mode = "fixed"
            self.bot.theme.accent = color
            self.bot.theme.save(self.bot.cfg.theme_file)

            embed = self.bot._settings_embed()
            embed.description = (
                f"✅ Цвет полоски обновлён: `#{color:06X}` — {name}.\n{embed.description}"
            )
            embed.color = discord.Color(color)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            await self.bot.update_status_now()

        return callback


# ---------------------------------------------------------------------------
# Модель данных
# ---------------------------------------------------------------------------


@dataclass
class ServerStatus:
    """Снимок состояния Minecraft-сервера."""

    online: bool = False
    players_online: int = 0
    players_max: int = 0
    version: str = ""
    players: list[str] = field(default_factory=list)
    #: Байты иконки сервера (PNG). Есть только когда сервер онлайн.
    icon_bytes: Optional[bytes] = None
    #: Строки MOTD - краткого описания сервера из списка Minecraft.
    motd: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Клиент публичного API mcsrvstat.us
# ---------------------------------------------------------------------------


class MinecraftAPI:
    """
    Обёртка над https://api.mcsrvstat.us/3/{address}.

    Важно: None в качестве результата означает «запрос не удался» (сеть,
    таймаут, 403). Это не то же самое, что «сервер оффлайн» — в таком
    случае бот не должен перекрашивать embed в красный.
    """

    def __init__(self, session: aiohttp.ClientSession, cfg: Config) -> None:
        self._session = session
        self._cfg = cfg

    async def fetch(self, address: Optional[str] = None) -> Optional[ServerStatus]:
        """Запрашивает состояние сервера. None — запрос не удался.

        address — конкретный адрес; по умолчанию берётся из настроек.
        """
        target = (address or self._cfg.server_ip).strip()
        url = f"{self._cfg.api_base_url}/{target}"
        timeout = aiohttp.ClientTimeout(total=self._cfg.request_timeout)

        try:
            async with self._session.get(url, timeout=timeout) as response:
                if response.status == 404:
                    log.warning("Сервер %s не найден в базе mcsrvstat.us", target)
                    return ServerStatus()
                if response.status == 403:
                    log.error("API отклонило запрос (403). Нужен нормальный User-Agent.")
                    return None
                response.raise_for_status()
                payload = await response.json(content_type=None)
        except asyncio.TimeoutError:
            log.error("Таймаут обращения к mcsrvstat.us (%s сек)", self._cfg.request_timeout)
            return None
        except aiohttp.ClientError as exc:
            log.error("Сеть недоступна: %s", exc)
            return None
        except ValueError as exc:
            log.error("API вернул не-JSON ответ: %s", exc)
            return None

        return self._parse(payload)

    def _parse(self, payload: dict[str, Any]) -> ServerStatus:
        """Разбирает JSON ответа API в ServerStatus."""
        players_data = payload.get("players") or {}
        player_list = players_data.get("list") or []

        return ServerStatus(
            online=bool(payload.get("online")),
            players_online=int(players_data.get("online") or 0),
            players_max=int(players_data.get("max") or 0),
            version=str(payload.get("version") or "").strip(),
            players=[str(p.get("name")) for p in player_list if p.get("name")],
            icon_bytes=self._decode_icon(payload.get("icon")),
            motd=self._parse_motd(payload.get("motd")),
        )

    @staticmethod
    def _parse_motd(motd: Any) -> list[str]:
        """Достаёт строки MOTD - краткого описания сервера.

        API отдаёт три варианта: raw (с кодами §), clean (без них)
        и html. Берём clean, но если его нет - чистим raw сами.
        """
        if not isinstance(motd, dict):
            return []

        lines = motd.get("clean") or []
        if not lines:
            lines = [strip_minecraft_codes(line) for line in (motd.get("raw") or [])]

        result: list[str] = []
        for line in lines:
            if not isinstance(line, str):
                continue
            cleaned = strip_minecraft_codes(line).strip()
            if cleaned:
                result.append(cleaned)
        return result

    @staticmethod
    def _decode_icon(icon: Optional[str]) -> Optional[bytes]:
        """Достаёт PNG-иконку сервера из data-URI вида data:image/png;base64,...."""
        if not icon or "," not in icon:
            return None
        try:
            return base64.b64decode(icon.split(",", 1)[1])
        except (ValueError, TypeError):
            log.warning("Не удалось декодировать иконку сервера")
            return None


# ---------------------------------------------------------------------------
# Server List Ping — запасной способ узнать имена игроков
# ---------------------------------------------------------------------------


class ServerListPing:
    """
    Опрос сервера напрямую по протоколу Server List Ping (без сторонних API).

    Нужен как запасной вариант: API отдаёт полный список игроков только если
    на сервере включён Query-протокол, а SLP — до 12 имён из поля sample.
    Если сервер закрыт или SLP недоступен — возвращается пустой список.
    """

    def __init__(self, timeout: float = 8.0) -> None:
        self._timeout = timeout

    async def fetch_players(self, host: str, port: int) -> list[str]:
        """Возвращает имена игроков (пустой список, если данных нет)."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=self._timeout
            )
        except (OSError, asyncio.TimeoutError) as exc:
            log.debug("SLP: не удалось подключиться к %s:%s (%s)", host, port, exc)
            return []

        try:
            await self._request_status(reader, writer, host, port)
            raw = await asyncio.wait_for(self._read_status(reader), timeout=self._timeout)
        except (OSError, asyncio.TimeoutError, ValueError, asyncio.IncompleteReadError) as exc:
            log.debug("SLP: ошибка обмена с %s:%s (%s)", host, port, exc)
            return []
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.TimeoutError):
                pass

        try:
            data = json.loads(raw)
        except ValueError:
            return []

        sample = (data.get("players") or {}).get("sample") or []
        return [str(p.get("name")) for p in sample if p.get("name")]

    async def _request_status(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, host: str, port: int
    ) -> None:
        """Отправляет handshake + status request."""
        host_bytes = host.encode("idna") if any(ord(c) > 127 for c in host) else host.encode()
        handshake = (
            _varint(0x00)
            + _varint(0xFFFFFFFF)  # protocol version = -1 (версия не важна)
            + _pack_string(host_bytes)
            + struct.pack(">H", port)
            + _varint(0x01)  # next state: status
        )
        writer.write(_varint(len(handshake)) + handshake)
        writer.write(_varint(1) + _varint(0x00))  # status request
        await writer.drain()

    async def _read_status(self, reader: asyncio.StreamReader) -> str:
        """Читает пакет ответа и достаёт JSON-строку."""
        await _read_varint(reader)  # длина пакета
        await _read_varint(reader)  # id пакета
        length = await _read_varint(reader)
        return (await reader.readexactly(length)).decode("utf-8", "replace")


def _varint(value: int) -> bytes:
    """Кодирует число в формате VarInt (Minecraft)."""
    value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


async def _read_varint(reader: asyncio.StreamReader) -> int:
    """Читает VarInt из потока."""
    result = 0
    shift = 0
    for _ in range(5):
        chunk = await reader.readexactly(1)
        byte = chunk[0]
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result
        shift += 7
    raise ValueError("VarInt слишком длинный")


def _pack_string(payload: bytes) -> bytes:
    """Кодирует строку с префиксом-длиной."""
    return _varint(len(payload)) + payload


# ---------------------------------------------------------------------------
# Сборка Embed
# ---------------------------------------------------------------------------


def build_embed(
    status: ServerStatus,
    cfg: Config,
    icon_url: Optional[str] = None,
    theme: Optional[Theme] = None,
) -> discord.Embed:
    """
    Собирает Embed витрины статуса по ТЗ.

    icon_url — ссылка на миниатюру. По умолчанию берётся из конфига
    (своя иконка или ссылка mcsrvstat.us). Бот передаёт сюда
    ``attachment://icon.png``, когда прикладывает настоящую иконку сервера.

    theme — оформление полоски слева. Без неё работают цвета по умолчанию.
    """
    active_theme = theme or Theme()
    color = active_theme.color_for(status.online)

    embed = discord.Embed(
        title=cfg.server_ip,
        description=_description_for(status, cfg),
        color=discord.Color(color),
    )

    if cfg.use_emoji:
        status_value = "🟢 Онлайн" if status.online else "🔴 Оффлайн"
    else:
        status_value = "Онлайн" if status.online else "Оффлайн"

    embed.add_field(name="Status", value=status_value, inline=True)
    embed.add_field(
        name="Players",
        value=f"{status.players_online}/{status.players_max}",
        inline=True,
    )

    if cfg.show_version:
        embed.add_field(
            name="Version",
            value=status.version[:48] if status.version else "—",
            inline=True,
        )

    players_value = _players_value(status, cfg)
    if players_value:
        # Отдельное поле во всю ширину: не ломает ряд из трёх inline-полей.
        embed.add_field(name="Игроки", value=players_value, inline=False)

    embed.set_thumbnail(url=icon_url or cfg.resolved_icon_url)
    embed.set_footer(text=cfg.footer_text)
    embed.timestamp = datetime.now(timezone.utc)
    return embed


def _description_for(status: ServerStatus, cfg: Config) -> str:
    """Текст описания embed.

    Если сервер онлайн и отдал MOTD - показываем его (это то же краткое
    описание, что видно в списке серверов Minecraft). Если MOTD нет,
    берём запасной текст из настроек.
    """
    if cfg.use_motd and status.motd:
        # MOTD может содержать markdown-символы Discord - экранируем.
        lines = [discord.utils.escape_markdown(line) for line in status.motd]
        return "\n".join(lines)[:4096]
    return cfg.description


def _players_value(status: ServerStatus, cfg: Config) -> str:
    """Формирует «маленькое окошко» со списком игроков (спойлер Discord)."""
    if not cfg.show_player_list or not status.online or not status.players:
        return ""

    shown = status.players[: cfg.player_list_limit]
    listed = ", ".join(shown)
    if len(status.players) > cfg.player_list_limit:
        listed += f" и ещё {len(status.players) - cfg.player_list_limit}"

    # ||...|| в Discord — кликабельный спойлер: список свёрнут, пока не нажмёшь.
    return f"||{listed}||"


# ---------------------------------------------------------------------------
# Бот
# ---------------------------------------------------------------------------


class LivecheckBot(commands.Bot):
    """Бот, который держит в канале одно живое сообщение со статусом."""

    def __init__(self, cfg: Config) -> None:
        # MESSAGE_CONTENT нам не нужен: ищем собственное сообщение бота,
        # а содержимое чужих сообщений читать не требуется.
        # Включать его не надо — иначе бот не стартует, пока в Developer Portal
        # не включён привилегированный intent.
        intents = discord.Intents.default()

        super().__init__(
            command_prefix=env_str("COMMAND_PREFIX", "!"),
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

        self.cfg = cfg
        self._session: Optional[aiohttp.ClientSession] = None
        self._api: Optional[MinecraftAPI] = None
        self._slp = ServerListPing(timeout=max(5, cfg.request_timeout / 2))
        self._last_status: Optional[ServerStatus] = None
        #: Последняя известная иконка сервера. Нужна, чтобы при падении сервера
        #: в embed осталась именно его иконка, а не дефолтный кубик.
        self._icon_cache: Optional[bytes] = None
        #: Загружена ли иконка в сообщение. Discord не возвращает вложения
        #: в messages.attachments, если на них ссылается embed, поэтому
        #: факт загрузки запоминаем сами - иначе иконка перезаливается
        #: каждый цикл и плодит новые файлы.
        self._icon_uploaded = False
        #: Оформление полоски слева (настраивается слэш-командой).
        self.theme = Theme.load(cfg.theme_file)
        #: HTTP-сервер проверки живости (для хостингов).
        self._health_runner: Any = None
        # Цвет из переменной окружения важнее файла: на хостинге файлы
        # не сохраняются между перезапусками.
        if cfg.accent_color is not None:
            self.theme.mode = "fixed"
            self.theme.accent = cfg.accent_color

        self._register_commands()

    # -- слэш-команды -------------------------------------------------------

    def _register_commands(self) -> None:
        """Регистрирует команду /livecheck."""
        group = app_commands.Group(
            name="livecheck",
            description="Настройка витрины статуса Minecraft-сервера",
        )

        @group.command(
            name="status",
            description="Показать текущие настройки оформления",
        )
        async def livecheck_status(interaction: discord.Interaction) -> None:
            log.info(
                "Вызвана команда /livecheck status от %s", interaction.user
            )
            if not await self._require_admin(interaction):
                return

            embed = self._settings_embed()
            # ephemeral=True - ответ видит только тот, кто вызвал команду.
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @group.command(
            name="color",
            description="Задать цвет полоски слева",
        )
        @app_commands.describe(
            color="Цвет в формате #2ECC71, 2ecc71 или 0x2ECC71",
            mode=(
                "fixed — один цвет на всё время; "
                "follow_status — зелёный/красный в зависимости от статуса"
            ),
        )
        @app_commands.choices(
            mode=[
                app_commands.Choice(name="Фиксированный (не меняется)", value="fixed"),
                app_commands.Choice(name="По статусу (зелёный/красный)", value="follow_status"),
            ]
        )
        async def livecheck_color(
            interaction: discord.Interaction,
            color: str,
            mode: str = "fixed",
        ) -> None:
            if not await self._require_admin(interaction):
                return

            parsed = parse_hex_color(color)
            if parsed is None:
                await interaction.response.send_message(
                    "❌ Не понял цвет. Укажите `#2ECC71` или название "
                    "(например «зелёный»), либо используйте "
                    "`/livecheck palette` с пипеткой.",
                    ephemeral=True,
                )
                return

            log.info(
                "Вызвана команда /livecheck color от %s: #%06X (%s)",
                interaction.user,
                parsed,
                mode,
            )

            if mode == "fixed":
                self.theme.mode = "fixed"
                self.theme.accent = parsed
            else:
                self.theme.mode = "follow_status"
                # В режиме «по статусу» задаём оба цвета сразу.
                self.theme.online = parsed
                self.theme.offline = parsed

            self.theme.save(self.cfg.theme_file)

            embed = self._settings_embed()
            embed.description = (
                f"✅ Цвет полоски обновлён: `#{parsed:06X}` (режим `{self.theme.mode}`)."
                f"\n{embed.description}"
            )
            embed.color = discord.Color(parsed)
            # ephemeral=True - ответ видит только тот, кто вызвал команду.
            await interaction.response.send_message(embed=embed, ephemeral=True)
            # Сразу применяем цвет к витрине, чтобы результат был виден сразу.
            await self.update_status_now()

        # Подсказки при вводе: Discord показывает список готовых цветов.
        @livecheck_color.autocomplete("color")
        async def color_autocomplete(
            interaction: discord.Interaction,
            current: str,
        ) -> None:
            typed = current.strip().casefold()
            choices: list[app_commands.Choice[str]] = []
            for color, name, emoji in PALETTE:
                if typed and typed not in name.casefold() and typed not in f"{color:06X}":
                    continue
                choices.append(
                    app_commands.Choice(
                        name=f"{emoji} {name} · #{color:06X}",
                        value=f"{color:06X}",
                    )
                )
            if not choices and typed:
                # Не нашли по названию - покажем весь список, пусть выберет.
                choices = [
                    app_commands.Choice(
                        name=f"{emoji} {name} · #{color:06X}",
                        value=f"{color:06X}",
                    )
                    for color, name, emoji in PALETTE
                ]
            await interaction.response.autocomplete(choices=choices[:25])

        @group.command(
            name="reset",
            description="Сбросить цвет полоски к стандартному",
        )
        async def livecheck_reset(interaction: discord.Interaction) -> None:
            if not await self._require_admin(interaction):
                return

            self.theme = Theme()
            self.theme.save(self.cfg.theme_file)

            embed = self._settings_embed()
            embed.description = (
                "✅ Цвета сброшены: зелёный `#2ECC71` / красный `#E74C3C` по статусу сервера."
                f"\n{embed.description}"
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            await self.update_status_now()

        @group.command(
            name="server",
            description="Указать адрес Minecraft-сервера и опубликовать витрину здесь",
        )
        @app_commands.describe(
            ip="Адрес сервера: mc.example.com, 1.2.3.4 или с портом :25565"
        )
        async def livecheck_server(
            interaction: discord.Interaction,
            ip: str,
        ) -> None:
            if not await self._require_admin(interaction):
                return

            address = ip.strip().split()[0] if ip.strip() else ""
            if not address:
                await interaction.response.send_message(
                    "❌ Укажите адрес сервера, например `mc.example.com`.",
                    ephemeral=True,
                )
                return

            log.info(
                "Вызвана команда /livecheck server от %s: %s (канал %s)",
                interaction.user,
                address,
                interaction.channel_id,
            )

            # Проверяем, что сервер вообще отвечает, прежде чем менять.
            status = await self._api.fetch(address)
            if status is None:
                await interaction.response.send_message(
                    "❌ Не удалось проверить сервер. Проверьте адрес и порт.",
                    ephemeral=True,
                )
                return

            old_channel_id = self.cfg.channel_id

            self.cfg.server_ip = address
            # Витрина живёт там, где вызвали команду.
            self.cfg.channel_id = interaction.channel_id
            self.cfg.save()

            # Прежнее сообщение в старом канале убираем, чтобы не висело дублей.
            await self._delete_old_showcase(old_channel_id)
            # Иконка в новом сообщении загрузится заново.
            self._icon_uploaded = False

            emoji = "🟢" if status.online else "🔴"
            word = "Онлайн" if status.online else "Оффлайн"
            embed = self._settings_embed()
            embed.description = (
                f"✅ Сервер изменён на `{address}` — сейчас {emoji} {word} "
                f"({status.players_online}/{status.players_max}).\n"
                f"Витрина опубликована в этом канале.\n"
                f"{embed.description}"
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            await self.update_status_now()

        @group.command(
            name="palette",
            description="Выбрать цвет пипеткой - нажмите на квадратик",
        )
        async def livecheck_palette(interaction: discord.Interaction) -> None:
            if not await self._require_admin(interaction):
                return

            log.info("Вызвана команда /livecheck palette от %s", interaction.user)

            rgb_colors = [
                ((color >> 16) & 255, (color >> 8) & 255, color & 255)
                for color, _, _ in PALETTE
            ]
            png = render_palette_png(rgb_colors)
            palette_file = discord.File(io.BytesIO(png), filename="palette.png")

            embed = discord.Embed(
                title="Палитра цветов",
                description=(
                    "Нажмите на кнопку с нужным цветом — полоска слева у витрины "
                    "сразу перекрасится.\n"
                    f"Можно и вручную: `/livecheck color #{self.theme.accent:06X}`"
                ),
                color=discord.Color(self.theme.accent),
            )
            embed.set_image(url="attachment://palette.png")
            embed.set_footer(text=self.cfg.footer_text)

            await interaction.response.send_message(
                embed=embed,
                file=palette_file,
                view=PaletteView(self),
                ephemeral=True,
            )

        @group.command(
            name="обзор",
            description="Показать все доступные подкоманды",
        )
        async def livecheck_help(interaction: discord.Interaction) -> None:
            embed = discord.Embed(
                title="Команды Livecheck",
                description=(
                    "**`/livecheck server <ip>`** — адрес сервера "
                    "(витрина появится в этом канале)\n"
                    "**`/livecheck palette`** — выбрать цвет пипеткой (нажать на квадратик)\n"
                    "**`/livecheck color <color>`** — задать цвет полоски вручную\n"
                    "**`/livecheck status`** — текущие настройки оформления\n"
                    "**`/livecheck reset`** — вернуть стандартные цвета\n"
                    "**`/livecheck обзор`** — эта справка"
                ),
                color=discord.Color(self.theme.accent),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        self.tree.add_command(group)

    def _settings_embed(self) -> discord.Embed:
        """Собирает embed с текущими настройками витрины."""
        theme = self.theme
        if theme.mode == "fixed":
            color_line = f"Фиксированный цвет полоски: `#{theme.accent:06X}`"
        else:
            color_line = (
                f"Онлайн: `#{theme.online:06X}` · Оффлайн: `#{theme.offline:06X}`"
            )

        embed = discord.Embed(
            title="Настройки витрины",
            description=(
                f"Режим цвета: `{theme.mode}`\n{color_line}\n"
                f"Minecraft-сервер: `{self.cfg.server_ip}`\n"
                f"Канал: <#{self.cfg.channel_id}>\n"
                f"Обновление раз в `{self.cfg.update_interval}` сек."
            ),
            color=discord.Color(theme.color_for(True)),
        )
        embed.set_footer(text=self.cfg.footer_text)
        return embed

    async def _require_admin(self, interaction: discord.Interaction) -> bool:
        """Проверяет право на управление оформлением.

        Достаточно любого из: администратор, управление каналами,
        управление сервером. Если бот не может определить права
        (например, нет интента участников) - не блокируем пользователя.
        """
        permissions = interaction.user.guild_permissions
        if permissions is None:
            # Не смогли определить права - считаем, что доступ есть.
            return True

        if (
            permissions.administrator
            or permissions.manage_channels
            or permissions.manage_guild
        ):
            return True

        await interaction.response.send_message(
            "❌ Нужно право «Управление каналами» или «Администратор».",
            ephemeral=True,
        )
        return False

    async def update_status_now(self) -> None:
        """Обновляет сообщение прямо сейчас, не дожидаясь цикла."""
        try:
            await self._refresh()
        except Exception:  # noqa: BLE001
            log.exception("Не удалось применить настройки")

    async def _delete_old_showcase(self, old_channel_id: int) -> None:
        """Удаляет прежнее сообщение витрины (при переносе в другой канал)."""
        if old_channel_id == self.cfg.channel_id:
            return

        channel = self.get_channel(old_channel_id)
        if channel is None or not hasattr(channel, "history"):
            return

        try:
            async for message in channel.history(limit=20):  # type: ignore[attr-defined]
                if message.author.id != self.user.id:
                    continue
                if self._is_livecheck_message(message):
                    await message.delete()
                    log.info("Прежняя витрина удалена из канала %s", old_channel_id)
                    return
        except discord.NotFound:
            return
        except discord.Forbidden:
            log.warning("Нет прав удалить прежнюю витрину в канале %s", old_channel_id)
        except discord.HTTPException as exc:
            log.warning("Не удалось удалить прежнюю витрину: %s", exc)

    # -- жизненный цикл -----------------------------------------------------

    async def setup_hook(self) -> None:
        """Создаёт HTTP-сессию до старта цикла обновления."""
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.cfg.request_timeout),
            # API требует непустой описательный User-Agent, иначе отдаёт 403.
            headers={"User-Agent": "discord-livecheck/1.0 (+mcsrvstat.us)"},
        )
        self._api = MinecraftAPI(self._session, self.cfg)
        await self._start_health_server()

    async def on_ready(self) -> None:
        """Стартует цикл обновления при подключении."""
        log.info("Бот авторизован как %s", self.user)
        if not self.update_status.is_running():
            # Интервал задаём здесь: в момент определения класса конфиг ещё не прочитан.
            self.update_status.change_interval(seconds=self.cfg.update_interval)
            self.update_status.start()
        await self._sync_commands()
        await self._set_presence(online=False, players=0)

    async def _sync_commands(self) -> None:
        """Регистрирует слэш-команды в Discord.

        Команды нужны только на одном сервере, поэтому регистрируем их
        локально (guild-команды появляются мгновенно), а глобальные
        удаляем - иначе Discord покажет обе копии и в списке будут дубли.

        Важно: sync(guild=...) берёт только серверные команды, поэтому
        сначала нужно copy_global_to(), иначе список уйдёт пустым.

        Перед регистрацией старую запись удаляем: Discord рассылает клиентам
        обновление списка команд, и старый кэш (с дублями) сбрасывается.
        """
        guild = self.get_guild(self.cfg.guild_id)
        if guild is None:
            log.info(
                "Сервер %s пока недоступен, пропускаю регистрацию команд",
                self.cfg.guild_id,
            )
            return

        await self._remove_global_commands()
        await self._remove_guild_commands(guild)

        try:
            self.tree.copy_global_to(guild=guild)
            guild_synced = await self.tree.sync(guild=guild)
            names = ", ".join(getattr(cmd, "name", str(cmd)) for cmd in guild_synced)
            log.info("Слэш-команды на сервере зарегистрированы: %s", names or "—")
        except discord.HTTPException as exc:
            log.warning("Не удалось зарегистрировать слэш-команды на сервере: %s", exc)

    async def _remove_guild_commands(self, guild: discord.Guild) -> None:
        """Удаляет прежнюю запись серверных команд (сброс кэша клиентов)."""
        try:
            commands = await self.http.get_guild_commands(self.application_id, guild.id)
        except discord.HTTPException as exc:
            log.debug("Не удалось получить серверные команды: %s", exc)
            return

        for command in commands:
            command_id = command.get("id") if isinstance(command, dict) else command.id
            name = command.get("name") if isinstance(command, dict) else command.name
            if command_id is None:
                continue
            try:
                await self.http.delete_guild_command(self.application_id, guild.id, command_id)
                log.info("Удалена прежняя запись команды /%s", name)
            except discord.HTTPException as exc:
                log.warning("Не удалось удалить команду /%s: %s", name, exc)

    async def _remove_global_commands(self) -> None:
        """Удаляет глобальные версии команд, чтобы не было дублей."""
        try:
            global_commands = await self.http.get_global_commands(self.application_id)
        except discord.HTTPException as exc:
            log.debug("Не удалось получить глобальные команды: %s", exc)
            return

        if not global_commands:
            return

        for command in global_commands:
            # API возвращает команды обычными словарями.
            command_id = command.get("id") if isinstance(command, dict) else command.id
            name = command.get("name") if isinstance(command, dict) else command.name
            if command_id is None:
                continue
            try:
                await self.http.delete_global_command(self.application_id, command_id)
                log.info("Удалена глобальная копия команды /%s", name)
            except discord.HTTPException as exc:
                log.warning("Не удалось удалить глобальную команду /%s: %s", name, exc)

    async def close(self) -> None:
        """Аккуратно закрывает HTTP-сессию и health-сервер."""
        if self._session and not self._session.closed:
            await self._session.close()
        if self._health_runner is not None:
            await self._health_runner.cleanup()
            self._health_runner = None
        await super().close()

    # -- основной цикл ------------------------------------------------------

    @tasks.loop(seconds=60)
    async def update_status(self) -> None:
        """Раз в UPDATE_INTERVAL секунд обновляет сообщение со статусом."""
        try:
            await self._refresh()
        except discord.HTTPException as exc:
            log.error("Discord отклонил запрос: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - цикл не должен падать никогда
            log.exception("Непредвиденная ошибка при обновлении статуса")

    @update_status.before_loop
    async def before_update_status(self) -> None:
        """Ждёт готовности бота перед первым обновлением."""
        await self.wait_until_ready()

    async def _refresh(self) -> None:
        """Один цикл: получить данные → собрать embed → отправить или отредактировать."""
        assert self._api is not None, "HTTP-сессия не инициализирована"

        status = await self._api.fetch()
        if status is None:
            # Запрос не удался — оставляем прошлые данные, чтобы не мигать оффлайном.
            log.warning("Пропускаю обновление: API недоступен, оставляю прошлый статус")
            return

        if self.cfg.show_player_list and status.online and not status.players:
            # В API список пуст (Query выключен) — пробуем достать имена через SLP.
            status.players = await self._slp.fetch_players(self.cfg.host, self.cfg.port)

        # Иконку запоминаем: сервер может упасть, а показать надо его иконку.
        if status.icon_bytes:
            self._icon_cache = status.icon_bytes

        self._last_status = status
        embed = build_embed(status, self.cfg, self._thumbnail_ref(status), self.theme)

        channel = await self._get_channel()
        if channel is None:
            return

        message = await self._resolve_message(channel)
        if message is None:
            await self._send_new(channel, embed, status)
        else:
            await self._edit_existing(message, embed, status)
        await self._set_presence(status.online, status.players_online)

    def _thumbnail_ref(self, status: ServerStatus) -> Optional[str]:
        """Ссылка на миниатюру: вложение с иконкой сервера либо обычный URL."""
        if self.cfg.icon_url:
            # Пользователь задал свою картинку - уважаем её.
            return self.cfg.icon_url
        if self._icon_cache:
            # Настоящая иконка лежит вложением в сообщение.
            return f"attachment://{ICON_FILENAME}"
        return None

    async def _start_health_server(self) -> None:
        """
        Поднимает крошечный HTTP-сервер для проверки живости.

        Нужен хостингам вроде Render: сервис обязан слушать порт,
        иначе деплой падает. Заодно такие запросы не дают бесплатному
        сервису «заснуть» - он просыпается при входящем трафике.
        """
        port = env_int("PORT", 8080)
        try:
            from aiohttp import web
        except ImportError:  # pragma: no cover
            return

        async def health(request: web.Request) -> web.Response:
            status = self._last_status
            body = {
                "ok": True,
                "bot": str(self.user),
                "server": self.cfg.server_ip,
                "online": status.online if status else None,
                "players": f"{status.players_online}/{status.players_max}" if status else None,
            }
            return web.json_response(body)

        async def root(request: web.Request) -> web.Response:
            return web.Response(text="Livecheck bot is running\n")

        app = web.Application()
        app.router.add_get("/", root)
        app.router.add_get("/health", health)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        try:
            await site.start()
        except OSError as exc:
            # Порт занят или недоступен - не критично, бот работает и без него.
            log.warning("Не удалось поднять health-сервер на порту %s: %s", port, exc)
            return
        log.info("Health-сервер запущен на порту %s", port)
        self._health_runner = runner

    async def _get_channel(self) -> Optional[discord.abc.Messageable]:
        """Находит канал для сообщения."""
        channel = self.get_channel(self.cfg.channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(self.cfg.channel_id)
            except discord.NotFound:
                log.error("Канал %s не найден. Проверьте CHANNEL_ID в .env", self.cfg.channel_id)
                return None
            except discord.Forbidden:
                log.error(
                    "Нет доступа к каналу %s. Выдайте боту права в канале", self.cfg.channel_id
                )
                return None
            except discord.HTTPException as exc:
                log.error("Не удалось получить канал %s: %s", self.cfg.channel_id, exc)
                return None
        return channel

    async def _resolve_message(
        self, channel: discord.abc.Messageable
    ) -> Optional[discord.Message]:
        """Ищет последнее сообщение бота в канале (для редактирования)."""
        if not hasattr(channel, "history"):
            log.error("CHANNEL_ID указывает не на текстовый канал")
            return None

        try:
            async for message in channel.history(limit=50):  # type: ignore[attr-defined]
                if message.author.id != self.user.id:
                    continue
                if self._is_livecheck_message(message):
                    # Иконка могла остаться в сообщении с прошлого запуска бота.
                    self._icon_uploaded = self._has_uploaded_icon(message)
                    return message
        except discord.Forbidden:
            log.error(
                "Нет прав на историю сообщений. Выдайте боту право «Чтение истории сообщений»"
            )
            return None
        except discord.HTTPException as exc:
            log.error("Не удалось прочитать историю канала: %s", exc)
            return None
        return None

    def _is_livecheck_message(self, message: discord.Message) -> bool:
        """Отличает наше сообщение от любых других сообщений бота."""
        for embed in message.embeds:
            if embed.footer and embed.footer.text == self.cfg.footer_text:
                return True
            if embed.title == self.cfg.server_ip:
                return True
        return False

    def _has_uploaded_icon(self, message: discord.Message) -> bool:
        """Проверяет, загружена ли иконка в это сообщение.

        Discord не отдаёт вложения в messages.attachments, если на них
        ссылается embed, поэтому смотрим на саму миниатюру.
        """
        for embed in message.embeds:
            thumbnail = embed.thumbnail
            if thumbnail and thumbnail.url and ICON_FILENAME in thumbnail.url:
                return True
        return False

    async def _send_new(
        self,
        channel: discord.abc.Messageable,
        embed: discord.Embed,
        status: ServerStatus,
    ) -> None:
        """Отправляет новое сообщение, если старого нет."""
        try:
            if self._icon_cache and not self.cfg.icon_url:
                icon = discord.File(io.BytesIO(self._icon_cache), filename=ICON_FILENAME)
                message = await channel.send(embed=embed, file=icon)  # type: ignore[attr-defined]
                self._icon_uploaded = True
            else:
                message = await channel.send(embed=embed)  # type: ignore[attr-defined]
        except discord.Forbidden:
            log.error("Нет прав на отправку сообщений в канале %s", self.cfg.channel_id)
            return
        except discord.HTTPException as exc:
            log.error("Не удалось отправить сообщение: %s", exc)
            return

        log.info("Отправлено новое сообщение со статусом (%s)", message.id)

    async def _edit_existing(
        self,
        message: discord.Message,
        embed: discord.Embed,
        status: ServerStatus,
    ) -> None:
        """Редактирует уже существующее сообщение."""
        try:
            if self._icon_cache and not self.cfg.icon_url:
                # Иконку держим вложением: Discord не умеет показывать data-URI.
                # Если вложение ещё не загружено (например, сервер был оффлайн
                # при старте) - добавляем его. Иначе просто редактируем embed:
                # ссылка attachment://icon.png продолжает указывать на файл.
                if not self._icon_uploaded:
                    icon = discord.File(io.BytesIO(self._icon_cache), filename=ICON_FILENAME)
                    await message.edit(embed=embed, attachments=[icon])
                    self._icon_uploaded = True
                else:
                    await message.edit(embed=embed)
            else:
                await message.edit(embed=embed)
        except discord.NotFound:
            log.info("Сообщение было удалено — отправляю новое")
            self._icon_uploaded = False
            channel = await self._get_channel()
            if channel is not None:
                await self._send_new(channel, embed, status)
        except discord.Forbidden:
            log.error("Нет прав на редактирование сообщения в канале %s", self.cfg.channel_id)
        except discord.HTTPException as exc:
            log.error("Не удалось обновить сообщение: %s", exc)

    async def _set_presence(self, online: bool, players: int) -> None:
        """Показывает статус сервера в «статусе» профиля бота."""
        state = "онлайн" if online else "оффлайн"
        try:
            await self.change_presence(
                activity=discord.Game(name=f"{self.cfg.server_ip} — {state}, {players} игроков")
            )
        except discord.HTTPException as exc:
            log.debug("Не удалось обновить статус профиля: %s", exc)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------


def main() -> None:
    """Точка входа: читает .env, поднимает бота."""
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"[ОШИБКА] {exc}", file=sys.stderr)
        print(
            "Подсказка: скопируйте .env.example в .env и заполните значения.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    # Нам не нужен Message Content Intent (мы читаем только свои сообщения),
    # поэтому предупреждение discord.py об этом просто отключаем.
    logging.getLogger("discord.ext.commands.bot").setLevel(logging.ERROR)
    log.info(
        "Сервер: %s | канал: %s | интервал: %s c",
        cfg.server_ip,
        cfg.channel_id,
        cfg.update_interval,
    )

    bot = LivecheckBot(cfg)
    try:
        bot.run(cfg.token, log_handler=None)
    except discord.LoginFailure:
        log.error("Токен отклонён Discord. Проверьте BOT_TOKEN в .env")
    except discord.HTTPException as exc:
        log.error("Ошибка подключения к Discord: %s", exc)
    except KeyboardInterrupt:
        log.info("Остановлено пользователем")


if __name__ == "__main__":
    main()
