#!/usr/bin/env python3
"""
TCDD YHT empty-seat tracker.

Checks one or more route/date searches and sends a Telegram message when
available seats are reported by the TCDD web API.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import logging
import os
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


DEFAULT_TCDD_API_URL = (
    "https://web-api-prod-ytp.tcddtasimacilik.gov.tr/tms/train/train-availability"
)
DEFAULT_SEARCHES_FILE = "searches.json"

DEFAULT_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "tr",
    "Origin": "https://ebilet.tcddtasimacilik.gov.tr",
    "Referer": "https://ebilet.tcddtasimacilik.gov.tr/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15"
    ),
}

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
log = logging.getLogger("tcdd-bot")


@dataclass(frozen=True)
class Search:
    kalkis: str
    varis: str
    tarih: str
    min_saat: Optional[str] = None
    max_saat: Optional[str] = None
    kalkis_id: Optional[int] = None
    varis_id: Optional[int] = None
    kalkis_api_adi: Optional[str] = None
    varis_api_adi: Optional[str] = None
    search_type: str = "DOMESTIC"
    bl_train_types: Optional[List[str]] = None
    cabin_names: Optional[List[str]] = None

    @property
    def key(self) -> str:
        return f"{self.kalkis}->{self.varis}@{self.tarih}"


class ConfigError(RuntimeError):
    pass


class TCDDAPIError(RuntimeError):
    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"TCDD API status {status_code}: {body}")


def load_dotenv(path: Path) -> None:
    """Small .env loader so this script does not need python-dotenv."""
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def get_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} tam sayı olmalı: {value}") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} pozitif olmalı: {value}")
    return parsed


def validate_date(value: str) -> None:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ConfigError(f"Tarih YYYY-MM-DD formatında olmalı: {value}") from exc


def validate_hhmm(value: Optional[str], field_name: str) -> None:
    if value is None:
        return
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError as exc:
        raise ConfigError(f"{field_name} HH:MM formatında olmalı: {value}") from exc


def optional_int(value: Any, field_name: str) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field_name} tam sayı olmalı: {value}") from exc


def optional_str_list(value: Any, field_name: str) -> Optional[List[str]]:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{field_name} string listesi olmalı.")
    return value


def load_searches(path: Path) -> List[Search]:
    if not path.exists():
        raise ConfigError(
            f"{path} bulunamadı. Takip edilecek seferleri bu JSON dosyasına yaz."
        )

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} geçerli JSON değil: {exc}") from exc

    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{path} boş olmayan bir liste olmalı.")

    searches: List[Search] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"{path} içindeki {index}. kayıt obje olmalı.")

        missing = [key for key in ("kalkis", "varis", "tarih") if not item.get(key)]
        if missing:
            raise ConfigError(f"{index}. aramada eksik alan var: {', '.join(missing)}")

        validate_date(item["tarih"])
        validate_hhmm(item.get("min_saat"), "min_saat")
        validate_hhmm(item.get("max_saat"), "max_saat")

        searches.append(
            Search(
                kalkis=item["kalkis"],
                varis=item["varis"],
                tarih=item["tarih"],
                min_saat=item.get("min_saat"),
                max_saat=item.get("max_saat"),
                kalkis_id=optional_int(
                    item.get("kalkis_id", item.get("departureStationId")),
                    "kalkis_id",
                ),
                varis_id=optional_int(
                    item.get("varis_id", item.get("arrivalStationId")),
                    "varis_id",
                ),
                kalkis_api_adi=item.get(
                    "kalkis_api_adi",
                    item.get("departureStationName"),
                ),
                varis_api_adi=item.get(
                    "varis_api_adi",
                    item.get("arrivalStationName"),
                ),
                search_type=item.get("search_type", item.get("searchType", "DOMESTIC")),
                bl_train_types=optional_str_list(
                    item.get("bl_train_types", item.get("blTrainTypes")),
                    "bl_train_types",
                ),
                cabin_names=optional_str_list(
                    item.get("cabin_names", item.get("cabinNames", ["EKONOMİ"])),
                    "cabin_names",
                ),
            )
        )

    return searches


def tcdd_departure_date(value: str) -> str:
    local_midnight = datetime.strptime(value, "%Y-%m-%d")
    # The web app sends local midnight in Turkey as a UTC-like timestamp string.
    api_date = local_midnight - timedelta(hours=3)
    return api_date.strftime("%d-%m-%Y %H:%M:%S")


def build_payload(search: Search) -> Dict[str, Any]:
    if search.kalkis_id is not None and search.varis_id is not None:
        route = {
            "departureStationId": search.kalkis_id,
            "departureStationName": search.kalkis_api_adi or search.kalkis,
            "arrivalStationId": search.varis_id,
            "arrivalStationName": search.varis_api_adi or search.varis,
            "departureDate": tcdd_departure_date(search.tarih),
        }
    else:
        route = {
            "departureStation": search.kalkis,
            "arrivalStation": search.varis,
            "departureDate": f"{search.tarih} 00:00:00",
        }

    payload = {
        "searchRoutes": [route],
        "passengerTypeCounts": [{"id": 0, "count": 1}],
        "searchReservation": False,
    }
    if search.kalkis_id is not None and search.varis_id is not None:
        payload["searchType"] = search.search_type
        payload["blTrainTypes"] = search.bl_train_types or ["TURISTIK_TREN"]
    return payload


def first_int(*values: Any) -> int:
    for value in values:
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def truthy_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def decode_jwt_payload(token: str) -> Optional[Dict[str, Any]]:
    token = token.strip()
    if token.lower().startswith("bearer "):
        token = token.split(None, 1)[1]

    parts = token.split(".")
    if len(parts) < 2:
        return None

    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8")
        data = json.loads(decoded)
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def expired_authorization_message(now: Optional[datetime] = None) -> Optional[str]:
    authorization = os.getenv("TCDD_AUTHORIZATION", "").strip()
    if not authorization:
        return None

    payload = decode_jwt_payload(authorization)
    if not payload or payload.get("exp") is None:
        return None

    try:
        expires_at = datetime.fromtimestamp(int(payload["exp"]), timezone.utc)
    except (TypeError, ValueError, OSError):
        return None

    current_time = now or datetime.now(timezone.utc)
    if expires_at > current_time:
        return None

    return (
        "TCDD_AUTHORIZATION süresi dolmuş "
        f"({expires_at.strftime('%Y-%m-%d %H:%M UTC')}). "
        "ebilet.tcddtasimacilik.gov.tr > Network > train-availability "
        "isteğinden güncel Authorization header'ını alıp .env.local ve "
        "Vercel Environment Variables içine tekrar ekle."
    )


def epoch_to_local_hhmm(value: float) -> Optional[str]:
    if value > 10_000_000_000:
        value = value / 1000
    if value > 1_000_000_000:
        local_time = datetime.utcfromtimestamp(value) + timedelta(hours=3)
        return local_time.strftime("%H:%M")
    return None


def extract_hhmm(value: Any) -> Optional[str]:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return epoch_to_local_hhmm(float(value))

    value = str(value)
    if value.isdigit():
        converted = epoch_to_local_hhmm(float(value))
        if converted:
            return converted
        if len(value) <= 5:
            seconds = int(value)
            if 0 <= seconds < 24 * 60 * 60:
                return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"

    if "T" in value:
        return value.split("T", 1)[1][:5]
    if len(value) >= 5 and value[2] == ":":
        return value[:5]

    for fmt in ("%Y-%m-%d %H:%M:%S", "%b %d, %Y %I:%M:%S %p"):
        try:
            return datetime.strptime(value, fmt).strftime("%H:%M")
        except ValueError:
            pass
    return None


def is_in_time_window(hhmm: Optional[str], search: Search) -> bool:
    if hhmm is None:
        return True
    if search.min_saat and hhmm < search.min_saat:
        return False
    if search.max_saat and hhmm > search.max_saat:
        return False
    return True


def station_id_matches(value: Any, station_id: Optional[int]) -> bool:
    if station_id is None:
        return False
    try:
        return int(value) == station_id
    except (TypeError, ValueError):
        return False


def shorten_response_body(text: str, limit: int = 300) -> str:
    return " ".join(text.split())[:limit]


def iter_trains(data: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    """Yield train dictionaries from the current web API response shape."""
    train_legs = data.get("trainLegs") or []
    if not isinstance(train_legs, list):
        return

    for leg in train_legs:
        if not isinstance(leg, dict):
            continue
        availabilities = leg.get("trainAvailabilities") or []
        if not isinstance(availabilities, list):
            continue
        for availability in availabilities:
            if not isinstance(availability, dict):
                continue
            trains = availability.get("trains")
            if isinstance(trains, list):
                for train in trains:
                    if isinstance(train, dict):
                        yield train
            elif "cabinClassAvailabilities" in availability:
                yield availability


def cabin_name(cabin: Dict[str, Any]) -> str:
    cabin_class = cabin.get("cabinClass")
    if isinstance(cabin_class, dict):
        return str(cabin_class.get("name") or cabin_class.get("description") or "?")
    return str(cabin.get("name") or cabin.get("cabinClassName") or "?")


def normalize_cabin_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    without_marks = "".join(
        char for char in normalized if not unicodedata.combining(char)
    )
    return without_marks.upper()


def cabin_matches_filters(cabin: Dict[str, Any], filters: Optional[List[str]]) -> bool:
    if not filters:
        return True
    normalized_name = normalize_cabin_text(cabin_name(cabin))
    return any(normalize_cabin_text(filter_value) in normalized_name for filter_value in filters)


def is_accessible_cabin(cabin: Dict[str, Any]) -> bool:
    cabin_class = cabin.get("cabinClass")
    if isinstance(cabin_class, dict):
        name = str(cabin_class.get("name") or "").upper()
        code = str(cabin_class.get("code") or "").upper()
    else:
        name = str(cabin.get("name") or cabin.get("cabinClassName") or "").upper()
        code = str(cabin.get("code") or "").upper()
    return "TEKERLEK" in name or code == "DSB"


def iter_cabins(train: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    fare_infos = train.get("availableFareInfo") or []
    if isinstance(fare_infos, list):
        for fare_info in fare_infos:
            if not isinstance(fare_info, dict):
                continue
            cabins = fare_info.get("cabinClasses") or []
            if isinstance(cabins, list):
                for cabin in cabins:
                    if isinstance(cabin, dict):
                        yield cabin

    cabins = train.get("cabinClassAvailabilities") or []
    if isinstance(cabins, list):
        for cabin in cabins:
            if isinstance(cabin, dict):
                yield cabin


def segment_departure_station_id(segment: Dict[str, Any]) -> Any:
    if "departureStationId" in segment:
        return segment.get("departureStationId")
    nested = segment.get("segment")
    if isinstance(nested, dict):
        station = nested.get("departureStation")
        if isinstance(station, dict):
            return station.get("id")
    return None


def train_departure_time(train: Dict[str, Any], search: Optional[Search] = None) -> Any:
    segments = train.get("segments") or []
    if isinstance(segments, list):
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            if station_id_matches(segment_departure_station_id(segment), search.kalkis_id if search else None):
                return (
                    segment.get("departureTime")
                    or segment.get("departureDate")
                    or segment.get("departureDateTime")
                    or ""
                )

    train_segments = train.get("trainSegments") or []
    if isinstance(train_segments, list):
        for segment in train_segments:
            if not isinstance(segment, dict):
                continue
            if station_id_matches(segment.get("departureStationId"), search.kalkis_id if search else None):
                return (
                    segment.get("departureTime")
                    or segment.get("departureDate")
                    or segment.get("departureDateTime")
                    or ""
                )

    direct = (
        train.get("departureTime")
        or train.get("departureDate")
        or train.get("departureDateTime")
        or ""
    )
    if direct:
        return direct

    if segments and isinstance(segments[0], dict):
        return (
            segments[0].get("departureTime")
            or segments[0].get("departureDate")
            or segments[0].get("departureDateTime")
            or ""
        )
    return ""


def format_time_for_message(value: Any) -> str:
    hhmm = extract_hhmm(value)
    if hhmm:
        return hhmm
    value = str(value)
    return value[-8:-3] if len(value) > 5 else value


def parse_availability(data: Dict[str, Any], search: Search) -> List[Dict[str, Any]]:
    available: List[Dict[str, Any]] = []
    seen: Dict[Tuple[str, Any, str], Dict[str, Any]] = {}
    include_accessible = truthy_env("INCLUDE_ACCESSIBLE_SEATS", False)

    for train in iter_trains(data):
        dep_time = train_departure_time(train, search)
        hhmm = extract_hhmm(dep_time)
        if not is_in_time_window(hhmm, search):
            continue

        train_name = str(
            train.get("commercialName")
            or train.get("name")
            or train.get("trainName")
            or train.get("trainNo")
            or "Tren"
        )

        for cabin in iter_cabins(train):
            if is_accessible_cabin(cabin) and not include_accessible:
                continue
            if not cabin_matches_filters(cabin, search.cabin_names or ["EKONOMİ"]):
                continue
            count = first_int(
                cabin.get("availabilityCount"),
                cabin.get("availability"),
                cabin.get("availableSeatCount"),
                cabin.get("availableSeats"),
                cabin.get("seatCount"),
            )
            if count > 0:
                key = (train_name, dep_time, cabin_name(cabin))
                current = seen.get(key)
                if current is None or count > current["bos_koltuk"]:
                    seen[key] = {
                        "tren": train_name,
                        "kalkis_saat": dep_time,
                        "sinif": cabin_name(cabin),
                        "bos_koltuk": count,
                    }

    available.extend(seen.values())
    return available


class TCDDClient:
    def __init__(
        self,
        api_url: str,
        headers: Dict[str, str],
        params: Optional[Dict[str, str]] = None,
        timeout: int = 20,
    ) -> None:
        self.api_url = api_url
        self.headers = headers
        self.params = params or {}
        self.timeout = timeout
        self.session = requests.Session()

    def query(self, search: Search) -> List[Dict[str, Any]]:
        response = self.session.post(
            self.api_url,
            params=self.params,
            json=build_payload(search),
            headers=self.headers,
            timeout=self.timeout,
        )

        if response.status_code != 200:
            raise TCDDAPIError(
                response.status_code,
                shorten_response_body(response.text),
            )

        try:
            data = response.json()
        except ValueError:
            raise TCDDAPIError(200, shorten_response_body(response.text))

        if not isinstance(data, dict):
            log.warning("TCDD API beklenmeyen cevap tipi döndü: %s", type(data).__name__)
            return []

        return parse_availability(data, search)


class TelegramClient:
    def __init__(self, token: str, chat_id: str, dry_run: bool = False) -> None:
        self.token = token
        self.chat_id = chat_id
        self.dry_run = dry_run
        self.session = requests.Session()
        self.last_error = ""

    def send(self, message: str) -> bool:
        self.last_error = ""
        if self.dry_run:
            log.info("DRY-RUN Telegram mesajı:\n%s", message)
            return True

        if not self.token or not self.chat_id:
            self.last_error = "Telegram token/chat_id boş; mesaj gönderilmedi."
            log.warning(self.last_error)
            return False

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            response = self.session.post(url, json=payload, timeout=15)
            if response.status_code != 200:
                self.last_error = f"Telegram hata: {response.status_code} {response.text}"
                log.error(self.last_error)
                return False
            return True
        except requests.RequestException as exc:
            self.last_error = f"Telegram gönderim hatası: {exc}"
            log.error(self.last_error)
            return False


def build_headers() -> Dict[str, str]:
    headers = dict(DEFAULT_HEADERS)
    headers["unit-id"] = os.getenv("TCDD_UNIT_ID", "3895")
    headers["Accept-Language"] = os.getenv("TCDD_ACCEPT_LANGUAGE", headers["Accept-Language"])
    headers["User-Agent"] = os.getenv("TCDD_USER_AGENT", headers["User-Agent"])

    authorization = os.getenv("TCDD_AUTHORIZATION", "").strip()
    if authorization:
        headers["Authorization"] = authorization

    cookie = os.getenv("TCDD_COOKIE", "").strip()
    if cookie:
        headers["Cookie"] = cookie

    extra_headers = os.getenv("TCDD_EXTRA_HEADERS_JSON", "").strip()
    if extra_headers:
        try:
            parsed = json.loads(extra_headers)
            if isinstance(parsed, dict):
                headers.update({str(key): str(value) for key, value in parsed.items()})
            else:
                log.warning("TCDD_EXTRA_HEADERS_JSON obje olmalı; yok sayıldı.")
        except json.JSONDecodeError as exc:
            log.warning("TCDD_EXTRA_HEADERS_JSON geçerli JSON değil; yok sayıldı: %s", exc)
    return headers


def build_query_params() -> Dict[str, str]:
    return {
        "environment": os.getenv("TCDD_ENVIRONMENT", "dev"),
        "userId": os.getenv("TCDD_USER_ID", "1"),
    }


def build_message(search: Search, results: List[Dict[str, Any]]) -> str:
    lines = [
        "<b>EKONOMI BOS KOLTUK!</b>",
        f"{html.escape(search.kalkis)} -> {html.escape(search.varis)}",
        f"Tarih: {html.escape(search.tarih)}",
        "",
    ]

    for result in results[:10]:
        train_name = html.escape(str(result["tren"]))
        dep_time = html.escape(format_time_for_message(str(result["kalkis_saat"])))
        cabin = html.escape(str(result["sinif"]))
        count = html.escape(str(result["bos_koltuk"]))
        lines.append(f"- <b>{train_name}</b> - {dep_time}\n  {cabin}: {count} koltuk")

    if len(results) > 10:
        lines.append(f"\n... ve {len(results) - 10} kayıt daha")

    lines.append("\nhttps://ebilet.tcddtasimacilik.gov.tr")
    return "\n".join(lines)


def run_cycle(
    searches: List[Search],
    tcdd: TCDDClient,
    telegram: TelegramClient,
    last_notified: Dict[str, float],
    notification_cooldown: int,
    pause_between_searches: int,
) -> None:
    for search in searches:
        log.info("Sorgu: %s", search.key)
        query_failed = False
        try:
            results = tcdd.query(search)
        except TCDDAPIError as exc:
            query_failed = True
            log.error("TCDD API cevap vermedi: %s", exc)
            if exc.status_code in (401, 403):
                log.error(
                    "Bu boş koltuk yok demek değil. TCDD isteği engellendi; "
                    ".env.local içine güncel TCDD_AUTHORIZATION ve gerekirse "
                    "TCDD_COOKIE eklenmeli."
                )
                auth_error = expired_authorization_message()
                if auth_error:
                    log.error(auth_error)
            results = []
        except requests.RequestException as exc:
            query_failed = True
            log.error("TCDD ağ hatası: %s", exc)
            results = []
        except Exception:
            query_failed = True
            log.exception("TCDD sorgusunda beklenmeyen hata")
            results = []

        if results:
            log.info("%d uygun kayıt bulundu", len(results))
            now = time.time()
            if now - last_notified.get(search.key, 0) >= notification_cooldown:
                telegram.send(build_message(search, results))
                last_notified[search.key] = now
            else:
                log.info("Bildirim cooldown içinde; mesaj atlanıyor.")
        else:
            if query_failed:
                log.info("Sorgu tamamlanamadı.")
            else:
                log.info("Uygun normal koltuk bulunmadı.")

        if pause_between_searches > 0:
            time.sleep(pause_between_searches)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TCDD bos koltuk takip botu")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Tek tur sorgu yapıp çık.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Telegram mesajını gerçekten gönderme, log'a yaz.",
    )
    parser.add_argument(
        "--no-start-message",
        action="store_true",
        help="Başlangıç Telegram mesajını gönderme.",
    )
    parser.add_argument(
        "--searches-file",
        default=os.getenv("SEARCHES_FILE", DEFAULT_SEARCHES_FILE),
        help=f"Arama JSON dosyası. Varsayılan: {DEFAULT_SEARCHES_FILE}",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt="%H:%M:%S")
    load_dotenv(Path(".env"))
    load_dotenv(Path(".env.local"))
    args = parse_args(argv)

    try:
        searches = load_searches(Path(args.searches_file))
        check_interval = get_int_env("CHECK_INTERVAL", 120)
        notification_cooldown = get_int_env("NOTIFICATION_COOLDOWN", 1800)
        pause_between_searches = get_int_env("PAUSE_BETWEEN_SEARCHES", 3)
    except ConfigError as exc:
        log.error("Ayar hatası: %s", exc)
        return 2

    telegram = TelegramClient(
        token=os.getenv("TELEGRAM_TOKEN", ""),
        chat_id=os.getenv("CHAT_ID", ""),
        dry_run=args.dry_run,
    )
    tcdd = TCDDClient(
        api_url=os.getenv("TCDD_API_URL", DEFAULT_TCDD_API_URL),
        headers=build_headers(),
        params=build_query_params(),
    )

    log.info("Bot başlıyor. %d sefer takip ediliyor.", len(searches))
    if not args.no_start_message:
        telegram.send(
            "TCDD takip botu basladi.\n"
            f"Takip edilen sefer sayisi: {len(searches)}\n"
            f"Kontrol araligi: {check_interval} sn"
        )

    last_notified: Dict[str, float] = {}

    while True:
        run_cycle(
            searches=searches,
            tcdd=tcdd,
            telegram=telegram,
            last_notified=last_notified,
            notification_cooldown=notification_cooldown,
            pause_between_searches=pause_between_searches,
        )

        if args.once:
            return 0

        log.info("Tüm seferler kontrol edildi. %d saniye bekleniyor.", check_interval)
        time.sleep(check_interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log.info("Bot durduruldu.")
