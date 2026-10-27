"""Agentic analyst: a local LLM plans tool calls over the warehouse, the models and the reviews.

Tools
  query_warehouse   read-only SQL on the DuckDB warehouse (one SELECT, row-capped, no file access)
  area_profile      price, separation and day-ahead forecast skill for one planning area
  hub_options       predefined vs clustered pricing hubs on AESO's two hub metrics
  run_what_if       SCED on the stylized Alberta network: add south-east wind, uprate a corridor
  pdup_review       outcome and top findings of a PDUP model review
  lmp_example       AESO's three-bus examples priced by the SCED engine
The model chooses the tools and writes the answer from their results; every number in an answer
comes from a tool. Without a local model server a deterministic planner picks tools by intent and
fills a template, so the workflow still runs, and is tested, offline.
"""

from __future__ import annotations

import json
import re
import time
from functools import lru_cache
from typing import Any

import duckdb

from lmplab import grid, sced
from lmplab.areas import NAMES
from lmplab.config import Settings
from lmplab.llm import LLMUnavailable, LocalLLM
from lmplab.pdup import MATERIALITY

MAX_STEPS = 6
FORBIDDEN = re.compile(r"(?i)\b(insert|update|delete|drop|create|alter|attach|detach|copy|pragma|install|load|"
                       r"export|import|call|set|read_csv|read_parquet|read_json|glob)\b")

SCHEMA = """Warehouse (DuckDB, AESO 2025 area-level backcast, hour-beginning MST):
  area(area_id, name, region, hub_region)                       42 planning areas
  v_hour(ts, area_id, name, region, hub_region, load_mw, sysgen_mw, net_gen_mw, lmp)   one row per area-hour
  v_system(ts, load_mw, sysgen_mw, net_import_mw, alp, lmp_min, lmp_max)               one row per hour
  alp is the Alberta Load Price (load-weighted LMP); basis = lmp - alp."""

SYSTEM_PROMPT = f"""You are a grid analytics assistant for Alberta's locational marginal pricing (LMP) study.
Use the tools for every number you report; never invent figures. Prefer the specific tools over SQL.
Every tool result has a "facts" field of verified sentences: build the answer from those sentences, keeping
their numbers and their directions (above/below, rises/falls) exactly. Call all the tools the question needs
before answering. When a tool returns an error, correct the call once or say what failed.
Answer in at most 140 words, plain English, and name the tool behind each number in brackets.
{SCHEMA}"""


def _function(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": properties, "required": required}}}


TOOLS = [
    _function("query_warehouse", "Run one read-only SELECT on the warehouse and return up to 50 rows.",
              {"sql": {"type": "string", "description": "A single DuckDB SELECT statement over area, v_hour, v_system."}},
              ["sql"]),
    _function("area_profile", "Price level, separation from the Alberta Load Price, and day-ahead forecast skill for a planning area.",
              {"area": {"type": "string", "description": "Planning area name (e.g. 'Medicine Hat') or number (e.g. '4')."}},
              ["area"]),
    _function("hub_options", "Compare predefined and clustered pricing hub designs on basis deviation and minimum separation.", {}, []),
    _function("run_what_if", "Dispatch the stylized Alberta network with N-1 security for sampled hours, with added south-east "
              "wind and/or an uprated corridor, and compare with today's system.",
              {"add_se_wind_mw": {"type": "number", "description": "MW of wind added in the Southeast zone."},
               "uprate_corridor": {"type": "string", "description": "Corridor to uprate, e.g. 'SW-SE', 'CEN-SE', 'NW-EDM'."},
               "uprate_mw": {"type": "number", "description": "MW added to every circuit of the corridor."}}, []),
    _function("pdup_review", "Outcome and main findings of the automated PDUP model review for a project.",
              {"project": {"type": "string", "description": "Project name or id, e.g. 'Prairie Ridge' or 'SYN-0002'."}},
              ["project"]),
    _function("lmp_example", "AESO's three-bus SCED example (1, 2 or 3) priced by the engine: dispatch, LMP components, "
              "and the prices AESO published.",
              {"number": {"type": "integer", "description": "Example number 1, 2 or 3."}}, ["number"]),
]


# --- tools ---------------------------------------------------------------------------------

class Toolbox:
    def __init__(self, settings: Settings, what_if_hours: int = 120):
        self.settings = settings
        self.what_if_hours = what_if_hours

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, name, None)
        if name not in {tool["function"]["name"] for tool in TOOLS} or handler is None:
            return {"error": f"Unknown tool {name}"}
        try:
            output = handler(**arguments)
        except TypeError as error:
            return {"error": f"Bad arguments for {name}: {error}"}
        if "error" not in output:
            # Verified sentences alongside the raw fields: small models paraphrase far more reliably than they read tables.
            output["facts"] = _sentence({"tool": name, "result": output})
        return output

    def _report(self, name: str) -> dict[str, Any]:
        return _load_report(str(self.settings.paths.reports / f"{name}.json"))

    def query_warehouse(self, sql: str) -> dict[str, Any]:
        text = sql.strip().rstrip(";").strip()
        if ";" in text or not re.match(r"(?is)^(select|with)\b", text) or FORBIDDEN.search(text):
            return {"error": "Only one read-only SELECT statement over the warehouse tables is allowed."}
        con = duckdb.connect(str(self.settings.paths.warehouse), read_only=True)
        try:
            con.execute("SET enable_external_access = false")
            frame = con.execute(f"SELECT * FROM ({text}) AS answer LIMIT 50").df()
        except duckdb.Error as error:
            return {"error": str(error).splitlines()[0][:300]}
        finally:
            con.close()
        rows = [[_plain(v) for v in row] for row in frame.itertuples(index=False)]
        return {"columns": list(frame.columns), "rows": rows, "row_count": len(rows)}

    def area_profile(self, area: str) -> dict[str, Any]:
        area_id = find_area(str(area))
        if area_id is None:
            return {"error": f"No planning area matches {area!r}"}
        analytics, forecast = self._report("analytics"), self._report("forecast")
        stats = next(a for a in analytics["areas"] if a["area_id"] == area_id)
        skill = next(a for a in forecast["per_area"] if a["area_id"] == area_id)
        region = stats["hub_region"]
        drivers = forecast["drivers"]["by_region"][region]
        top = sorted(((forecast["labels"].get(k, k), v) for k, v in drivers.items() if k != "area_id"), key=lambda kv: -kv[1])[:3]
        return {"area": f"{area_id} {NAMES[area_id]}", "region": region, "mean_lmp": stats["mean_lmp"],
                "mean_basis": stats["mean_basis"], "share_separated": stats["separated_share"],
                "share_below_alp": stats["below_share"], "share_above_alp": stats["above_share"],
                "mean_net_generation_mw": stats["mean_net_gen_mw"], "annual_alp": analytics["summary"]["annual_alp"],
                "forecast_mae": skill["mae"], "uniform_price_mae": skill["mae_uniform"],
                "interval_coverage": skill["coverage"], "separation_auc": skill["auc"],
                "top_drivers_in_region": [{"feature": k, "mean_abs_shap": round(v, 3)} for k, v in top]}

    def hub_options(self) -> dict[str, Any]:
        hubs = self._report("analytics")["hubs"]
        rows = [{"design": s["name"], "hubs": s["hubs"], "basis_deviation": s["basis_deviation"],
                 "min_separation": s["min_separation"]} for s in hubs["scenarios"]]
        return {"designs": rows, "aeso_reference": hubs["aeso_reference"],
                "note": "Lower basis deviation fits areas better; higher minimum separation gives distinct hub prices."}

    def run_what_if(self, add_se_wind_mw: float = 0.0, uprate_corridor: str | None = None,
                    uprate_mw: float = 0.0) -> dict[str, Any]:
        case = grid.load_case(self.settings.paths.configs / "alberta7.yaml")
        corridors = sorted(set(grid.corridors(case).values()))
        if uprate_corridor and uprate_corridor not in corridors:
            return {"error": f"Unknown corridor {uprate_corridor!r}; choose from {corridors}"}
        hours = grid.sample_hours(case, self.settings.seed)[: self.what_if_hours]
        spec: dict[str, Any] = {}
        if add_se_wind_mw:
            spec["add_capacity"] = {"SE wind": float(add_se_wind_mw)}
        if uprate_corridor and uprate_mw:
            spec["uprate"] = {uprate_corridor: float(uprate_mw)}
        base, scenario = grid.summarize(case, {}, hours), grid.summarize(case, spec, hours)
        zones = {z: {"today": base["zones"][z]["mean"], "scenario": scenario["zones"][z]["mean"]} for z in base["zones"]}
        return {"hours_sampled": len(hours), "scenario": spec, "mean_price_by_zone": zones,
                "alp": {"today": base["alp_mean"], "scenario": scenario["alp_mean"]},
                "curtailment_mw": {"today": base["curtailment_mw"], "scenario": scenario["curtailment_mw"]},
                "production_cost_saving_musd_per_year": round((base["production_cost_per_hour"]
                                                               - scenario["production_cost_per_hour"]) * 8760 / 1e6, 1),
                "binding_limits_scenario": dict(list(scenario["binding_share"].items())[:4]),
                "rating_value_kusd_per_mw_year": dict(list(scenario["rating_value_kusd_per_mw_year"].items())[:4]),
                "note": "Stylized seven-zone network, not AESO's model."}

    def pdup_review(self, project: str) -> dict[str, Any]:
        reviews = self._report("pdup")["reviews"]
        wanted = str(project).lower()
        match = next((r for r in reviews if wanted in r["project"]["name"].lower() or wanted == r["project"]["id"].lower()), None)
        if match is None:
            return {"error": f"No review for {project!r}; available: {[r['project']['name'] for r in reviews]}"}

        def order(f: dict[str, Any]) -> tuple[int, int]:
            rank = MATERIALITY.index(f["rule"]) if f["rule"] in MATERIALITY else len(MATERIALITY)
            return (0 if f["severity"] == "error" else 1, rank)

        top = [{"rule": f["rule"], "severity": f["severity"], "element": f["element"], "finding": f["message"],
                "probable_cause": f.get("probable_cause")} for f in sorted(match["findings"], key=order)[:8]]
        return {"project": match["project"]["name"], "outcome": match["outcome"], "deficiencies": match["errors"],
                "clarifications": match["warnings"], "top_findings": top}

    def lmp_example(self, number: int) -> dict[str, Any]:
        examples = sced.aeso_examples()
        if not 1 <= int(number) <= len(examples):
            return {"error": "Choose example 1, 2 or 3"}
        example = examples[int(number) - 1]
        return {key: example[key] for key in ("name", "dispatch", "flows", "prices", "binding", "published",
                                               "matches_published", "incremental_cost")}


@lru_cache(maxsize=8)
def _load_report(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _plain(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat(sep=" ")
    if isinstance(value, float):
        return round(value, 3)
    return value.item() if hasattr(value, "item") else value


def find_area(text: str) -> int | None:
    text = text.strip().lower()
    match = re.fullmatch(r"(?:area\s*)?(\d{1,2})", text) or re.search(r"\barea\s+(\d{1,2})\b", text)
    if match and int(match.group(1)) in NAMES:
        return int(match.group(1))
    for area_id, name in sorted(NAMES.items(), key=lambda kv: -len(kv[1])):
        if name.lower() in text or any(part.lower() == text for part in name.split("/")):
            return area_id
    return None


# --- the agent -----------------------------------------------------------------------------

def ask(settings: Settings, question: str, llm: LocalLLM | None = None, toolbox: Toolbox | None = None) -> dict[str, Any]:
    toolbox = toolbox or Toolbox(settings)
    started = time.time()
    try:
        client = llm or LocalLLM()
        model = client.model()
        result = _llm_loop(client, toolbox, question)
        result["mode"] = f"local LLM ({model})"
    except LLMUnavailable as error:
        result = _planned(toolbox, question)
        result["mode"] = "deterministic planner"
        result["llm_status"] = str(error)
    result["question"] = question
    result["grounding"] = grounding(result["answer"], result["steps"], question)
    result["seconds"] = round(time.time() - started, 1)
    return result


NUMBER = re.compile(r"(?<![\w.])[-+]?\$?\d[\d,]*(?:\.\d+)?%?")


def grounding(answer: str, steps: list[dict[str, Any]], question: str = "") -> dict[str, Any]:
    """Trace every number in the answer to a tool result (or the question), allowing for rounding.
    An answer with untraceable numbers is flagged, never silently shown as fact."""
    pool: list[float] = [float(v) for v in re.findall(r"\d+(?:\.\d+)?", question.replace(",", ""))]

    def walk(value: Any) -> None:
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            pool.append(float(value))
        elif isinstance(value, str):
            pool.extend(float(v) for v in re.findall(r"\d+(?:\.\d+)?", value.replace(",", "")))
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    for step in steps:
        walk(step["result"])
        walk(step["arguments"])
    traced, untraced = [], []
    for token in NUMBER.findall(answer):
        raw = token.replace("$", "").replace(",", "").lstrip("+")
        percent = raw.endswith("%")
        try:
            value = abs(float(raw.rstrip("%")))
        except ValueError:
            continue
        candidates = [value, value / 100] if percent else [value]
        ok = any(abs(c - abs(t)) <= max(0.005 * abs(t), 0.006) or (c == round(abs(t)) and abs(c - abs(t)) < 0.5)
                 or (c == round(abs(t), 1) and abs(c - abs(t)) < 0.05) for c in candidates for t in pool)
        (traced if ok else untraced).append(token)
    return {"numbers": len(traced) + len(untraced), "traced": len(traced), "untraced": untraced}


def _llm_loop(client: LocalLLM, toolbox: Toolbox, question: str) -> dict[str, Any]:
    # "/no_think" turns off the reasoning trace of Qwen3-family models; other models ignore it.
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT},
                                      {"role": "user", "content": f"{question} /no_think"}]
    steps = []
    for _ in range(MAX_STEPS):
        message = client.complete(messages, tools=TOOLS, max_tokens=2500)
        calls = message.get("tool_calls") or []
        if not calls:
            answer = (message.get("content") or "").split("</think>")[-1].strip()
            if not answer:   # the model spent its budget reasoning: answer from the tools' verified facts instead
                return {"steps": steps, "answer": " ".join(_sentence(s) for s in steps),
                        "answer_source": "tool facts (the model returned no text)"}
            return {"steps": steps, "answer": answer, "answer_source": "model"}
        messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": [
            {"id": c.get("id", f"call_{i}"), "type": "function",
             "function": {"name": c["function"]["name"], "arguments": c["function"].get("arguments") or "{}"}}
            for i, c in enumerate(calls)]})
        for i, call in enumerate(calls):
            name, arguments = call["function"]["name"], call["function"].get("parsed", {})
            output = toolbox.call(name, arguments)
            steps.append({"tool": name, "arguments": arguments, "result": output})
            messages.append({"role": "tool", "tool_call_id": call.get("id", f"call_{i}"),
                             "content": json.dumps(output, default=str)[:4000]})
    return {"steps": steps, "answer": " ".join(_sentence(s) for s in steps),
            "answer_source": "tool facts (the model hit the step limit)"}


def plan(question: str) -> list[tuple[str, dict[str, Any]]]:
    """Intent rules for the offline path: which tools a question needs, with arguments."""
    q = question.lower()
    calls: list[tuple[str, dict[str, Any]]] = []
    if "pdup" in q or "model review" in q or "prairie ridge" in q or "coulee" in q:
        calls.append(("pdup_review", {"project": "Coulee" if "coulee" in q else "Prairie Ridge"}))
    if re.search(r"what if|what-if|new wind|more wind|upgrade|uprat|corridor", q):
        sizes = [float(m.replace(",", "")) for m in re.findall(r"(\d[\d,]*)\s*mw", q)]
        wind = {"add_se_wind_mw": sizes[0] if sizes and "wind" in q else 0.0}
        corridor = re.search(r"\b(nw-edm|ne-edm|edm-cen|edm-cal|cen-cal|cal-sw|cen-se|sw-se|ne-se)\b", q)
        if wind["add_se_wind_mw"]:
            calls.append(("run_what_if", wind))   # the change on its own first, so the upgrade can be compared with it
        if corridor:
            calls.append(("run_what_if", {**wind, "uprate_corridor": corridor.group(1).upper(),
                                          "uprate_mw": sizes[-1] if sizes else 400.0}))
        if not wind["add_se_wind_mw"] and not corridor:
            calls.append(("run_what_if", wind))
    if "hub" in q:
        calls.append(("hub_options", {}))
    example = re.search(r"example\s*([123])", q)
    if example or "three-bus" in q or "3-bus" in q:
        calls.append(("lmp_example", {"number": int(example.group(1)) if example else 2}))
    area = find_area(q)
    if area is not None:
        calls.append(("area_profile", {"area": str(area)}))
    if re.search(r"how many|which areas|count|\$\s?1,?000|price cap", q):
        calls.append(("query_warehouse", {"sql": "SELECT name, count(*) AS hours_at_cap FROM v_hour WHERE lmp >= 999.99 "
                                                 "GROUP BY name ORDER BY hours_at_cap DESC"}))
    if not calls:
        calls.append(("query_warehouse", {"sql": "SELECT name, round(avg(lmp), 2) AS mean_lmp FROM v_hour GROUP BY name "
                                                 "ORDER BY mean_lmp DESC LIMIT 5"}))
    return calls


def _planned(toolbox: Toolbox, question: str) -> dict[str, Any]:
    steps = [{"tool": name, "arguments": args, "result": toolbox.call(name, args)} for name, args in plan(question)]
    sentences = []
    for i, step in enumerate(steps):
        before = next((s for s in reversed(steps[:i]) if s["tool"] == step["tool"] == "run_what_if"), None)
        sentences.append(_sentence(step, before))
    return {"steps": steps, "answer": " ".join(sentences), "answer_source": "tool facts (deterministic planner)"}


def _sentence(step: dict[str, Any], before: dict[str, Any] | None = None) -> str:
    r, tool = step["result"], step["tool"]
    if "error" in r:
        return f"[{tool}] failed: {r['error']}"
    if tool == "run_what_if" and before is not None and "error" not in before["result"]:
        prior = before["result"]
        corridor, size = next(iter(r["scenario"]["uprate"].items()))
        return (f"Adding the {size:g} MW uprate of {corridor} on top cuts curtailment from {prior['curtailment_mw']['scenario']} to "
                f"{r['curtailment_mw']['scenario']} MW, moves the south-east price to ${r['mean_price_by_zone']['SE']['scenario']}/MWh "
                f"and saves a further ${round(r['production_cost_saving_musd_per_year'] - prior['production_cost_saving_musd_per_year'], 1)}M "
                f"a year [run_what_if, stylized network].")
    if tool == "area_profile":
        side = "below" if r["share_below_alp"] >= r["share_above_alp"] else "above"
        return (f"{r['area']} averaged ${r['mean_lmp']}/MWh against an annual ALP of ${r['annual_alp']}, separating "
                f"from it {r['share_separated']:.0%} of hours, mostly {side} it; its mean net generation is "
                f"{r['mean_net_generation_mw']:+.0f} MW, the signature of {'surplus supply behind a constraint' if side == 'below' else 'a load pocket'}. "
                f"The day-ahead model's error is ${r['forecast_mae']:.2f}/MWh against ${r['uniform_price_mae']:.2f} for a uniform price, "
                f"its 80% bands cover {r['interval_coverage']:.0%}, and the strongest drivers in the {r['region']} are "
                f"{', '.join(d['feature'].lower() for d in r['top_drivers_in_region'])} [area_profile].")
    if tool == "run_what_if":
        se = r["mean_price_by_zone"]["SE"]
        spec = r["scenario"]
        what = " and ".join(filter(None, [f"{spec['add_capacity']['SE wind']:g} MW of south-east wind" if "add_capacity" in spec else "",
                                          f"a {next(iter(spec['uprate'].values())):g} MW uprate of {next(iter(spec['uprate']))}" if "uprate" in spec else ""]))
        return (f"With {what or 'no change'}, the south-east price moves from ${se['today']} to ${se['scenario']}/MWh, curtailment "
                f"from {r['curtailment_mw']['today']} to {r['curtailment_mw']['scenario']} MW on average, and production cost "
                f"falls ${r['production_cost_saving_musd_per_year']}M a year across {r['hours_sampled']} sampled hours; the most "
                f"valuable rating is then {next(iter(r['rating_value_kusd_per_mw_year']), 'none')} [run_what_if, stylized network].")
    if tool == "pdup_review":
        first = [f for f in r["top_findings"] if f["severity"] == "error"][:3]
        return (f"{r['project']}: {r['outcome']} with {r['deficiencies']} deficiencies and {r['clarifications']} clarifications. "
                + (f"Fix first: {'; '.join(f['rule'] + ' ' + f['element'] + ' - ' + f['finding'] for f in first)} [pdup_review]."
                   if first else "[pdup_review]"))
    if tool == "hub_options":
        best = min((d for d in r["designs"] if d["hubs"] == 2), key=lambda d: d["basis_deviation"])
        return (f"Among two-hub designs, {best['design']} has basis deviation ${best['basis_deviation']} and minimum "
                f"separation ${best['min_separation']}/MWh [hub_options].")
    if tool == "lmp_example":
        prices = ", ".join(f"{p['bus']} ${p['lmp']:g}" for p in r["prices"])
        verdict = "matching AESO" if r["matches_published"] else f"differing from AESO's published {r['published']}"
        return f"{r['name']}: LMPs {prices}, {verdict} [lmp_example]."
    if tool == "query_warehouse":
        if not r["rows"]:
            return "The query returned no rows [query_warehouse]."
        listed = "; ".join(", ".join(str(v) for v in row) for row in r["rows"][:6])
        return f"Query result ({', '.join(r['columns'])}): {listed} [query_warehouse]."
    return ""


DEMO_QUESTIONS = [
    "Why does Medicine Hat price below the Alberta Load Price, and how well can we forecast its basis a day ahead?",
    "If 400 MW of new wind connects in the Southeast, what happens to prices and curtailment, and does uprating the "
    "SW-SE corridor by 400 MW help?",
    "Is the Prairie Ridge Wind PDUP ready for interconnection studies? What must the developer fix first?",
    "In the 2025 backcast, how many hours did each planning area reach the $1,000/MWh cap?",
]


def run(settings: Settings, questions: list[str] | None = None) -> dict[str, Any]:
    toolbox = Toolbox(settings)
    transcripts = [ask(settings, question, toolbox=toolbox) for question in questions or DEMO_QUESTIONS]
    report = {"tools": [tool["function"] for tool in TOOLS], "transcripts": transcripts}
    settings.paths.reports.mkdir(parents=True, exist_ok=True)
    (settings.paths.reports / "agent.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8", newline="\n")
    return report
