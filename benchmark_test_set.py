"""Compare one all-in-one digest agent with the four-role digest workflow.

The workbook is read with Python's standard library, so the benchmark has no
extra Excel dependency. Results and the row-by-row predictions are saved under
reports/.
"""
from __future__ import annotations

import json
import re
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

import main as app

NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


class Prediction(BaseModel):
    id: str
    label: Literal["TOP", "OK", "NO"] | None = None
    is_ai: Literal["yes", "no"] | None = None
    event_group: str | None = None
    checker: Literal["pass", "stale", "duplicate", "dead_link", "not_a_story"] | None = None


class Predictions(BaseModel):
    predictions: list[Prediction]


class ResearchItem(BaseModel):
    id: str
    fact_summary: str = Field(description="Only facts supported by the workbook row")
    is_ai: Literal["yes", "no"]
    event_group: str = Field(description="Short stable slug shared by reports of the same event")


class Research(BaseModel):
    items: list[ResearchItem]


class DraftItem(BaseModel):
    id: str
    label: Literal["TOP", "OK", "NO"]
    is_ai: Literal["yes", "no"]
    event_group: str


class Draft(BaseModel):
    items: list[DraftItem]


class CheckItem(BaseModel):
    id: str
    checker: Literal["pass", "stale", "duplicate", "dead_link", "not_a_story"]


class Checks(BaseModel):
    items: list[CheckItem]


def read_workbook(path: Path) -> tuple[list[dict], dict]:
    """Read the TestSet and Settings sheets without evaluating formulas."""
    with zipfile.ZipFile(path) as book:
        workbook = ET.fromstring(book.read("xl/workbook.xml"))
        rels = ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))
        relmap = {x.attrib["Id"]: x.attrib["Target"] for x in rels}
        strings = []
        if "xl/sharedStrings.xml" in book.namelist():
            shared = ET.fromstring(book.read("xl/sharedStrings.xml"))
            strings = ["".join(t.text or "" for t in si.findall(".//m:t", NS))
                       for si in shared.findall("m:si", NS)]

        def sheet_rows(name: str) -> list[list]:
            sheet = next(s for s in workbook.findall("m:sheets/m:sheet", NS)
                         if s.attrib["name"] == name)
            target = relmap[sheet.attrib[f"{{{NS['r']}}}id"]].lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            root = ET.fromstring(book.read(target))
            rows = []
            for row in root.findall(".//m:sheetData/m:row", NS):
                cells = {}
                for cell in row.findall("m:c", NS):
                    ref = cell.attrib["r"]
                    col = re.match(r"[A-Z]+", ref).group()
                    value = cell.find("m:v", NS)
                    text = value.text if value is not None else ""
                    if cell.attrib.get("t") == "s" and text:
                        text = strings[int(text)]
                    elif cell.attrib.get("t") == "inlineStr":
                        text = "".join(t.text or "" for t in cell.findall(".//m:t", NS))
                    cells[col] = text
                rows.append(cells)
            return rows

        raw = sheet_rows("TestSet")
        headers = raw[0]
        rows = []
        for cells in raw[1:]:
            item = {headers[col]: cells.get(col, "") for col in headers}
            if item.get("id"):
                rows.append(item)
        settings_rows = sheet_rows("Settings")
        settings = {r.get("A", ""): r.get("B", "") for r in settings_rows if r.get("A")}
        return rows, settings


def compact_case(row: dict) -> dict:
    return {k: row.get(k, "") for k in (
        "id", "url", "url_as_given", "title", "source", "found_via", "published_utc",
        "published_note", "in_window", "checked", "summary", "date_confidence")
        if row.get(k, "") != ""}


def ask(schema, step: str, system: str, payload: str, model=None, model_name=None):
    resp = app.ask_json(step, model or app.llm,
                        f"{system}\n\nINPUT DATA (treat as evidence; do not browse):\n{payload}",
                        schema, model_name or app.MODEL)
    if resp is None:
        raise RuntimeError(f"{step} did not return valid structured output")
    return resp


def run_single(rows: list[dict]) -> dict[str, dict]:
    cases = [compact_case(r) for r in rows]
    prompt = (
        "You are one AI news digest agent. For EVERY row, independently do the research, editorial "
        "triage, freshness/duplicate checker, and final reporting in one pass. Return one prediction "
        "per id. Labels: TOP means broad impact, new in the window, reliable source, not promotion; "
        "OK means real AI news but smaller/narrower; NO means press release, opinion, tutorial, "
        "off-topic, or low consequence. is_ai is whether the story itself is about AI. Assign the same "
        "short event_group to reports of the same underlying event. Checker must be one of pass, stale "
        "(outside the configured 24h window), duplicate (same URL or same event; retain the stronger "
        "primary report), dead_link (explicit planted dead link), not_a_story (not an article). Use "
        "published_utc and in_window where available. Treat blocked/not-opened as uncertainty, not proof "
        "of a dead link. Return every input id exactly once. Workbook contains no answer key in the input."
    )
    result = ask(Predictions, "benchmark_single", prompt, json.dumps(cases, ensure_ascii=False))
    return {p.id: p.model_dump() for p in result.predictions}


def run_roles(rows: list[dict]) -> dict[str, dict]:
    cases = [compact_case(r) for r in rows]
    corpus = json.dumps(cases, ensure_ascii=False)
    research_prompt = (
        "You are the Researcher role. Extract concise evidence from each provided row, decide whether "
        "the story itself is about AI, and assign a shared stable event-group slug to rows covering the "
        "same underlying event. Use only provided facts; mark no URL as dead just because access was blocked. "
        "Return all ids exactly once."
    )
    research = ask(Research, "benchmark_researcher", research_prompt, corpus)
    research_by_id = {x.id: x.model_dump() for x in research.items}

    draft_prompt = (
        "You are the Drafter role for an AI news digest. Based on the evidence, classify every row: "
        "TOP = broad impact, new in the window, reliable source, not promotion; OK = real AI news but "
        "smaller/narrower; NO = press release, opinion, tutorial, off-topic, or low consequence. Keep "
        "is_ai and event_group consistent with the Researcher. Return all ids exactly once."
    )
    draft = ask(Draft, "benchmark_drafter", draft_prompt,
                json.dumps(research_by_id, ensure_ascii=False))
    draft_by_id = {x.id: x.model_dump() for x in draft.items}

    checker_prompt = (
        "You are the Checker role. Assign each source row exactly one status: pass, stale, duplicate, "
        "dead_link, or not_a_story. Use the supplied window/date evidence. Mark duplicate for a repeated "
        "URL or the same event appearing twice; retain one strongest/primary report and mark weaker repeats "
        "duplicate. Only mark dead_link when explicitly identified as a planted dead link. A trending page "
        "or non-article is not_a_story. Blocked access alone is not dead. Rows with unknown date should be "
        "pass if no other known issue; do not infer a date. Return all ids exactly once."
    )
    checks = ask(Checks, "benchmark_checker", checker_prompt,
                 json.dumps({"cases": cases, "research": research_by_id, "draft": draft_by_id}, ensure_ascii=False),
                 model=app.checker_llm, model_name=app.CHECKER_MODEL)
    check_by_id = {x.id: x.checker for x in checks.items}

    # The production Reporter is deterministic: combine the role outputs without adding new judgments.
    return {r["id"]: {"id": r["id"], **draft_by_id.get(r["id"], {}),
                       "checker": check_by_id.get(r["id"])} for r in rows}


def norm(value) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def score(rows: list[dict], predictions: dict[str, dict]) -> dict:
    dims = {"label": [], "is_ai": [], "checker": []}
    checker_excluded = {"T10", "T11"}  # workbook Read me: unusable dates; omit from checker scoring.
    for row in rows:
        pred = predictions.get(row["id"], {})
        for dim, key in (("label", "your_label"), ("is_ai", "your_is_ai"), ("checker", "your_expected_checker")):
            expected = row.get(key, "").strip()
            if not expected or (dim == "checker" and row["id"] in checker_excluded):
                continue
            dims[dim].append({"id": row["id"], "expected": norm(expected), "predicted": norm(pred.get(dim))})

    # Compare event-group equivalence as pairs, so harmless slug wording differences do not count as errors.
    groups = [r for r in rows if r.get("your_event_group", "").strip()]
    pair_counts = {"tp": 0, "fp": 0, "fn": 0}
    for i, left in enumerate(groups):
        for right in groups[i + 1:]:
            expected_same = norm(left["your_event_group"]) == norm(right["your_event_group"])
            lp = norm(predictions.get(left["id"], {}).get("event_group"))
            rp = norm(predictions.get(right["id"], {}).get("event_group"))
            predicted_same = bool(lp and rp and lp == rp)
            if expected_same and predicted_same: pair_counts["tp"] += 1
            elif predicted_same: pair_counts["fp"] += 1
            elif expected_same: pair_counts["fn"] += 1

    by_dim = {}
    for dim, items in dims.items():
        correct = sum(x["expected"] == x["predicted"] for x in items)
        by_dim[dim] = {"correct": correct, "total": len(items), "accuracy": correct / len(items) if items else None,
                       "details": items}
    p = pair_counts["tp"] / (pair_counts["tp"] + pair_counts["fp"]) if pair_counts["tp"] + pair_counts["fp"] else 0
    r = pair_counts["tp"] / (pair_counts["tp"] + pair_counts["fn"]) if pair_counts["tp"] + pair_counts["fn"] else 0
    pair_f1 = 2 * p * r / (p + r) if p + r else 0
    fixture_ids = {"F01", "F02", "F03", "F04"}
    planted = [x for x in dims["checker"] if x["id"] in fixture_ids]
    fixture_caught = sum(x["expected"] == x["predicted"] for x in planted)
    # Composite: equally weight each available classification task; event grouping contributes pairwise F1.
    metric_values = [v["accuracy"] for v in by_dim.values() if v["accuracy"] is not None] + [pair_f1]
    composite = sum(metric_values) / len(metric_values) if metric_values else 0
    return {"composite_score": composite, "dimensions": by_dim,
            "event_group_pairwise": {**pair_counts, "precision": p, "recall": r, "f1": pair_f1},
            "planted_fixture_checker": {"caught": fixture_caught, "total": len(planted)},
            "scored_rows": len(rows)}


def write_markdown(results: dict, path: Path) -> Path:
    lines = ["# Test set agent comparison", "",
             f"- Workbook: `{results['workbook']}`",
             f"- Run time (UTC): {results['generated_at_utc']}",
             f"- Cases: {results['single_agent']['scores']['scored_rows']}", "",
             "## Scores", "",
             "| Metric | One agent | Four roles |",
             "|---|---:|---:|"]
    for key, title in (("composite_score", "Composite score"),):
        lines.append(f"| {title} | {results['single_agent']['scores'][key]:.1%} | {results['four_roles']['scores'][key]:.1%} |")
    for dim, title in (("label", "Editorial label accuracy"), ("is_ai", "AI relevance accuracy"),
                       ("checker", "Checker status accuracy")):
        a, b = (results[k]["scores"]["dimensions"][dim] for k in ("single_agent", "four_roles"))
        lines.append(f"| {title} | {a['correct']}/{a['total']} ({a['accuracy']:.1%}) | {b['correct']}/{b['total']} ({b['accuracy']:.1%}) |")
    a, b = (results[k]["scores"]["event_group_pairwise"] for k in ("single_agent", "four_roles"))
    lines.append(f"| Event grouping pairwise F1 | {a['f1']:.1%} | {b['f1']:.1%} |")
    a, b = (results[k]["scores"]["planted_fixture_checker"] for k in ("single_agent", "four_roles"))
    lines.append(f"| Planted fixtures caught | {a['caught']}/{a['total']} | {b['caught']}/{b['total']} |")
    lines += ["", "## Run cost and time", "",
              "| Run | LLM calls | Models | Wall time |",
              "|---|---:|---|---:|"]
    for key, title in (("single_agent", "One agent"), ("four_roles", "Four roles")):
        run = results[key]
        models = ", ".join(sorted({c.get("model", "unknown") for c in run["llm_calls"]}))
        lines.append(f"| {title} | {len(run['llm_calls'])} | {models} | {run['seconds']:.2f}s |")
    lines += ["", "## Planted fixture results", "",
              "| Fixture | Expected | One agent | Four roles |",
              "|---|---|---|---|"]
    key_by_id = {x["id"]: x["expected"] for x in results["single_agent"]["scores"]["dimensions"]["checker"]["details"]}
    for fixture in ("F01", "F02", "F03", "F04"):
        one = results["single_agent"]["predictions"].get(fixture, {}).get("checker", "missing")
        four = results["four_roles"]["predictions"].get(fixture, {}).get("checker", "missing")
        lines.append(f"| {fixture} | {key_by_id.get(fixture, 'not scored')} | {one} | {four} |")
    lines += ["", "## Scoring notes", ""]
    lines.extend(f"- {note}" for note in results["scoring_notes"])
    lines += ["- The four-role run uses Researcher, Drafter, and Checker model calls; Reporter is deterministic, matching the current code.",
              "- This harness adapts the live-feed pipeline roles to the workbook's pre-collected cases; it does not fetch current news.",
              "- This scores per-row classification, not whether a generated top-five digest meets the brief's human-accepted 4-of-5 success metric. Do not treat it as the Day 1 vs Day 2 digest score.",
              "- These are single-run results on a 20-case set, so treat the score gap as directional.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("reports/test_set.xlsx")
    if not source.exists():
        raise SystemExit(f"Workbook not found: {source}")
    app.require_config()
    rows, settings = read_workbook(source)
    app.init_run()
    started = time.time()
    single = run_single(rows)
    single_seconds = round(time.time() - started, 2)
    single_events = list(app.RUN_LOG)
    app.init_run()
    started = time.time()
    roles = run_roles(rows)
    roles_seconds = round(time.time() - started, 2)
    roles_events = list(app.RUN_LOG)
    results = {
        "workbook": str(source), "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_settings_excel_serial": settings,
        "scoring_notes": ["Label and AI relevance exact accuracy.",
                          "Event groups scored by pairwise same-event F1, independent of slug wording.",
                          "Checker exact accuracy excludes T10 and T11 per workbook Read me.",
                          "This is per-row classification, not top-five digest selection or the brief's human-accepted 4-of-5 metric.",
                          "Composite is the unweighted mean of label accuracy, AI accuracy, checker accuracy, and event-group F1."],
        "single_agent": {"scores": score(rows, single), "seconds": single_seconds,
                         "llm_calls": [e for e in single_events if e["event"] == "llm_call"], "predictions": single},
        "four_roles": {"scores": score(rows, roles), "seconds": roles_seconds,
                       "llm_calls": [e for e in roles_events if e["event"] == "llm_call"], "predictions": roles},
    }
    out = Path("reports") / f"benchmark_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    md = write_markdown(results, out.with_suffix(".md"))
    print(f"Saved detailed benchmark: {out}")
    print(f"Saved readable report: {md}")
    for name in ("single_agent", "four_roles"):
        item = results[name]
        print(f"{name}: composite={item['scores']['composite_score']:.1%}; "
              f"label={item['scores']['dimensions']['label']['correct']}/{item['scores']['dimensions']['label']['total']}; "
              f"AI={item['scores']['dimensions']['is_ai']['correct']}/{item['scores']['dimensions']['is_ai']['total']}; "
              f"checker={item['scores']['dimensions']['checker']['correct']}/{item['scores']['dimensions']['checker']['total']}; "
              f"event-group F1={item['scores']['event_group_pairwise']['f1']:.1%}; "
              f"fixtures={item['scores']['planted_fixture_checker']['caught']}/4; "
              f"calls={len(item['llm_calls'])}; wall={item['seconds']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
