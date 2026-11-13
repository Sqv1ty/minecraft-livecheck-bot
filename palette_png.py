"""
Генератор PNG-палитры без внешних зависимостей.

Рисует сетку квадратов заданных цветов и пишет готовый PNG.
Нужен для картинки-палитры в Embed (Discord умеет показывать вложение).
"""

from __future__ import annotations

import struct
import zlib


def _chunk(tag: bytes, data: bytes) -> bytes:
    """Один PNG-чанк: длина, тип, данные, CRC."""
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def render_palette_png(
    colors: list[tuple[int, int, int]],
    cell: int = 56,
    columns: int = 8,
    gap: int = 6,
) -> bytes:
    """
    Рисует палитру и возвращает байты PNG.

    colors — список цветов в формате (R, G, B).
    cell — сторона одного квадрата в пикселях.
    columns — сколько квадратов в ряду.
    gap — отступ между квадратами.
    """
    if not colors:
        colors = [(43, 45, 49)]

    rows = (len(colors) + columns - 1) // columns
    width = columns * cell + (columns + 1) * gap
    height = rows * cell + (rows + 1) * gap

    # Фон картинки - тёмно-серый, как у Discord.
    background = (43, 45, 49)
    pixels = bytearray()
    for y in range(height):
        pixels.append(0)  # PNG-фильтр: None
        for x in range(width):
            pixels.extend(background)

    def fill_rect(x0: int, y0: int, x1: int, y1: int, rgb: tuple[int, int, int]) -> None:
        for y in range(y0, y1):
            row_start = 1 + y * (width * 3 + 1)
            for x in range(x0, x1):
                offset = row_start + x * 3
                pixels[offset : offset + 3] = bytes(rgb)

    for index, rgb in enumerate(colors):
        row, column = divmod(index, columns)
        x0 = gap + column * (cell + gap)
        y0 = gap + row * (cell + gap)
        fill_rect(x0, y0, x0 + cell, y0 + cell, rgb)

    raw = zlib.compress(bytes(pixels), 9)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", raw)
        + _chunk(b"IEND", b"")
    )
