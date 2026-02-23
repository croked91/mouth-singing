"""Russian nickname generator for karaoke participants.

Generates funny, karaoke-friendly nicknames in the form
"АдъективноеСуществительное" (adjective + noun, CamelCase).

With ~50 adjectives and ~50 nouns there are 2500 unique combinations,
which is more than enough for any single session.
"""

import random

_ADJECTIVES: list[str] = [
    "Лихой", "Весёлый", "Дерзкий", "Хитрый", "Бравый",
    "Шустрый", "Задорный", "Бодрый", "Яркий", "Крутой",
    "Озорной", "Пылкий", "Модный", "Дикий", "Рыжий",
    "Тихий", "Громкий", "Ленивый", "Мощный", "Быстрый",
    "Хмурый", "Смелый", "Нежный", "Пушистый", "Ловкий",
    "Звонкий", "Сонный", "Чёткий", "Суровый", "Залётный",
    "Молниеносный", "Душевный", "Упоротый", "Свирепый", "Зубастый",
    "Пафосный", "Кудрявый", "Полосатый", "Лунный", "Отважный",
    "Невозмутимый", "Пламенный", "Железный", "Легендарный", "Космический",
    "Таинственный", "Мятежный", "Вечный", "Золотой", "Атомный",
]

_NOUNS: list[str] = [
    "Котяра", "Ёжик", "Пингвин", "Бобёр", "Барсук",
    "Медведь", "Лось", "Кабан", "Хомяк", "Бурундук",
    "Попугай", "Фламинго", "Тукан", "Кальмар", "Осьминог",
    "Единорог", "Дракон", "Крокодил", "Жираф", "Кенгуру",
    "Пельмень", "Самовар", "Бублик", "Валенок", "Чайник",
    "Компот", "Баян", "Кактус", "Блинчик", "Пирожок",
    "Батон", "Шпротик", "Фонарь", "Якорь", "Ракета",
    "Астронавт", "Танкист", "Пират", "Ниндзя", "Самурай",
    "Снежок", "Гром", "Вулкан", "Комета", "Метеорит",
    "Титан", "Феникс", "Мустанг", "Варяг", "Викинг",
]


def generate_nickname(existing_names: set[str] | None = None) -> str:
    """Generate a funny Russian nickname unique within the given set.

    Combines a random adjective and noun in CamelCase, e.g. "ЛихойПингвин".
    Tries up to 50 random combinations before falling back to adding a
    numeric suffix to guarantee uniqueness.

    Args:
        existing_names: Names already taken in the current session.
            Pass ``None`` (or omit) if uniqueness is not required.

    Returns:
        A unique nickname string.
    """
    if existing_names is None:
        existing_names = set()

    # Try random combos — 2500 possibilities, so 50 attempts is very safe.
    for _ in range(50):
        name = random.choice(_ADJECTIVES) + random.choice(_NOUNS)
        if name not in existing_names:
            return name

    # Fallback: append an incrementing number until we find a free slot.
    base = random.choice(_ADJECTIVES) + random.choice(_NOUNS)
    for suffix in range(2, 1000):
        name = f"{base}{suffix}"
        if name not in existing_names:
            return name

    # Practically unreachable — only if every possible name is taken.
    return base
