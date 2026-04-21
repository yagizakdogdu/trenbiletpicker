#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs

from tcdd_bot import (
    DEFAULT_TCDD_API_URL,
    LOG_FORMAT,
    Search,
    TCDDAPIError,
    TCDDClient,
    TelegramClient,
    build_headers,
    build_message,
    build_query_params,
    format_time_for_message,
    get_int_env,
    load_dotenv,
)


STATIONS: Dict[str, Dict[str, Any]] = {
    "ankara": {
        "id": 98,
        "display": "Ankara Gar, Ankara",
        "api_name": "ANKARA GAR",
    },
    "sogutlucesme": {
        "id": 1325,
        "display": "İstanbul(Söğütlüçeşme)",
        "api_name": "İSTANBUL(SÖĞÜTLÜÇEŞME)",
    },
}

DEFAULT_FORM = {
    "departure": "sogutlucesme",
    "arrival": "ankara",
    "departure_date": (date.today() + timedelta(days=1)).isoformat(),
    "return_enabled": "on",
    "return_date": (date.today() + timedelta(days=3)).isoformat(),
    "departure_min_saat": "09:00",
    "departure_max_saat": "15:00",
    "return_min_saat": "09:00",
    "return_max_saat": "15:00",
    "telegram": "on",
}

TRACKER_LOCK = threading.Lock()
TRACKER_STATE: Dict[str, Any] = {
    "active": False,
    "form": dict(DEFAULT_FORM),
    "runs": [],
    "started_at": "",
    "last_checked_at": "",
    "next_check_at": "",
    "interval": 120,
    "cycle_count": 0,
    "stop_event": None,
    "thread": None,
}


def make_search(
    departure_key: str,
    arrival_key: str,
    travel_date: str,
    min_saat: str,
    max_saat: str,
) -> Search:
    departure = STATIONS[departure_key]
    arrival = STATIONS[arrival_key]
    return Search(
        kalkis=departure["display"],
        varis=arrival["display"],
        tarih=travel_date,
        min_saat=min_saat,
        max_saat=max_saat,
        kalkis_id=departure["id"],
        kalkis_api_adi=departure["api_name"],
        varis_id=arrival["id"],
        varis_api_adi=arrival["api_name"],
        search_type="DOMESTIC",
        bl_train_types=["TURISTIK_TREN"],
        cabin_names=["EKONOMİ"],
    )


def form_value(form: Dict[str, str], key: str) -> str:
    return html.escape(form.get(key, DEFAULT_FORM.get(key, "")), quote=True)


def selected(form: Dict[str, str], key: str, value: str) -> str:
    return " selected" if form.get(key) == value else ""


def checked(form: Dict[str, str], key: str) -> str:
    return " checked" if form.get(key) == "on" else ""


def station_options(form: Dict[str, str], key: str) -> str:
    options = []
    for station_key, station in STATIONS.items():
        options.append(
            f'<option value="{station_key}"{selected(form, key, station_key)}>'
            f'{html.escape(station["display"])}</option>'
        )
    return "\n".join(options)


def current_tracker_snapshot() -> Dict[str, Any]:
    with TRACKER_LOCK:
        snapshot = dict(TRACKER_STATE)
        snapshot["form"] = dict(TRACKER_STATE.get("form") or DEFAULT_FORM)
        snapshot["runs"] = list(TRACKER_STATE.get("runs") or [])
        return snapshot


def tracker_meta_refresh() -> str:
    state = current_tracker_snapshot()
    if state["active"]:
        return '<meta http-equiv="refresh" content="10">'
    return ""


def render_tracker_status() -> str:
    state = current_tracker_snapshot()
    if state["active"]:
        details = [
            f'Takip açık · {html.escape(str(state["interval"]))} sn',
        ]
        if state.get("last_checked_at"):
            details.append(f'Son kontrol: {html.escape(state["last_checked_at"])}')
        if state.get("next_check_at"):
            details.append(f'Sıradaki: {html.escape(state["next_check_at"])}')
        if state.get("cycle_count"):
            details.append(f'Kontrol sayısı: {html.escape(str(state["cycle_count"]))}')
        return (
            '<section class="tracker active">'
            f'<div><strong>{details[0]}</strong><span>{" · ".join(details[1:])}</span></div>'
            '<form method="post" action="/stop"><button class="secondary danger" type="submit">Takibi Durdur</button></form>'
            '</section>'
        )

    return (
        '<section class="tracker">'
        '<div><strong>Takip kapalı</strong><span>Çalıştır tek sefer sorgular, Takibi Başlat 120 saniyede bir sorgular.</span></div>'
        '</section>'
    )


def render_results(runs: List[Dict[str, Any]]) -> str:
    if not runs:
        return ""

    sections = ['<section class="results" aria-live="polite">']
    for run in runs:
        status_class = "ok" if run["results"] else "empty"
        if run.get("error"):
            status_class = "error"

        sections.append(
            f'<section class="result-row {status_class}">'
            f'<div class="route">{html.escape(run["title"])}</div>'
        )
        if run.get("error"):
            sections.append(f'<p class="status">Hata: {html.escape(run["error"])}</p>')
        elif run.get("notice"):
            sections.append(f'<p class="status">{html.escape(run["notice"])}</p>')
        elif run["results"]:
            sections.append(
                f'<p class="status">{len(run["results"])} ekonomi kaydı bulundu.</p>'
            )
            if run.get("telegram_error"):
                sections.append(
                    f'<p class="status error-text">Telegram: {html.escape(run["telegram_error"])}</p>'
                )
            if run.get("telegram_status"):
                sections.append(
                    f'<p class="status">{html.escape(run["telegram_status"])}</p>'
                )
            sections.append("<table><thead><tr><th>Tren</th><th>Saat</th><th>Sınıf</th><th>Koltuk</th></tr></thead><tbody>")
            for item in run["results"]:
                sections.append(
                    "<tr>"
                    f"<td>{html.escape(str(item['tren']))}</td>"
                    f"<td>{html.escape(format_time_for_message(item['kalkis_saat']))}</td>"
                    f"<td>{html.escape(str(item['sinif']))}</td>"
                    f"<td>{html.escape(str(item['bos_koltuk']))}</td>"
                    "</tr>"
                )
            sections.append("</tbody></table>")
        else:
            sections.append('<p class="status">Ekonomi boş koltuk bulunmadı.</p>')
        sections.append("</section>")
    sections.append("</section>")
    return "\n".join(sections)


def render_page(form: Dict[str, str], runs: List[Dict[str, Any]]) -> bytes:
    body = f"""<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {tracker_meta_refresh()}
  <title>TCDD Ekonomi Sorgu</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --ink: #1d2430;
      --muted: #5d6675;
      --line: #cfd6df;
      --panel: #ffffff;
      --accent: #0d6e6e;
      --accent-dark: #0a5555;
      --ok: #16794c;
      --empty: #8a5a00;
      --error: #b42318;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{
      width: min(1080px, calc(100vw - 32px));
      margin: 28px auto;
    }}
    h1 {{
      margin: 0 0 18px;
      font-size: 28px;
      font-weight: 700;
      letter-spacing: 0;
    }}
    form, .result-row, .tracker {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
    }}
    .tracker {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
    }}
    .tracker strong {{
      display: block;
      margin-bottom: 4px;
      font-size: 15px;
    }}
    .tracker span {{
      display: block;
      color: var(--muted);
      font-size: 13px;
    }}
    .tracker form {{
      border: 0;
      padding: 0;
      background: transparent;
    }}
    .tracker.active {{
      border-color: #72b7a4;
      background: #f1fbf7;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      align-items: end;
    }}
    .group-label {{
      grid-column: 1 / -1;
      color: var(--ink);
      font-size: 14px;
      font-weight: 750;
      padding-top: 4px;
    }}
    label {{
      display: grid;
      gap: 7px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 600;
    }}
    select, input {{
      width: 100%;
      height: 42px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      padding: 0 11px;
      font: inherit;
      font-size: 15px;
    }}
    input[type="time"] {{
      cursor: pointer;
    }}
    .time-picker-menu {{
      position: fixed;
      z-index: 20;
      width: 150px;
      max-height: 260px;
      overflow: auto;
      display: none;
      padding: 6px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      box-shadow: 0 14px 36px rgba(29, 36, 48, 0.18);
    }}
    .time-picker-menu.open {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 4px;
    }}
    .time-picker-menu button {{
      min-height: 34px;
      border-radius: 5px;
      background: #fff;
      color: var(--ink);
      border: 1px solid transparent;
      padding: 0 6px;
      font-size: 13px;
      font-weight: 650;
    }}
    .time-picker-menu button:hover,
    .time-picker-menu button.active {{
      border-color: var(--accent);
      background: #edf8f6;
      color: var(--accent-dark);
    }}
    .checkline {{
      display: flex;
      align-items: center;
      gap: 9px;
      min-height: 42px;
      color: var(--ink);
      font-size: 15px;
      font-weight: 600;
    }}
    .checkline input {{
      width: 18px;
      height: 18px;
      padding: 0;
    }}
    .actions {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-top: 16px;
      flex-wrap: wrap;
    }}
    .button-row {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }}
    button {{
      min-height: 44px;
      border: 0;
      border-radius: 6px;
      background: var(--accent);
      color: #fff;
      padding: 0 18px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }}
    button:hover {{ background: var(--accent-dark); }}
    button.secondary {{
      background: #eef2f6;
      color: var(--ink);
      border: 1px solid var(--line);
    }}
    button.secondary:hover {{ background: #dfe6ee; }}
    button.danger {{
      color: var(--error);
    }}
    .hint {{
      color: var(--muted);
      font-size: 13px;
    }}
    .results {{
      display: grid;
      gap: 12px;
      margin-top: 18px;
    }}
    .route {{
      font-size: 17px;
      font-weight: 750;
      margin-bottom: 8px;
    }}
    .status {{
      margin: 0 0 12px;
      color: var(--muted);
    }}
    .ok .status {{ color: var(--ok); }}
    .empty .status {{ color: var(--empty); }}
    .error .status {{ color: var(--error); }}
    .error-text {{ color: var(--error) !important; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }}
    th, td {{
      padding: 10px 8px;
      border-top: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0;
    }}
    @media (max-width: 760px) {{
      main {{ width: min(100vw - 20px, 620px); margin: 16px auto; }}
      .grid {{ grid-template-columns: 1fr; }}
      form, .result-row {{ padding: 14px; }}
      .tracker {{ align-items: stretch; flex-direction: column; padding: 14px; }}
      h1 {{ font-size: 23px; }}
      table {{ font-size: 13px; }}
    }}
  </style>
</head>
<body>
  <main>
    <h1>TCDD Ekonomi Sorgu</h1>
    {render_tracker_status()}
    <form method="post" action="/run">
      <div class="grid">
        <label>Kalkış
          <select name="departure">{station_options(form, "departure")}</select>
        </label>
        <label>Varış
          <select name="arrival">{station_options(form, "arrival")}</select>
        </label>
        <label>Gidiş Tarihi
          <input type="date" name="departure_date" value="{form_value(form, "departure_date")}" required>
        </label>
        <label>Dönüş Tarihi
          <input type="date" name="return_date" value="{form_value(form, "return_date")}">
        </label>
        <div class="group-label">Gidiş Saat Aralığı</div>
        <label>Gidiş Min
          <input type="time" name="departure_min_saat" value="{form_value(form, "departure_min_saat")}" required>
        </label>
        <label>Gidiş Maks
          <input type="time" name="departure_max_saat" value="{form_value(form, "departure_max_saat")}" required>
        </label>
        <div class="group-label">Dönüş Saat Aralığı</div>
        <label>Dönüş Min
          <input type="time" name="return_min_saat" value="{form_value(form, "return_min_saat")}">
        </label>
        <label>Dönüş Maks
          <input type="time" name="return_max_saat" value="{form_value(form, "return_max_saat")}">
        </label>
        <label class="checkline">
          <input type="checkbox" name="return_enabled"{checked(form, "return_enabled")}>
          Dönüş
        </label>
        <label class="checkline">
          <input type="checkbox" name="telegram"{checked(form, "telegram")}>
          Telegram
        </label>
      </div>
      <div class="actions">
        <div class="button-row">
          <button type="submit" name="action" value="run">Çalıştır</button>
          <button class="secondary" type="submit" name="action" value="start">Takibi Başlat</button>
        </div>
        <span class="hint">Sınıf: Ekonomi · Aralık: 120 sn</span>
      </div>
    </form>
    {render_results(runs)}
  </main>
  <div id="time-picker-menu" class="time-picker-menu" role="listbox"></div>
  <script>
    const menu = document.getElementById('time-picker-menu');
    const times = [];
    for (let hour = 0; hour < 24; hour += 1) {{
      for (const minute of [0, 30]) {{
        times.push(`${{String(hour).padStart(2, '0')}}:${{String(minute).padStart(2, '0')}}`);
      }}
    }}

    let activeInput = null;

    const closeMenu = () => {{
      menu.classList.remove('open');
      activeInput = null;
    }};

    const positionMenu = (input) => {{
      const rect = input.getBoundingClientRect();
      const width = 150;
      const left = Math.min(rect.left, window.innerWidth - width - 8);
      const top = Math.min(rect.bottom + 6, window.innerHeight - 270);
      menu.style.left = `${{Math.max(8, left)}}px`;
      menu.style.top = `${{Math.max(8, top)}}px`;
      menu.style.width = `${{width}}px`;
    }};

    const openMenu = (input) => {{
      activeInput = input;
      menu.innerHTML = '';
      times.forEach((time) => {{
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = time;
        button.setAttribute('role', 'option');
        if (input.value === time) button.classList.add('active');
        button.addEventListener('click', () => {{
          input.value = time;
          input.dispatchEvent(new Event('change', {{ bubbles: true }}));
          closeMenu();
          input.focus();
        }});
        menu.appendChild(button);
      }});
      positionMenu(input);
      menu.classList.add('open');
    }};

    document.querySelectorAll('input[type="time"]').forEach((input) => {{
      input.addEventListener('click', (event) => {{
        event.preventDefault();
        openMenu(input);
      }});
      input.addEventListener('keydown', (event) => {{
        if (event.key === 'ArrowDown' || event.key === 'Enter' || event.key === ' ') {{
          event.preventDefault();
          openMenu(input);
        }}
        if (event.key === 'Escape') closeMenu();
      }});
    }});

    document.addEventListener('click', (event) => {{
      if (!menu.contains(event.target) && event.target !== activeInput) {{
        closeMenu();
      }}
    }});
    window.addEventListener('resize', closeMenu);
    window.addEventListener('scroll', (event) => {{
      if (event.target !== menu && !menu.contains(event.target)) {{
        closeMenu();
      }}
    }}, true);
  </script>
</body>
</html>"""
    return body.encode("utf-8")


def parse_form(raw_body: bytes) -> Dict[str, str]:
    parsed = parse_qs(raw_body.decode("utf-8"), keep_blank_values=True)
    form = dict(DEFAULT_FORM)
    for key, values in parsed.items():
        form[key] = values[0] if values else ""
    if "min_saat" in parsed and "departure_min_saat" not in parsed:
        form["departure_min_saat"] = parsed["min_saat"][0]
    if "min_saat" in parsed and "return_min_saat" not in parsed:
        form["return_min_saat"] = parsed["min_saat"][0]
    if "max_saat" in parsed and "departure_max_saat" not in parsed:
        form["departure_max_saat"] = parsed["max_saat"][0]
    if "max_saat" in parsed and "return_max_saat" not in parsed:
        form["return_max_saat"] = parsed["max_saat"][0]
    if not form.get("return_min_saat"):
        form["return_min_saat"] = form.get("departure_min_saat", DEFAULT_FORM["departure_min_saat"])
    if not form.get("return_max_saat"):
        form["return_max_saat"] = form.get("departure_max_saat", DEFAULT_FORM["departure_max_saat"])
    form["return_enabled"] = "on" if "return_enabled" in parsed else ""
    form["telegram"] = "on" if "telegram" in parsed else ""
    return form


def normalize_form(form: Dict[str, str]) -> Dict[str, str]:
    normalized = dict(DEFAULT_FORM)
    normalized.update(form)
    if "min_saat" in form and "departure_min_saat" not in form:
        normalized["departure_min_saat"] = form["min_saat"]
    if "max_saat" in form and "departure_max_saat" not in form:
        normalized["departure_max_saat"] = form["max_saat"]
    if not normalized.get("return_min_saat"):
        normalized["return_min_saat"] = normalized.get("departure_min_saat", DEFAULT_FORM["departure_min_saat"])
    if not normalized.get("return_max_saat"):
        normalized["return_max_saat"] = normalized.get("departure_max_saat", DEFAULT_FORM["departure_max_saat"])
    return normalized


def validate_form(form: Dict[str, str]) -> List[str]:
    form = normalize_form(form)
    errors = []
    if form["departure"] not in STATIONS:
        errors.append("Kalkış istasyonu geçersiz.")
    if form["arrival"] not in STATIONS:
        errors.append("Varış istasyonu geçersiz.")
    if form["departure"] == form["arrival"]:
        errors.append("Kalkış ve varış farklı olmalı.")
    for field in ("departure_date", "departure_min_saat", "departure_max_saat"):
        if not form.get(field):
            errors.append(f"{field} boş olamaz.")
    if form.get("return_enabled") == "on":
        for field in ("return_date", "return_min_saat", "return_max_saat"):
            if not form.get(field):
                errors.append(f"{field} boş olamaz.")
    if (
        form.get("departure_min_saat")
        and form.get("departure_max_saat")
        and form["departure_min_saat"] > form["departure_max_saat"]
    ):
        errors.append("Gidiş min saat gidiş maks saatten büyük olamaz.")
    if (
        form.get("return_enabled") == "on"
        and form.get("return_min_saat")
        and form.get("return_max_saat")
        and form["return_min_saat"] > form["return_max_saat"]
    ):
        errors.append("Dönüş min saat dönüş maks saatten büyük olamaz.")
    return errors


def build_searches_from_form(form: Dict[str, str]) -> List[Search]:
    form = normalize_form(form)
    searches = [
        make_search(
            form["departure"],
            form["arrival"],
            form["departure_date"],
            form["departure_min_saat"],
            form["departure_max_saat"],
        )
    ]
    if form.get("return_enabled") == "on":
        searches.append(
            make_search(
                form["arrival"],
                form["departure"],
                form["return_date"],
                form["return_min_saat"],
                form["return_max_saat"],
            )
        )
    return searches


def notification_key(search: Search, result: Dict[str, Any]) -> str:
    return "|".join(
        [
            search.key,
            str(result.get("tren", "")),
            format_time_for_message(result.get("kalkis_saat", "")),
            str(result.get("sinif", "")),
        ]
    )


def run_searches(
    form: Dict[str, str],
    *,
    last_notified: Optional[Dict[str, float]] = None,
    notification_cooldown: int = 0,
) -> List[Dict[str, Any]]:
    form = normalize_form(form)
    errors = validate_form(form)
    if errors:
        return [{"title": "Form", "results": [], "error": " ".join(errors)}]

    searches = build_searches_from_form(form)

    tcdd = TCDDClient(
        api_url=DEFAULT_TCDD_API_URL,
        headers=build_headers(),
        params=build_query_params(),
    )
    telegram = TelegramClient(
        token=os.getenv("TELEGRAM_TOKEN", ""),
        chat_id=os.getenv("CHAT_ID", ""),
        dry_run=form.get("telegram") != "on",
    )

    runs: List[Dict[str, Any]] = []
    for search in searches:
        title = f"{search.kalkis} → {search.varis} · {search.tarih} · {search.min_saat}-{search.max_saat}"
        try:
            results = tcdd.query(search)
        except TCDDAPIError as exc:
            runs.append({"title": title, "results": [], "error": str(exc)})
            continue
        except Exception as exc:
            runs.append({"title": title, "results": [], "error": str(exc)})
            continue

        runs.append({"title": title, "results": results, "error": ""})
        logging.getLogger("tcdd-web").info(
            "Sorgu tamamlandı: %s ekonomi sonucu=%d",
            title,
            len(results),
        )
        if results and form.get("telegram") == "on":
            notify_results = results
            notify_keys: List[str] = []
            if last_notified is not None:
                now = time.time()
                notify_results = []
                for result in results:
                    key = notification_key(search, result)
                    if now - last_notified.get(key, 0) >= notification_cooldown:
                        notify_results.append(result)
                        notify_keys.append(key)

            if not notify_results:
                runs[-1]["telegram_status"] = "Telegram cooldown içinde; tekrar gönderilmedi."
            elif not telegram.send(build_message(search, notify_results)):
                runs[-1]["telegram_error"] = telegram.last_error
            else:
                if last_notified is not None:
                    sent_at = time.time()
                    for key in notify_keys:
                        last_notified[key] = sent_at
                runs[-1]["telegram_status"] = "Telegram bildirimi gönderildi."
    return runs


def timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def tracker_worker(
    form: Dict[str, str],
    stop_event: threading.Event,
    interval: int,
    notification_cooldown: int,
) -> None:
    logger = logging.getLogger("tcdd-tracker")
    last_notified: Dict[str, float] = {}
    logger.info("Takip başladı: %s sn", interval)

    while not stop_event.is_set():
        checked_at = timestamp()
        next_check = datetime.now() + timedelta(seconds=interval)
        with TRACKER_LOCK:
            TRACKER_STATE["last_checked_at"] = checked_at
            TRACKER_STATE["next_check_at"] = next_check.strftime("%H:%M:%S")
            TRACKER_STATE["cycle_count"] = int(TRACKER_STATE.get("cycle_count") or 0) + 1

        runs = run_searches(
            form,
            last_notified=last_notified,
            notification_cooldown=notification_cooldown,
        )
        with TRACKER_LOCK:
            if TRACKER_STATE.get("stop_event") is stop_event:
                TRACKER_STATE["runs"] = runs
                TRACKER_STATE["last_checked_at"] = checked_at
                TRACKER_STATE["next_check_at"] = next_check.strftime("%H:%M:%S")

        stop_event.wait(interval)

    with TRACKER_LOCK:
        if TRACKER_STATE.get("stop_event") is stop_event:
            TRACKER_STATE["active"] = False
            TRACKER_STATE["next_check_at"] = ""
    logger.info("Takip durdu.")


def stop_tracker() -> None:
    with TRACKER_LOCK:
        event = TRACKER_STATE.get("stop_event")
        TRACKER_STATE["active"] = False
        TRACKER_STATE["next_check_at"] = ""
    if isinstance(event, threading.Event):
        event.set()


def start_tracker(form: Dict[str, str]) -> List[Dict[str, Any]]:
    form = normalize_form(form)
    errors = validate_form(form)
    if errors:
        return [{"title": "Form", "results": [], "error": " ".join(errors)}]

    stop_tracker()
    interval = get_int_env("CHECK_INTERVAL", 120)
    notification_cooldown = get_int_env("NOTIFICATION_COOLDOWN", 1800)
    event = threading.Event()
    form_copy = dict(form)
    thread = threading.Thread(
        target=tracker_worker,
        args=(form_copy, event, interval, notification_cooldown),
        daemon=True,
    )
    with TRACKER_LOCK:
        TRACKER_STATE.update(
            {
                "active": True,
                "form": form_copy,
                "runs": [
                    {
                        "title": "Takip",
                        "results": [],
                        "error": "",
                        "notice": "Takip başlatıldı. İlk sorgu hemen yapılacak.",
                    }
                ],
                "started_at": timestamp(),
                "last_checked_at": "",
                "next_check_at": "",
                "interval": interval,
                "cycle_count": 0,
                "stop_event": event,
                "thread": thread,
            }
        )
    thread.start()
    return current_tracker_snapshot()["runs"]


class AppHandler(BaseHTTPRequestHandler):
    def do_HEAD(self) -> None:
        if self.path != "/":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path not in ("/", "/run"):
            self.send_error(404)
            return
        state = current_tracker_snapshot()
        form = state["form"] if state["active"] else dict(DEFAULT_FORM)
        runs = state["runs"] if state["active"] else []
        self.respond(render_page(form, runs))

    def do_POST(self) -> None:
        if self.path == "/stop":
            stop_tracker()
            state = current_tracker_snapshot()
            self.respond(render_page(state["form"], state["runs"]))
            return

        if self.path != "/run":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        form = parse_form(self.rfile.read(length))
        if form.get("action") == "start":
            runs = start_tracker(form)
        else:
            runs = run_searches(form)
        self.respond(render_page(form, runs))

    def respond(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        logging.getLogger("tcdd-web").info(format, *args)


def main() -> int:
    parser = argparse.ArgumentParser(description="TCDD lokal web paneli")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt="%H:%M:%S")
    load_dotenv(Path(".env"))
    load_dotenv(Path(".env.local"))

    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    logging.getLogger("tcdd-web").info(
        "Web panel hazır: http://%s:%s", args.host, args.port
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
