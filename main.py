"""
Task A - Daily AI news digest (LangGraph)

Flow:
  researcher -> drafter -> checker --pass--> human (optional) -> reporter -> END
                   ^          |
                   |          +--fail, enough good candidates--> drafter
                   +----------+--fail, too few good candidates--> researcher
  (a failure counter stops the loop after MAX_CHECK_FAILURES)

Install:
  pip install langchain-openai langgraph tavily-python feedparser requests pydantic python-dotenv

Run:
  export TAVILY_API_KEY=...            # or USE_AGENT_SEARCH=0 to skip web search
  python main.py                       # full pipeline with human approval
  python main.py --no-human            # full pipeline, no input() prompt (for scoring runs)
  python main.py --baseline            # Day 1: researcher + one drafter call, no checker
  python main.py --test-checker        # planted-error tests, no LLM or network needed

Useful env vars:
  MAIN_API_KEY, MAIN_MODEL, MAIN_BASE_URL      (researcher + drafter; default Gemini free tier)
  CHECKER_API_KEY, CHECKER_MODEL, CHECKER_BASE_URL (default Groq; any OpenAI-compatible provider works)
  REQUIRE_HUMAN, USE_AGENT_SEARCH, USE_LLM_REVIEW, PRICE_IN_PER_M, PRICE_OUT_PER_M
"""
import argparse
import difflib
import json
import operator
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Annotated, Literal, Optional, TypedDict
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import requests
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from tavily import TavilyClient

load_dotenv()  # read .env from the current folder before any config below


# ----------------------------------------------------------------------------
# 1. CONFIG
# ----------------------------------------------------------------------------
def env_flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "y")


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"  # confirm in Google's docs
GROQ_URL = "https://api.groq.com/openai/v1"


def role_cfg(prefix: str, default_url: str, default_model: str) -> dict:
    return {"base_url": os.environ.get(f"{prefix}_BASE_URL", default_url),
            "api_key": os.environ.get(f"{prefix}_API_KEY", ""),
            "model": os.environ.get(f"{prefix}_MODEL", default_model)}


# Pin exact model IDs. Free model lists change weekly; check the provider's list before a scored run.
MAIN_CFG = role_cfg("MAIN", GEMINI_URL, "gemini-3.5-flash-lite")   # researcher + drafter
CHECKER_CFG = role_cfg("CHECKER", GROQ_URL, "openai/gpt-oss-120b")  # confirm the ID in the Groq console
MODEL = MAIN_CFG["model"]
CHECKER_MODEL = CHECKER_CFG["model"]
HUMAN_HOOK = None  # set by server.py so the web UI can approve picks instead of input()


def require_config() -> None:
    missing = [n for n, v in [("MAIN_API_KEY", MAIN_CFG["api_key"]), ("CHECKER_API_KEY", CHECKER_CFG["api_key"]),
                              ("CHECKER_MODEL", CHECKER_CFG["model"])] if not v]
    if missing:
        raise RuntimeError("Missing environment variables: " + ", ".join(missing))

TOP_N = 5
FRESH_HOURS = 24               # a pick must be newer than this
CANDIDATE_WINDOW_HOURS = 48    # how far back feeds/HN are pulled
MAX_PER_FEED = 8
MAX_DRAFT_CANDIDATES = 30      # keep the prompt small for local models
MAX_TOOL_CALLS = 5             # budget for the researcher's search loop
MAX_CHECK_FAILURES = 3
MAX_HUMAN_ROUNDS = 3

REQUIRE_HUMAN = env_flag("REQUIRE_HUMAN", True)
USE_AGENT_SEARCH = env_flag("USE_AGENT_SEARCH", True)
USE_LLM_REVIEW = env_flag("USE_LLM_REVIEW", True)
PRICE_IN_PER_M = float(os.environ.get("PRICE_IN_PER_M", "0"))   # $ per 1M input tokens (0 for local)
PRICE_OUT_PER_M = float(os.environ.get("PRICE_OUT_PER_M", "0"))

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ai-digest-bot/0.1)"}

# Verify these URLs before relying on them; feeds move. A failing feed is logged and skipped.
FEEDS = [
    ("OpenAI", "https://openai.com/news/rss.xml"),
    ("Google AI", "https://blog.google/technology/ai/rss/"),
    ("DeepMind", "https://deepmind.google/blog/rss.xml"),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml"),
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    ("NVIDIA", "https://blogs.nvidia.com/feed/"),
]
HN_QUERIES = ["AI", "LLM"]


# ----------------------------------------------------------------------------
# 2. LOGGING (JSONL per run) AND LLM CALLS
# ----------------------------------------------------------------------------
RUN_LOG: list[dict] = []
_log_path: Optional[Path] = None
_run_start = time.time()


def init_run() -> None:
    global _log_path, _run_start
    RUN_LOG.clear()
    Path("logs").mkdir(exist_ok=True)
    _log_path = Path("logs") / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    _run_start = time.time()


def log_event(event: str, **fields) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
    RUN_LOG.append(entry)
    if _log_path:
        with _log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def make_llm(cfg: dict) -> ChatOpenAI:
    return ChatOpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"] or "missing", model=cfg["model"],
                      temperature=0.1, max_retries=0)  # we handle 429s ourselves in call_llm


llm = make_llm(MAIN_CFG)
checker_llm = make_llm(CHECKER_CFG)


def call_llm(step: str, model, messages, model_name: str = MODEL):
    """Every model call goes through here so tokens and latency are always logged."""
    t0 = time.time()
    for attempt in range(4):
        try:
            resp = model.invoke(messages)
            break
        except Exception as e:  # noqa: BLE001
            if "429" in str(e) and attempt < 3:  # free-tier rate limit: wait and retry
                wait = 15 * (attempt + 1)
                log_event("rate_limited", step=step, wait_s=wait)
                time.sleep(wait)
                continue
            raise
    usage = getattr(resp, "usage_metadata", None) or {}
    log_event(
        "llm_call", step=step, model=model_name,
        input_tokens=usage.get("input_tokens", 0), output_tokens=usage.get("output_tokens", 0),
        latency_s=round(time.time() - t0, 2),
    )
    return resp


def ask_json(step: str, model, prompt: str, schema: type[BaseModel], model_name: str = MODEL, retries: int = 2):
    """One call that must return JSON matching `schema`. Works on any local model (no native structured output needed)."""
    hint = json.dumps(schema.model_json_schema())
    base = f"{prompt}\n\nReply with ONLY a JSON object matching this JSON schema. No prose, no code fences.\n{hint}"
    err = ""
    for attempt in range(retries):
        text = base if attempt == 0 else f"{base}\n\nYour previous reply was invalid ({err}). Return valid JSON only."
        resp = call_llm(step, model, text, model_name)
        raw = resp.content if isinstance(resp.content, str) else str(resp.content)
        try:
            match = re.search(r"\{.*\}", raw, re.S)
            return schema.model_validate_json(match.group(0))
        except Exception as e:  # noqa: BLE001
            err = str(e)[:160].replace("\n", " ")
    log_event("parse_fail", step=step, error=err)
    return None


# ----------------------------------------------------------------------------
# 3. SMALL HELPERS (dates, URLs, candidates)
# ----------------------------------------------------------------------------
def parse_dt(value) -> Optional[datetime]:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
        s = str(value).strip()
        try:
            dt = parsedate_to_datetime(s)  # RFC 822, used by RSS and Tavily
        except (TypeError, ValueError):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def age_hours(published, now: Optional[datetime] = None) -> Optional[float]:
    dt = parse_dt(published)
    if dt is None:
        return None
    return ((now or datetime.now(timezone.utc)) - dt).total_seconds() / 3600


def normalize_url(url: str) -> str:
    try:
        p = urlparse(url.strip())
    except Exception:  # noqa: BLE001
        return url
    host = p.netloc.lower().removeprefix("www.")
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith("utm_")])
    return urlunparse(("", host, p.path.rstrip("/"), "", query, ""))


def similar_titles(a: str, b: str, threshold: float = 0.85) -> bool:
    na, nb = (re.sub(r"[^a-z0-9 ]", "", t.lower()) for t in (a, b))
    return difflib.SequenceMatcher(None, na, nb).ratio() >= threshold


def make_candidate(title, url, published, source, snippet="") -> dict:
    dt = parse_dt(published)
    return {
        "title": (title or "").strip(),
        "url": (url or "").strip(),
        "published": dt.isoformat(timespec="seconds") if dt else "",
        "source": source,
        "snippet": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", snippet or "")).strip()[:300],
    }


def merge_candidates(existing: list[dict], new: list[dict]) -> list[dict]:
    seen = {normalize_url(c["url"]) for c in existing}
    merged = list(existing)
    for c in new:
        key = normalize_url(c["url"])
        if c["title"] and c["url"] and key not in seen:
            seen.add(key)
            merged.append(c)
    return merged


def eligible_candidates(candidates: list[dict], rejected_urls) -> list[dict]:
    """Fresh, de-duplicated, not previously rejected; newest first. This is what the drafter sees."""
    rejected = {normalize_url(u) for u in rejected_urls}
    now = datetime.now(timezone.utc)
    pool = []
    for c in candidates:
        age = age_hours(c["published"], now)
        if age is None or age > FRESH_HOURS or age < -1:
            continue
        if normalize_url(c["url"]) in rejected:
            continue
        pool.append((age, c))
    pool.sort(key=lambda t: t[0])
    out: list[dict] = []
    for _, c in pool:
        if not any(similar_titles(c["title"], o["title"]) for o in out):
            out.append(c)
    return out


# ----------------------------------------------------------------------------
# 4. RESEARCHER: deterministic pulls (RSS + Hacker News) + a bounded tool-using agent
# ----------------------------------------------------------------------------
def pull_feeds() -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=CANDIDATE_WINDOW_HOURS)
    out: list[dict] = []
    for name, url in FEEDS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
            items = []
            for e in parsed.entries:
                t = e.get("published_parsed") or e.get("updated_parsed")
                dt = datetime(*t[:6], tzinfo=timezone.utc) if t else None
                if dt and dt >= cutoff and e.get("title") and e.get("link"):
                    items.append(make_candidate(e["title"], e["link"], dt, name, e.get("summary", "")))
            items.sort(key=lambda c: c["published"], reverse=True)
            out.extend(items[:MAX_PER_FEED])
            log_event("feed_ok", feed=name, items=len(items))
        except Exception as e:  # noqa: BLE001
            log_event("feed_error", feed=name, error=str(e)[:160])
    return out


def pull_hn() -> list[dict]:
    since = int((datetime.now(timezone.utc) - timedelta(hours=CANDIDATE_WINDOW_HOURS)).timestamp())
    out: list[dict] = []
    for q in HN_QUERIES:
        try:
            r = requests.get(
                "https://hn.algolia.com/api/v1/search_by_date",
                params={"query": q, "tags": "story", "hitsPerPage": 15,
                        "numericFilters": f"created_at_i>{since},points>=20"},
                headers=HEADERS, timeout=10,
            )
            r.raise_for_status()
            for h in r.json().get("hits", []):
                url = h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}"
                out.append(make_candidate(h.get("title"), url, h.get("created_at_i"),
                                          "Hacker News", f"HN points: {h.get('points')}"))
        except Exception as e:  # noqa: BLE001
            log_event("feed_error", feed=f"HN:{q}", error=str(e)[:160])
    return out


_tavily: Optional[TavilyClient] = None


def get_tavily() -> TavilyClient:
    global _tavily
    if _tavily is None:
        key = os.environ.get("TAVILY_API_KEY")
        if not key:
            raise RuntimeError("TAVILY_API_KEY is not set")
        _tavily = TavilyClient(api_key=key)
    return _tavily


# Tools given to the researcher agent. They return structured results (title, url, date),
# so the evidence is kept in code and does not depend on what the model writes back.
def search_news(query: str) -> list[dict]:
    """Search the web for AI news published in the last 24 hours. Use a short, specific query such as 'open-source LLM release' or 'AI chip export rules'."""
    try:
        res = get_tavily().search(query=query, topic="news", time_range="day", search_depth="basic", max_results=8)
    except Exception as e:  # noqa: BLE001
        log_event("tool_error", tool="search_news", error=str(e)[:160])
        return []
    return [
        make_candidate(r.get("title"), r.get("url"), r.get("published_date"),
                       urlparse(r.get("url", "")).netloc.removeprefix("www."), r.get("content", ""))
        for r in res.get("results", [])
    ]


def read_feed(url: str) -> list[dict]:
    """Read an RSS/Atom feed URL (for example a company blog) and return its most recent entries."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        entries = feedparser.parse(r.content).entries[:MAX_PER_FEED]
    except Exception as e:  # noqa: BLE001
        log_event("tool_error", tool="read_feed", error=str(e)[:160])
        return []
    source = urlparse(url).netloc.removeprefix("www.")
    out = []
    for e in entries:
        t = e.get("published_parsed") or e.get("updated_parsed")
        dt = datetime(*t[:6], tzinfo=timezone.utc) if t else None
        out.append(make_candidate(e.get("title"), e.get("link"), dt, source, e.get("summary", "")))
    return out


TOOL_FUNCS = {"search_news": search_news, "read_feed": read_feed}


def agent_search(state: "AgentState", have: list[dict]) -> list[dict]:
    """A real tool loop: the model decides what to search; capped at MAX_TOOL_CALLS."""
    model = llm.bind_tools(list(TOOL_FUNCS.values()))
    feedback = state.get("feedback") or "none"
    sources = ", ".join(sorted({c["source"] for c in have})) or "none"
    messages = [
        SystemMessage(content="You are a news researcher for a daily AI news digest. Use the tools to find "
                              "significant AI news from the last 24 hours that is missing from what we already have. "
                              f"You have a budget of {MAX_TOOL_CALLS} tool calls. When done, reply with the word DONE."),
        HumanMessage(content=f"We already have {len(have)} candidates from: {sources}.\n"
                             f"Checker feedback from the last attempt: {feedback}\n"
                             "Search for gaps: model releases, research results, funding, regulation, AI chips."),
    ]
    found: list[dict] = []
    calls = 0
    while calls < MAX_TOOL_CALLS:
        resp = call_llm("researcher", model, messages)
        messages.append(resp)
        if not resp.tool_calls:
            break
        for tc in resp.tool_calls:
            fn = TOOL_FUNCS.get(tc["name"])
            if fn is None or calls >= MAX_TOOL_CALLS:
                messages.append(ToolMessage(content="Tool unavailable or budget exhausted.", tool_call_id=tc["id"]))
                continue
            calls += 1
            try:
                results = fn(**tc["args"])
            except TypeError as e:
                results = []
                log_event("tool_error", tool=tc["name"], error=str(e)[:160])
            log_event("tool_call", tool=tc["name"], args=tc["args"], results=len(results))
            found.extend(results)
            brief = [{"title": r["title"], "published": r["published"]} for r in results]
            messages.append(ToolMessage(content=json.dumps(brief), tool_call_id=tc["id"]))
    return found


# ----------------------------------------------------------------------------
# 5. STATE
# ----------------------------------------------------------------------------
class AgentState(TypedDict, total=False):
    candidates: list[dict]
    picks: list[dict]
    verdict: dict
    feedback: str
    rejected_urls: list[str]
    check_failures: int
    human_rounds: int
    human_decision: Literal["Accept", "Reject"]
    rejection_log: Annotated[list[dict], operator.add]   # appended across nodes
    doubts: Annotated[list[str], operator.add]
    status: str
    report_path: str


# ----------------------------------------------------------------------------
# 6. NODES
# ----------------------------------------------------------------------------
def researcher_node(state: AgentState):
    print("\n[Node] Researcher: pulling feeds, Hacker News and searching for gaps...")
    candidates = list(state.get("candidates", []))
    if not candidates:
        candidates = merge_candidates(candidates, pull_feeds())
        candidates = merge_candidates(candidates, pull_hn())
    if USE_AGENT_SEARCH:
        candidates = merge_candidates(candidates, agent_search(state, candidates))
    eligible = len(eligible_candidates(candidates, state.get("rejected_urls", [])))
    log_event("researcher_done", candidates=len(candidates), eligible=eligible)
    print(f"        {len(candidates)} candidates, {eligible} eligible (fresh, unique)")
    return {"candidates": candidates}


class PickOut(BaseModel):
    id: int = Field(description="Number of the chosen candidate in the list")
    why_it_matters: str = Field(description="One sentence, max 30 words")


class DraftOut(BaseModel):
    picks: list[PickOut]


def drafter_node(state: AgentState):
    print("\n[Node] Drafter: choosing the top stories...")
    pool = eligible_candidates(state["candidates"], state.get("rejected_urls", []))[:MAX_DRAFT_CANDIDATES]
    now = datetime.now(timezone.utc)
    listing = "\n".join(
        f"{i}. {c['title']} | {c['source']} | {age_hours(c['published'], now):.0f}h ago | {c['snippet'][:160]}"
        for i, c in enumerate(pool, 1)
    )
    previous = ", ".join(p["title"] for p in state.get("picks", [])) or "none"
    prompt = (
        "You are the editor of a daily AI news digest for ML engineers and AI product builders.\n"
        f"From the numbered candidates, choose the {TOP_N} stories that matter most today. Prefer major model or "
        "product releases, research results, and funding, regulation or infrastructure moves with real consequences. "
        "Avoid pure marketing, tutorials, opinion pieces, and two entries about the same event. "
        "For each pick, write ONE sentence on why it matters.\n\n"
        f"Candidates:\n{listing}\n\n"
        f"Previous picks: {previous}\n"
        f"Feedback to address (keep picks that were fine, replace the rest): {state.get('feedback') or 'none'}"
    )
    out = ask_json("drafter", llm, prompt, DraftOut, MODEL)
    picks, used = [], set()
    for p in (out.picks if out else []):
        if 1 <= p.id <= len(pool) and p.id not in used and len(picks) < TOP_N:
            used.add(p.id)
            c = pool[p.id - 1]
            picks.append({**c, "why_it_matters": p.why_it_matters.strip()[:240]})
    log_event("drafter_done", picks=len(picks), pool=len(pool))
    return {"picks": picks}


def check_link(url: str) -> tuple[str, str]:
    """Returns (status, detail); status is ok, dead or unverifiable (site blocks bots)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=8, stream=True, allow_redirects=True)
        code = r.status_code
        r.close()
    except requests.RequestException as e:
        return "dead", type(e).__name__
    if code < 400:
        return "ok", ""
    if code in (401, 403, 429):
        return "unverifiable", f"HTTP {code}"
    return "dead", f"HTTP {code}"


def run_checks(picks: list[dict], candidates: list[dict], link_fn=check_link) -> dict:
    """Deterministic gate. Pure function, so the planted-error tests can run offline."""
    issues, bad_urls, doubts = [], [], []
    now = datetime.now(timezone.utc)
    known = {normalize_url(c["url"]) for c in candidates}

    def add(pick, kind, detail):
        issues.append({"pick": pick, "type": kind, "detail": detail})

    if len(picks) != TOP_N:
        add(None, "count", f"expected {TOP_N} picks, got {len(picks)}")

    for i, p in enumerate(picks, 1):
        nu = normalize_url(p["url"])
        if nu not in known:
            add(i, "not_in_candidates", "URL was not found by the researcher (possibly invented)")
            bad_urls.append(p["url"])
        age = age_hours(p.get("published"), now)
        if age is None:
            add(i, "no_date", "no publish date, freshness cannot be verified")
        elif age > FRESH_HOURS:
            add(i, "stale", f"published {age:.0f}h ago, limit is {FRESH_HOURS}h")
        elif age < -1:
            add(i, "future_date", "publish date is in the future")
        for j, q in enumerate(picks[: i - 1], 1):
            if normalize_url(q["url"]) == nu or similar_titles(q["title"], p["title"]):
                add(i, "duplicate", f"duplicates pick {j}")
                bad_urls.append(p["url"])
                break
        status, detail = link_fn(p["url"])
        if status == "dead":
            add(i, "dead_link", detail)
            bad_urls.append(p["url"])
        elif status == "unverifiable":
            doubts.append(f"Link for pick {i} could not be verified ({detail}); the site may block bots.")
    return {"pass": not issues, "issues": issues, "bad_urls": bad_urls, "doubts": doubts}


class Flag(BaseModel):
    pick: int
    reason: str


class FlagList(BaseModel):
    flags: list[Flag] = Field(default_factory=list)


def llm_review(picks: list[dict]) -> Optional[list[dict]]:
    """Judgment checks that code can't do. Returns issues, or None if the model's reply was unusable."""
    listing = "\n".join(f"{i}. {p['title']} ({p['source']}): {p['why_it_matters']}" for i, p in enumerate(picks, 1))
    prompt = (
        "You are a strict news editor reviewing a digest. Flag a pick ONLY if it is clearly (a) the same underlying "
        "event as another pick, or (b) marketing, a press release, a tutorial or opinion rather than news. "
        'Use the pick numbers. If everything is fine, return {"flags": []}.\n\n' + listing
    )
    out = ask_json("checker_llm", checker_llm, prompt, FlagList, CHECKER_MODEL)
    if out is None:
        return None
    return [{"pick": f.pick, "type": "llm_review", "detail": f.reason}
            for f in out.flags if 1 <= f.pick <= len(picks)]


def build_feedback(issues: list[dict], picks: list[dict]) -> str:
    lines = []
    for it in issues:
        n = it["pick"]
        title = picks[n - 1]["title"] if n and n <= len(picks) else ""
        who = f"Pick {n} ('{title}')" if n else "Overall"
        lines.append(f"{who}: {it['type']} - {it['detail']}")
    return "\n".join(lines)


def checker_node(state: AgentState):
    print("\n[Node] Checker: running code checks, then LLM review...")
    picks = state.get("picks", [])
    verdict = run_checks(picks, state["candidates"])
    extra_doubts = list(verdict["doubts"])

    # Gate first, evaluator second: only spend an LLM call once the code checks pass.
    if verdict["pass"] and USE_LLM_REVIEW:
        flagged = llm_review(picks)
        if flagged is None:
            extra_doubts.append("LLM judgment review returned unparseable output and was skipped.")
        elif flagged:
            verdict["issues"].extend(flagged)
            verdict["bad_urls"].extend(picks[f["pick"] - 1]["url"] for f in flagged)
            verdict["pass"] = False

    rejected = sorted(set(state.get("rejected_urls", [])) | set(verdict["bad_urls"]))
    verdict["valid_candidates"] = len(eligible_candidates(state["candidates"], rejected))
    update: dict = {"verdict": verdict, "rejected_urls": rejected, "doubts": extra_doubts}

    if verdict["pass"]:
        update["feedback"] = ""
        print("        PASS")
    else:
        failures = state.get("check_failures", 0) + 1
        update["check_failures"] = failures
        update["feedback"] = build_feedback(verdict["issues"], picks)
        update["rejection_log"] = [{"attempt": failures, "issues": verdict["issues"]}]
        print(f"        REJECT (failure {failures}/{MAX_CHECK_FAILURES}):\n" +
              "\n".join("          " + ln for ln in update["feedback"].splitlines()))
    log_event("checker_done", passed=verdict["pass"], issues=len(verdict["issues"]),
              valid_candidates=verdict["valid_candidates"])
    return update


def human_node(state: AgentState):
    if HUMAN_HOOK:  # web UI path
        approved, text = HUMAN_HOOK(state["picks"])
        log_event("human_decision", choice="approve" if approved else "reject")
        if approved:
            return {"human_decision": "Accept", "feedback": ""}
        return {"human_decision": "Reject", "feedback": text or "Human requested revisions.",
                "human_rounds": state.get("human_rounds", 0) + 1, "check_failures": 0}
    print("\n========================================================")
    print("DRAFT READY. PENDING HUMAN VERIFICATION:")
    print("========================================================")
    for i, p in enumerate(state["picks"], 1):
        print(f"{i}. {p['title']}\n   {p['url']}\n   {p['why_it_matters']}")
    print("========================================================\n")
    choice = input("Approve this digest? [approve / reject]: ").strip().lower()
    log_event("human_decision", choice=choice)
    if choice in ("approve", "a", "yes", "y"):
        return {"human_decision": "Accept", "feedback": ""}
    text = input("Revision instructions for the drafter: ").strip()
    return {"human_decision": "Reject", "feedback": text or "Human requested revisions.",
            "human_rounds": state.get("human_rounds", 0) + 1, "check_failures": 0}


# ----------------------------------------------------------------------------
# 7. REPORTER (deterministic: nothing here should be able to hallucinate)
# ----------------------------------------------------------------------------
def token_summary() -> tuple[list[str], float]:
    by_step: dict[str, list[int]] = {}
    for e in RUN_LOG:
        if e["event"] == "llm_call":
            row = by_step.setdefault(e["step"], [0, 0, 0])
            row[0] += 1
            row[1] += e["input_tokens"]
            row[2] += e["output_tokens"]
    lines = ["| step | calls | input tokens | output tokens |", "|---|---|---|---|"]
    t_in = t_out = 0
    for step, (n, i, o) in by_step.items():
        lines.append(f"| {step} | {n} | {i} | {o} |")
        t_in, t_out = t_in + i, t_out + o
    cost = t_in / 1e6 * PRICE_IN_PER_M + t_out / 1e6 * PRICE_OUT_PER_M
    return lines, cost


def decide_status(state: AgentState) -> str:
    if not state.get("verdict", {}).get("pass"):
        return f"CHECK_FAILED (gave up after {state.get('check_failures', 0)} failed checks)"
    if REQUIRE_HUMAN:
        return "APPROVED_BY_HUMAN" if state.get("human_decision") == "Accept" else "REJECTED_BY_HUMAN"
    return "PASSED_CHECKS_NO_HUMAN_REVIEW"


def save_picks(picks: list[dict], mode: str) -> Path:
    Path("reports").mkdir(exist_ok=True)
    path = Path("reports") / f"picks_{mode}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(picks, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def reporter_node(state: AgentState):
    print("\n[Node] Reporter: writing digest and run report...")
    status = decide_status(state)
    picks = state.get("picks", [])
    doubts = list(dict.fromkeys(state.get("doubts", [])))
    doubts += [f"Feed failed: {e['feed']} ({e['error']})" for e in RUN_LOG if e["event"] == "feed_error"]
    doubts += [f"Model output could not be parsed in step '{e['step']}'." for e in RUN_LOG if e["event"] == "parse_fail"]
    table, cost = token_summary()

    L = [f"# AI News Digest - {datetime.now().strftime('%Y-%m-%d')}", "", f"**Status:** {status}", "",
         "## Top picks", ""]
    for i, p in enumerate(picks, 1):
        L += [f"{i}. [{p['title']}]({p['url']}) ({p['source']}, {p['published']})",
              f"   - Why it matters: {p['why_it_matters']}"]
    L += ["", "## Run report", "",
          f"- Wall time: {time.time() - _run_start:.0f}s",
          f"- Candidates collected: {len(state.get('candidates', []))}",
          f"- Failed checks: {state.get('check_failures', 0)}, human rounds: {state.get('human_rounds', 0)}",
          "", "### Checker rejections", ""]
    log = state.get("rejection_log", [])
    L += [f"- Attempt {r['attempt']}: pick {i['pick']} - {i['type']} ({i['detail']})" for r in log for i in r["issues"]] \
        or ["- none"]
    L += ["", "### Tokens and cost", ""] + table + ["", f"Estimated cost: ${cost:.4f} "
          "(0 for a local model unless PRICE_IN_PER_M / PRICE_OUT_PER_M are set)", "", "### Open doubts", ""]
    L += [f"- {d}" for d in doubts] or ["- none"]
    text = "\n".join(L) + "\n"

    Path("reports").mkdir(exist_ok=True)
    path = Path("reports") / f"digest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    path.write_text(text, encoding="utf-8")
    save_picks(picks, "full")
    log_event("run_done", status=status, report=str(path))
    print(text)
    return {"status": status, "report_path": str(path)}


# ----------------------------------------------------------------------------
# 8. ROUTING AND GRAPH
# ----------------------------------------------------------------------------
def route_after_checker(state: AgentState) -> str:
    v = state["verdict"]
    if v["pass"]:
        return "human" if REQUIRE_HUMAN else "reporter"
    if state.get("check_failures", 0) >= MAX_CHECK_FAILURES:
        return "reporter"                       # give up and report honestly
    if v["valid_candidates"] < TOP_N:
        return "researcher"                     # not enough good stories: search again
    return "drafter"                            # enough stories: the picks were bad


def route_after_human(state: AgentState) -> str:
    if state.get("human_decision") == "Reject" and state.get("human_rounds", 0) < MAX_HUMAN_ROUNDS:
        return "drafter"
    return "reporter"


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("researcher", researcher_node)
    g.add_node("drafter", drafter_node)
    g.add_node("checker", checker_node)
    g.add_node("human", human_node)
    g.add_node("reporter", reporter_node)
    g.add_edge(START, "researcher")
    g.add_edge("researcher", "drafter")
    g.add_edge("drafter", "checker")
    g.add_conditional_edges("checker", route_after_checker,
                            {"human": "human", "reporter": "reporter", "researcher": "researcher", "drafter": "drafter"})
    g.add_conditional_edges("human", route_after_human, {"drafter": "drafter", "reporter": "reporter"})
    g.add_edge("reporter", END)
    return g.compile(checkpointer=MemorySaver())


# ----------------------------------------------------------------------------
# 9. PLANTED-ERROR TESTS (offline: no LLM, no network)
# ----------------------------------------------------------------------------
def run_checker_tests() -> int:
    now = datetime.now(timezone.utc)
    titles = ["OpenAI ships new reasoning model", "EU finalizes AI Act guidance for foundation models",
              "Nvidia unveils datacenter GPU roadmap", "Mistral raises new funding round",
              "DeepMind publishes protein design results", "Meta releases open weights vision model"]

    def story(n, hours=2.0, url=None, title=None):
        return {"title": title or titles[n], "url": url or f"https://site{n}.example/story-{n}",
                "published": (now - timedelta(hours=hours)).isoformat(timespec="seconds"),
                "source": f"site{n}.example", "snippet": "", "why_it_matters": "test"}

    def stub_link(url):  # dead if the host ends in .invalid, otherwise fine
        return ("dead", "DNS failure") if ".invalid" in url else ("ok", "")

    clean = [story(n) for n in range(5)]
    cases = [
        ("clean baseline (should pass)", clean, [], None),
        ("old story", clean[:2] + [story(5, hours=120)] + clean[3:], [story(5, hours=120)], "stale"),
        ("duplicate URL", clean[:1] + [story(0, url="https://site0.example/story-0/?utm_source=x")] + clean[2:], [], "duplicate"),
        ("same story, different outlet", clean[:1] + [story(5, title="OpenAI ships a new reasoning model")] + clean[2:],
         [story(5, title="OpenAI ships a new reasoning model")], "duplicate"),
        ("dead link", clean[:3] + [story(5, url="https://gone.invalid/story")] + clean[4:],
         [story(5, url="https://gone.invalid/story")], "dead_link"),
        ("invented story", clean[:3] + [story(5, url="https://made-up.example/never-existed")] + clean[4:], [], "not_in_candidates"),
    ]
    failures = 0
    for name, picks, extra_candidates, expected in cases:
        verdict = run_checks(picks, clean + extra_candidates, link_fn=stub_link)
        found = {i["type"] for i in verdict["issues"]}
        ok = verdict["pass"] if expected is None else expected in found
        failures += 0 if ok else 1
        print(f"{'OK     ' if ok else 'MISSED '} {name}: {sorted(found) or 'no issues'}")
    print(f"\n{len(cases) - failures}/{len(cases)} checker tests behaved as expected")
    return 1 if failures else 0


# ----------------------------------------------------------------------------
# 10. ENTRY POINT
# ----------------------------------------------------------------------------
def run_baseline() -> None:
    """Day 1 baseline: researcher plus ONE drafter call. No checker, no loops. Score this first."""
    init_run()
    candidates = merge_candidates(merge_candidates([], pull_feeds()), pull_hn())
    if USE_AGENT_SEARCH:
        candidates = merge_candidates(candidates, agent_search({}, candidates))
    picks = drafter_node({"candidates": candidates, "rejected_urls": [], "picks": [], "feedback": ""})["picks"]
    for i, p in enumerate(picks, 1):
        print(f"{i}. {p['title']}\n   {p['url']}\n   {p['why_it_matters']}")
    print(f"\nSaved: {save_picks(picks, 'baseline')}")


def main() -> None:
    global REQUIRE_HUMAN
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-checker", action="store_true", help="run planted-error tests (offline)")
    ap.add_argument("--baseline", action="store_true", help="Day 1 baseline without the checker")
    ap.add_argument("--no-human", action="store_true", help="skip the input() approval step")
    args = ap.parse_args()

    if args.test_checker:
        sys.exit(run_checker_tests())
    if args.no_human:
        REQUIRE_HUMAN = False
    try:
        require_config()
    except RuntimeError as e:
        sys.exit(str(e))
    if USE_AGENT_SEARCH and not os.environ.get("TAVILY_API_KEY"):
        sys.exit("TAVILY_API_KEY is not set. Set it, or run with USE_AGENT_SEARCH=0 to skip web search.")

    if args.baseline:
        run_baseline()
        return

    init_run()
    app = build_graph()
    config = {"configurable": {"thread_id": f"digest_{int(time.time())}"}, "recursion_limit": 60}
    final = app.invoke({"candidates": [], "rejected_urls": [], "check_failures": 0, "human_rounds": 0}, config=config)
    print(f"\nPIPELINE COMPLETE: {final.get('status')}\nReport: {final.get('report_path')}")


if __name__ == "__main__":
    main()