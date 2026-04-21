from __future__ import annotations

from pathlib import Path

from flask import Flask, Response, request

from tcdd_bot import load_dotenv
from web_app import DEFAULT_FORM, normalize_form, parse_form, render_page, run_searches


load_dotenv(Path(".env"))
load_dotenv(Path(".env.local"))

app = Flask(__name__)


@app.get("/")
def index() -> Response:
    return html_response(render_page(dict(DEFAULT_FORM), []))


@app.post("/run")
def run() -> Response:
    form = normalize_form(parse_form(request.get_data()))
    if form.get("action") == "start":
        runs = [
            {
                "title": "Vercel",
                "results": [],
                "error": "",
                "notice": (
                    "Vercel serverless ortamında 120 saniyelik arka plan takip "
                    "modu çalışmaz. Bu ekranda tek sefer sorgu yapıldı."
                ),
            }
        ]
        runs.extend(run_searches(form))
    else:
        runs = run_searches(form)
    return html_response(render_page(form, runs))


@app.post("/stop")
def stop() -> Response:
    runs = [
        {
            "title": "Vercel",
            "results": [],
            "error": "",
            "notice": "Vercel deployunda arka plan takip modu yok; durdurulacak takip bulunmuyor.",
        }
    ]
    return html_response(render_page(dict(DEFAULT_FORM), runs))


def html_response(body: bytes) -> Response:
    return Response(body, mimetype="text/html; charset=utf-8")
