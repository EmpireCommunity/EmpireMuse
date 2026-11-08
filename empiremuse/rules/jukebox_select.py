"""Plano de páginas do select da Jukebox, sem I/O (T-435)."""

from typing import NamedTuple

SELECT_CAPACITY = 25


class Page(NamedTuple):
    page: int
    pages: int
    start: int
    stop: int
    previous: int | None
    next: int | None


def plan(total: int, fixed: int, page: int) -> Page:
    """Fatias sem sobreposição; fixos só na primeira página, controles cabem nas 25 vagas."""
    if total < 0 or not 0 <= fixed <= SELECT_CAPACITY - 2:
        raise ValueError("total ou quantidade de entradas fixas inválido")
    spans = []
    start = 0
    capacity = SELECT_CAPACITY - fixed
    while total - start > capacity:
        stop = start + capacity - 1
        spans.append((start, stop))
        start = stop
        capacity = SELECT_CAPACITY - 1  # página anterior
    spans.append((start, total))
    number = max(1, min(page, len(spans)))
    start, stop = spans[number - 1]
    return Page(
        number, len(spans), start, stop, number - 1 if number > 1 else None, number + 1 if number < len(spans) else None
    )
