"""FastAPI wrapper around main.py so the React UI can start a run, watch progress and approve picks.

  pip install fastapi uvicorn
  uvicorn server:app --port 8000
One run at a time (the run log is global). If digest-ui/dist exists it is served at /.
"""
import base64
import binascii
import json
import os
import re
import secrets
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Literal

import main as m

app = FastAPI()
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
REQUIRE_APP_AUTH = os.environ.get("REQUIRE_APP_AUTH", "").strip().lower() in ("1", "true", "yes", "y")
if REQUIRE_APP_AUTH and not APP_PASSWORD:
    raise RuntimeError("APP_PASSWORD must be set when REQUIRE_APP_AUTH=1")

STATE: dict = {"status": "idle", "error": None, "pending": None, "result": None, "decision": None,
              "run_mode": "four_roles"}
DECIDED = threading.Event()


@app.middleware("http")
async def password_gate(request: Request, call_next):
    if not REQUIRE_APP_AUTH or request.url.path == "/healthz":
        return await call_next(request)
    header = request.headers.get("authorization", "")
    try:
        scheme, encoded = header.split(" ", 1)
        credentials = base64.b64decode(encoded, validate=True).decode("utf-8")
        _, password = credentials.split(":", 1)
        authorized = scheme.lower() == "basic" and secrets.compare_digest(password, APP_PASSWORD)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        authorized = False
    if not authorized:
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Daily AI Digest"'})
    return await call_next(request)


class RunReq(BaseModel):
    require_human: bool = True
    mode: Literal["single_agent", "four_roles"] = "four_roles"


class DecisionReq(BaseModel):
    approve: bool
    feedback: str = ""


def human_hook(picks):
    STATE.update(pending=picks, status="awaiting_review")
    DECIDED.clear()
    DECIDED.wait()
    d = STATE["decision"]
    STATE.update(pending=None, status="running")
    return d["approve"], d["feedback"]


def worker(require_human: bool, mode: str):
    try:
        m.init_run()
        m.log_event("run_started", mode=mode)
        m.REQUIRE_HUMAN = require_human
        m.HUMAN_HOOK = human_hook if require_human else None
        graph = m.build_single_agent_graph() if mode == "single_agent" else m.build_graph()
        initial = {"candidates": [], "rejected_urls": [], "check_failures": 0, "human_rounds": 0,
                   "run_mode": mode}
        final = graph.invoke(
            initial,
            config={"configurable": {"thread_id": "ui"}, "recursion_limit": 60})
        STATE.update(status="done", result={
            "status": final.get("status", ""), "picks": final.get("picks", []),
            "rejection_log": final.get("rejection_log", []), "doubts": final.get("doubts", []),
            "report": Path(final["report_path"]).read_text(encoding="utf-8"), "run_mode": mode})
    except Exception as e:  # noqa: BLE001
        STATE.update(status="error", error=str(e)[:400])


@app.post("/api/runs")
def start(req: RunReq):
    if STATE["status"] in ("running", "awaiting_review"):
        raise HTTPException(409, "A run is already in progress")
    try:
        m.require_config(require_checker=req.mode != "single_agent")
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    STATE.update(status="running", error=None, pending=None, result=None, run_mode=req.mode)
    threading.Thread(target=worker, args=(req.require_human, req.mode), daemon=True).start()
    return {"ok": True}


@app.get("/api/state")
def state(since: int = 0):
    return {**{k: STATE[k] for k in ("status", "error", "pending", "result", "run_mode")},
            "events": m.RUN_LOG[since:], "next": len(m.RUN_LOG)}


@app.post("/api/decision")
def decide(req: DecisionReq):
    if STATE["status"] != "awaiting_review":
        raise HTTPException(409, "Nothing is waiting for review")
    STATE["decision"] = req.model_dump()
    DECIDED.set()
    return {"ok": True}


LOGS = Path("logs")


def read_events(path: Path) -> list:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


@app.get("/api/config")
def config():
    return {"main_model": m.MAIN_CFG["model"], "checker_model": m.CHECKER_CFG["model"],
            "price_in": m.PRICE_IN_PER_M, "price_out": m.PRICE_OUT_PER_M, "require_human": m.REQUIRE_HUMAN,
            "agent_search": m.USE_AGENT_SEARCH, "llm_review": m.USE_LLM_REVIEW}


@app.get("/healthz")
def health():
    return {"ok": True}


@app.get("/api/history")
def history():
    items = []
    for f in sorted(LOGS.glob("run_*.jsonl"), reverse=True)[:30]:
        ev = read_events(f)
        calls = [e for e in ev if e["event"] == "llm_call"]
        done = next((e for e in ev if e["event"] == "run_done"), None)
        started = next((e for e in ev if e["event"] == "run_started"), {})
        items.append({"name": f.stem, "started": ev[0]["ts"] if ev else "", "events": len(ev),
                      "status": done["status"] if done else "incomplete", "mode": started.get("mode", "four_roles"),
                      "calls": len(calls),
                      "input_tokens": sum(e.get("input_tokens", 0) for e in calls),
                      "output_tokens": sum(e.get("output_tokens", 0) for e in calls),
                      "latency_s": round(sum(e.get("latency_s", 0) for e in calls), 1)})
    return items


@app.get("/api/history/{name}")
def history_run(name: str):
    if not re.fullmatch(r"run_\d{8}_\d{6}", name) or not (LOGS / f"{name}.jsonl").exists():
        raise HTTPException(404, "Run not found")
    return read_events(LOGS / f"{name}.jsonl")


if Path("digest-ui/dist").exists():
    app.mount("/", StaticFiles(directory="digest-ui/dist", html=True), name="ui")
