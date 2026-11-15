import math
import aiohttp
import aiosqlite
from tariffs_by import BELARUS_CITIES

DB_PATH = "geo_cache_by.db"
ROAD_FACTOR = 1.24  # Поправка на трассы М1/М4/М5 и др.


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cities_by (
                name TEXT PRIMARY KEY,
                lat REAL NOT NULL,
                lon REAL NOT NULL
            )
        """)
        await db.commit()


def clean_city_name(name: str) -> str:
    return name.strip().lower().replace("ё", "е")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


async def find_belarus_city(raw_name: str) -> tuple[str, tuple[float, float]] | None:
    city = clean_city_name(raw_name)
    display_name = raw_name.strip().title()

    # 1. Ищем в быстром словаре
    if city in BELARUS_CITIES:
        return display_name, BELARUS_CITIES[city]

    # 2. Ищем в локальном кэше SQLite
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT lat, lon FROM cities_by WHERE name = ?", (city,)) as cur:
            row = await cur.fetchone()
            if row:
                return display_name, (row[0], row[1])

    # 3. Если город редкий (например, Дзержинск, Несвиж, Браслав) — бесплатно ищем через OpenStreetMap
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": city,
        "countrycodes": "by",  # Ищем строго по Беларуси!
        "format": "json",
        "limit": 1,
        "accept-language": "ru",
    }
    headers = {"User-Agent": "BelarusLogisticsBot/1.0"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=headers, timeout=6) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data:
                        lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute(
                                "INSERT OR REPLACE INTO cities_by (name, lat, lon) VALUES (?, ?, ?)",
                                (city, lat, lon),
                            )
                            await db.commit()
                        return display_name, (lat, lon)
    except Exception:
        return None

    return None


async def calc_distance_km(c1: tuple[float, float], c2: tuple[float, float]) -> int:
    # Если отправление и получение в одном городе
    straight = haversine_km(c1[0], c1[1], c2[0], c2[1])
    if straight < 5:
        return 15

    # Пробуем бесплатный дорожный маршрутизатор OSRM
    osrm_url = (
        f"https://router.project-osrm.org/route/v1/driving/"
        f"{c1[1]},{c1[0]};{c2[1]},{c2[0]}?overview=false"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(osrm_url, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("code") == "Ok" and data.get("routes"):
                        return max(15, round(data["routes"][0]["distance"] / 1000))
    except Exception:
        pass

    # Резервный расчёт, если сервис карт временно не ответил
    return max(15, round(straight * ROAD_FACTOR))