#!/usr/bin/env python3
"""Project state for the vk-ads-keywords skill: categories, phrases, minus words, Wordstat measurements.

One research project is one folder with state.json. The skill runs five steps in order, each once:
1 phrases -> 2 Wordstat demand -> 2.1 junk and double-meaning cleaning -> 3 enough queries? ->
4 expansion -> result. Every phrase belongs to one category (one kind of phrase: business
education, finance education, book authors ...). A city campaign joins the categories into one
audience; a nationwide campaign runs one audience per category. This tool keeps the parts that
have to be exact and repeatable:

  init / card / fork   project card: offer, intent, region, mode, target; a copy for another region
  add / rm / move      phrases by category; refuse duplicates and additions that cannot add volume
  minus                minus words that never cut a phrase of the list
  audit / checked      per-phrase double-meaning check (script for Wordstat, then the verdicts)
  js / record          OR expressions for the whole list and each category, the in-browser batch
                       script, then what Wordstat returned, with evidence and deltas
  cleaned / expanded   close step 2.1 and step 4
  status               the next step and the numbers behind it
  candidates           new bases from observed Wordstat rows for step 4
  export               copy-ready lists per category and the final report

Lists come from stdin (UTF-8, one item per line) or --in FILE.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ru_stem import STOP_WORDS, signature, significant_words, stem  # noqa: E402

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "wordstat_batch.js"
AUDIT_TEMPLATE = HERE / "wordstat_audit.js"
REGIONS = HERE / "regions.txt"
STATE, PENDING, EVIDENCE, EXPORT = "state.json", "pending.json", "evidence", "export"

# Wordstat queries per 30 days. A VK Ads keyword audience is smaller than Wordstat demand:
# the operators' rule of thumb is ~15 000 queries for ~10 000 people.
TARGET_DEFAULT = 15_000
MODES = {"city": "город: категории объединяются в одну аудиторию",
         "rf": "вся РФ: отдельная аудитория на каждую категорию"}
# getTable returned exact totals for a 22 521-character, 2 516-word OR in testing;
# above this cap the group is split into packets and reported as a range.
MAX_EXPR_CHARS = 20_000
MAX_WORDS = 7  # Wordstat and Direct reject longer keyword phrases
DEVICES = "desktop,phone,tablet"
# Rows travel through the agent (get_page_text -> record), so keep them to what review needs.
GROUP_ROWS, PROBE_ROWS, SIMILAR_ROWS = 30, 20, 15
CATEGORY_ROWS, CATEGORY_SIMILAR = 10, 5
SHOW_GROUP_ROWS, SHOW_PROBE_ROWS, SHOW_SIMILAR_ROWS = 25, 15, 10
AUDIT_BATCH, AUDIT_ROWS, AUDIT_SIMILAR = 25, 12, 10
WINDOW_DAYS = 30  # the popular table covers 30 days ending at period.endDate
MSK = dt.timezone(dt.timedelta(hours=3))

# Rows that look like another meaning of a phrase. Hints for the audit only: the verdict is the
# agent's, made on the rows themselves.
MARKERS = [
    ["фильм/сериал", r"фильм|сериал|смотреть|сери[яи]|сезон|актер|актёр|мульт|трейлер|кинопоиск"],
    ["музыка", r"песн|клип|аккорд|минусовк|караоке"],
    ["игры", r"(^|\s)игр[аыуе]?(\s|$)|играть|гта|gta|роблокс|roblox|майнкрафт|minecraft"],
    ["школа", r"краткое содержание|сочинени|гдз|\d+\s*класс|егэ|огэ|урок"],
    ["персона", r"биографи|жена|(^|\s)муж(\s|$)|возраст|национальност|умер|смерт|похорон"],
    ["соцсети", r"инстаграм|instagram|тикток|tiktok|телеграм|telegram|ютуб|youtube|рутуб"],
    ["18+", r"порно|секс|эротик"],
    ["работа", r"ваканси|зарплат"],
    ["новости", r"новост"],
    ["справка", r"википеди|что такое|простыми словами"],
    ["халява", r"скачать|торрент|бесплатно|читать онлайн"],
]

# Hints for the step 2.1 review of list rows only; the brief decides what is junk.
JUNK_HINTS = {stem(word): word for word in (
    "вакансия работа зарплата резюме гдз реферат егэ огэ скачать торрент смотреть "
    "фильм сериал песня википедия доллар евро юань валюта").split()}

_NON_WORD = re.compile(r"[^0-9a-zа-я]+")


class Fail(Exception):
    """A user-facing error: printed without a traceback, exit status 2."""


def now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def say(text=""):
    print(text)


def num(value):
    return f"{value:,}".replace(",", " ")


def ru_date(iso_date):
    year, month, day = iso_date.split("-")
    return f"{day}.{month}.{year}"


# ---------- files ----------

def write_json(path, value):
    """Atomic write: an interrupted run never leaves half a state file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def load(project):
    path = Path(project) / STATE
    if not path.is_file():
        raise Fail(f"проект не найден: {path}. Создай его: kw.py init {project} ...")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise Fail(f"{path} не читается: {error}") from None
    if state.get("version") != 1:
        raise Fail(f"{path}: неизвестная версия формата")
    migrate(state)
    return state


def migrate(state):
    """Older projects: per-list reviews -> step milestones, branches -> categories, no audit marks."""
    reviews = state.pop("reviews", None) or {}
    if "cleaned" not in state:
        state["cleaned"] = min(reviews.values(), key=lambda r: r["at"]) if reviews else None
    state.setdefault("expanded", None)
    branches = state.pop("branches", None) or {}
    categories = state.setdefault("categories", {})
    for name, branch in branches.items():
        categories.setdefault(name, {"definition": "", "status": branch.get("status", "open"),
                                     "note": branch.get("note", "")})
    state["project"].setdefault("mode", "city")
    for phrase in state["phrases"]:
        phrase.setdefault("stage", "1")
        branch = phrase.pop("branch", "")
        phrase.setdefault("category", branch or "без категории")
        phrase.setdefault("checked", None)
        categories.setdefault(phrase["category"], {"definition": "", "status": "open", "note": ""})


def save(project, state):
    write_json(Path(project) / STATE, state)


def read_text(args):
    if args.infile:
        try:
            return Path(args.infile).read_bytes().decode("utf-8-sig")
        except OSError as error:
            raise Fail(f"не читается {args.infile}: {error}") from None
    if sys.stdin is None or sys.stdin.isatty():
        raise Fail("нет входных данных: передай их через stdin или --in ФАЙЛ")
    return sys.stdin.buffer.read().decode("utf-8-sig")


def read_lines(args):
    lines = (line.strip() for line in read_text(args).splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def journal(state, text):
    state["journal"].append({"at": now(), "text": text})


def strip_comments(code):
    # Comments are for whoever edits a template; the agent pastes the code, so keep it short.
    return "\n".join(line for line in code.splitlines() if not line.strip().startswith("//"))


# ---------- phrases and categories ----------

def normalize(raw):
    """Lower-case plain phrase: Wordstat operators, hyphens and punctuation become spaces."""
    return " ".join(_NON_WORD.sub(" ", raw.lower().replace("ё", "е")).split())


def key_of(text):
    return " ".join(sorted(signature(text)))


def sig(entry_or_key):
    key = entry_or_key["key"] if isinstance(entry_or_key, dict) else entry_or_key
    return frozenset(key.split())


def active(state):
    return [p for p in state["phrases"] if p["status"] == "active"]


def find_active(state, raw):
    key = key_of(normalize(raw))
    return next((p for p in active(state) if p["key"] == key), None)


def covering(state, words, exclude=None):
    """Active phrase whose stems are a subset of `words`: it already matches every such query."""
    return next((p for p in active(state) if p is not exclude and sig(p) <= words), None)


def junk_hints(text):
    return sorted({JUNK_HINTS[s] for s in signature(text) if s in JUNK_HINTS})


def categories_in_use(state):
    order = []
    for phrase in active(state):
        if phrase["category"] not in order:
            order.append(phrase["category"])
    return order


def category_phrases(state, name):
    return [p for p in active(state) if p["category"] == name]


def _digest(phrases, state):
    payload = {
        "phrases": sorted(p["key"] for p in phrases),
        "minus": sorted(m["stem"] for m in state["minus"]),
        "region": state["project"]["region"]["id"],
        "devices": DEVICES,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:12]


def snapshot(state):
    """Identity of what a list measurement measured: phrases, minus words, region, devices."""
    return _digest(active(state), state)


def category_snapshot(state, name):
    return _digest(category_phrases(state, name), state)


def expression(texts, minus_words):
    return "(" + "|".join(texts) + ")" + "".join(f" -{word}" for word in minus_words)


def packets(texts, minus_words, limit):
    chunks, current = [], []
    for text in texts:
        if current and len(expression(current + [text], minus_words)) > limit:
            chunks.append(current)
            current = []
        current.append(text)
    if current:
        chunks.append(current)
    return chunks


def slug(name):
    return re.sub(r"[^\w\-]+", "-", name.strip().lower()).strip("-") or "category"


# ---------- regions ----------

REGION_ALIASES = {"спб": "санкт петербург", "питер": "санкт петербург", "мск": "москва",
                  "екб": "екатеринбург", "нск": "новосибирск", "рф": "россия", "вся россия": "россия"}


def load_regions():
    """[[id, name, parent_id], ...] from the bundled Wordstat region list (id|name|parent), or []."""
    try:
        lines = REGIONS.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    regions = []
    for line in lines:
        parts = line.split("|")
        if line.startswith("#") or len(parts) != 3 or not parts[0].isdigit():
            continue
        regions.append([int(parts[0]), parts[1], int(parts[2]) if parts[2].isdigit() else None])
    return regions


def region_path(region, by_id):
    names, parent = [], region[2]
    while parent in by_id and len(names) < 3:
        names.append(by_id[parent][1])
        parent = by_id[parent][2]
    return " / ".join(names)


def find_regions(query):
    regions = load_regions()
    by_id = {r[0]: r for r in regions}
    wanted = normalize(query)
    wanted = REGION_ALIASES.get(wanted, wanted)
    exact = [r for r in regions if normalize(r[1]) == wanted]
    partial = [r for r in regions if wanted and wanted in normalize(r[1])]
    return (exact or partial), by_id


def resolve_region(region_id, name):
    if region_id is not None:
        if region_id < 1:
            raise Fail("id региона — положительное число из адреса Вордстата (region=...)")
        if not name:
            by_id = {r[0]: r for r in load_regions()}
            if region_id not in by_id:
                raise Fail("укажи и --region: названия для этого id нет в справочнике")
            name = by_id[region_id][1]
        return region_id, name
    if not name:
        raise Fail("укажи регион: --region НАЗВАНИЕ или --region-id N")
    matches, by_id = find_regions(name)
    if len(matches) == 1:
        return matches[0][0], matches[0][1]
    if not matches:
        raise Fail(f"«{name}» нет в справочнике. Выбери регион в Вордстате и возьми число из адреса "
                   "(region=...), затем --region-id N --region НАЗВАНИЕ")
    listed = "; ".join(f"{r[0]} {r[1]} ({region_path(r, by_id)})" for r in matches[:12])
    raise Fail(f"несколько регионов «{name}»: {listed}. Укажи --region-id")


# ---------- pipeline ----------

def current_measurement(state):
    snap = snapshot(state)
    return next((m for m in reversed(state["measurements"])
                 if m["kind"] == "group" and m["snapshot"] == snap), None)


def current_category_measurements(state):
    """{category: measurement or None} for the current phrases of every category in use."""
    names = categories_in_use(state)
    found = {}
    for name in names:
        snap = category_snapshot(state, name)
        found[name] = next((m for m in reversed(state["measurements"])
                            if m["kind"] == "category" and m.get("category") == name
                            and m["snapshot"] == snap), None)
    if len(names) == 1 and found[names[0]] is None:
        found[names[0]] = current_measurement(state)  # one category is the whole list
    return found


def bounds(measurement):
    if measurement["exact"]:
        return measurement["total"], measurement["total"]
    return measurement["total_low"], measurement["total_high"]


def evaluate(state):
    """Next step of 1 -> 2 -> 2.1 -> 3 -> 4 -> result.

    Steps never repeat: once 2.1 is closed, a short list goes to step 4, and once step 4 has
    added phrases (or was closed with nothing to add) a measured list is the result. Every
    phrase must pass the double-meaning audit before 2.1 closes and before the result.
    """
    info = {"code": "", "measurement": None, "low": None, "high": None, "unchecked": [], "short": []}
    phrases = active(state)
    info["unchecked"] = [p for p in phrases if not p.get("checked")]
    if not phrases:
        info["code"] = "step1"
        return info
    if not state.get("cleaned"):
        measured = any(m["kind"] == "group" for m in state["measurements"])
        info["code"] = "step2.1" if measured else "step2"
        return info
    expanding = bool(state.get("expanded")) or any(p.get("stage") == "4" for p in phrases)
    if info["unchecked"]:
        info["code"] = "audit"
        return info
    measurement = current_measurement(state)
    categories = current_category_measurements(state)
    rf = state["project"].get("mode") == "rf"
    if measurement is None or (rf and any(m is None for m in categories.values())):
        info["code"] = "step4-measure" if expanding else "step3"
        return info
    target = state["project"]["target"]
    info["measurement"] = measurement
    info["low"], info["high"] = bounds(measurement)
    if rf:
        info["short"] = [(name, bounds(m)[0]) for name, m in categories.items() if bounds(m)[0] < target]
        reached = not info["short"]
    else:
        reached = info["low"] >= target
    info["code"] = "done" if reached else ("short" if expanding else "step4")
    return info


def status_line(state):
    info = evaluate(state)
    target = num(state["project"]["target"])
    measurement, low, high = info["measurement"], info["low"], info["high"]
    got = ""
    if measurement:
        got = num(low) if measurement["exact"] else f"{num(low)}–{num(high)} (замер пачками)"
    short = ", ".join(f"«{name}» {num(value)}" for name, value in info["short"])
    unchecked = len(info["unchecked"])
    rf = state["project"].get("mode") == "rf"
    audit_part = (f"проверить двойной смысл каждой фразы (kw.py audit, не проверено: {unchecked}) и "
                  if unchecked else "")
    return {
        "step1": "ШАГ 1 — предложить категории и подобрать фразы: kw.py add --category",
        "step2": "ШАГ 2 — замерить список в Вордстате: kw.py js -> javascript_tool -> get_page_text "
                 "-> kw.py record",
        "step2.1": f"ШАГ 2.1 — {audit_part}вычистить мусор по строкам замера (kw.py rm / minus), "
                   "затем kw.py cleaned --note",
        "audit": f"ШАГ 4 — проверить двойной смысл новых фраз: {unchecked} (kw.py audit -> kw.py checked / rm), "
                 "затем итоговый замер",
        "step3": "ШАГ 3 — замерить очищенный список и сравнить с целью: kw.py js -> ... -> kw.py record",
        "step4": (f"ШАГ 4 — ниже цели {target}: {short}. Расширить эти категории без потери качества"
                  if rf else
                  f"ШАГ 4 — запросов недостаточно: {got} из {target}. Расширить без потери качества: "
                  "kw.py candidates -> kw.py add -> kw.py audit -> итоговый замер"),
        "step4-measure": "ШАГ 4 — замерить итоговый список: kw.py js -> ... -> kw.py record",
        "done": (f"ИТОГ — все категории ≥ {target}. Выдача: kw.py export" if rf else
                 f"ИТОГ — цель достигнута: {got} ≥ {target}. Выдача: kw.py export"),
        "short": (f"ИТОГ — ниже цели {target} после расширения: {short}. Выдача: kw.py export, "
                  "варианты — пользователю" if rf else
                  f"ИТОГ — недобор после расширения: {got} из {target}. Выдача: kw.py export, "
                  "варианты — пользователю"),
    }[info["code"]]


def total_text(measurement):
    low, high = bounds(measurement)
    return num(low) if measurement["exact"] else f"{num(low)}–{num(high)} (пачками)"


def period_of(result):
    end_ms = (result.get("period_ms") or {}).get("endDate")
    if not isinstance(end_ms, (int, float)) or isinstance(end_ms, bool):
        return None
    end = dt.datetime.fromtimestamp(end_ms / 1000, MSK).date()
    return [(end - dt.timedelta(days=WINDOW_DAYS - 1)).isoformat(), end.isoformat()]


def period_text(measurement):
    period = measurement.get("period")
    return f"{ru_date(period[0])}–{ru_date(period[1])}" if period else "период не сообщён"


def clean_rows(rows):
    """Validate [[text, count], ...] from the page; drop malformed rows."""
    cleaned = []
    for row in rows or []:
        if (isinstance(row, list) and len(row) == 2 and isinstance(row[0], str)
                and type(row[1]) is int and row[1] >= 0):
            cleaned.append([row[0], row[1]])
    return cleaned


def merge_rows(row_lists):
    best = {}
    for rows in row_lists:
        for text, count in rows:
            best[text] = max(count, best.get(text, 0))
    return sorted(([t, c] for t, c in best.items()), key=lambda row: -row[1])


def fnv1a(text):
    """FNV-1a over UTF-8; identical to fnv() in wordstat_batch.js."""
    value = 0x811C9DC5
    for byte in text.encode("utf-8"):
        value = ((value ^ byte) * 0x01000193) & 0xFFFFFFFF
    return f"{value:08x}"


def canon(result):
    """The string wordstat_batch.js hashes for one result (field order and format must match)."""
    def rows(key):
        return ";".join(f"{text}:{count}" for text, count in result.get(key) or [])
    total = result.get("total")
    end = (result.get("period_ms") or {}).get("endDate") or ""
    return "|".join([str(result.get("id")), str(result.get("h")), result.get("error") or "",
                     "" if total is None else str(total), "1" if result.get("invalid") else "0",
                     str(end), rows("popular"), rows("similar")])


def checksum(data):
    return fnv1a(f"{data.get('request_id')}\n" + "\n".join(canon(r) for r in data.get("results") or []))


def extract_json(text):
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise Fail("во входе нет JSON — передай вывод get_page_text целиком")
    try:
        return json.loads(text[start:end + 1])
    except ValueError as error:
        raise Fail(f"JSON повреждён ({error}) — передай вывод get_page_text без правок") from None


def check_region(state, page):
    region = state["project"]["region"]
    label = page.get("region_label")
    if str(page.get("region_param")) == str(region["id"]) and label:
        if normalize(label) != normalize(region["name"]):
            raise Fail(f"в Вордстате регион {region['id']} называется «{label}», а в карточке — "
                       f"«{region['name']}». Проверь id (kw.py region «{region['name']}») и исправь: "
                       "kw.py card ПАПКА --region-id N --region НАЗВАНИЕ")
        region["verified"] = label
    elif not region.get("verified"):
        raise Fail("регион ещё не сверен: открой вкладку по ссылке из kw.py js (в ней region=...), "
                   "выполни тот же JS ещё раз и снова get_page_text")


def row_marks(state, text):
    marks = []
    key = key_of(text)
    known = next((p for p in state["phrases"] if p["key"] == key), None)
    if known and known["status"] == "removed":
        marks.append("удалена ранее")
    elif covering(state, sig(key)):
        marks.append("в списке")
    elif len(significant_words(text)) >= 2:
        marks.append("НОВАЯ ОСНОВА")
    hints = junk_hints(text)
    if hints:
        marks.append("мусор? " + ", ".join(hints))
    return marks


def show_rows(state, rows, limit, marks=True):
    """Print rows; `marks` adds list coverage (for probes), junk hints are always shown."""
    for text, count in rows[:limit]:
        if marks:
            flags = row_marks(state, text)
        else:
            hints = junk_hints(text)
            flags = ["мусор? " + ", ".join(hints)] if hints else []
        say(f"    {num(count):>9}  {text}" + (f"   [{'; '.join(flags)}]" if flags else ""))


def category_table(state):
    """Lines: category, phrases, unchecked, current total."""
    lines = []
    measured = current_category_measurements(state)
    for name in categories_in_use(state):
        phrases = category_phrases(state, name)
        unchecked = sum(not p.get("checked") for p in phrases)
        measurement = measured.get(name)
        total = total_text(measurement) if measurement else "не измерена"
        note = f" · без проверки двойного смысла: {unchecked}" if unchecked else ""
        lines.append(f"  {name}: {len(phrases)} фраз · {total}{note}")
    return lines


# ---------- commands ----------

def empty_state(card):
    return {"version": 1, "project": card, "categories": {}, "phrases": [], "minus": [],
            "measurements": [], "cleaned": None, "expanded": None, "journal": [], "seq": 0}


def cmd_init(args):
    root = Path(args.project)
    if (root / STATE).exists():
        raise Fail(f"{root / STATE} уже есть — продолжай этот проект или выбери другую папку")
    if args.target < 1:
        raise Fail("цель — положительное число запросов")
    region_id, region_name = resolve_region(args.region_id, args.region)
    state = empty_state({
        "name": args.name, "offer": args.offer or "", "anchor": args.anchor or "",
        "region": {"id": region_id, "name": region_name, "verified": None},
        "target": args.target, "mode": args.mode,
        "forbidden": sorted({normalize(w) for w in args.forbid or [] if normalize(w)}),
        "created_at": now(),
    })
    journal(state, "проект создан")
    save(root, state)
    say(f"Проект «{args.name}»: регион {region_name} ({region_id}), цель {num(args.target)}, "
        f"режим — {MODES[args.mode]}. Папка: {root}")
    say("Дальше шаг 1: kw.py add " + str(root) + " --category КАТЕГОРИЯ  (фразы в stdin, по одной на строку)")


def cmd_card(args):
    state = load(args.project)
    card = state["project"]
    for field in ("name", "offer", "anchor", "mode"):
        if getattr(args, field) is not None:
            card[field] = getattr(args, field)
    if args.target is not None:
        if args.target < 1:
            raise Fail("цель — положительное число запросов")
        card["target"] = args.target
    if args.region_id is not None or args.region is not None:
        region_id, name = resolve_region(args.region_id, args.region)
        card["region"] = {"id": region_id, "name": name, "verified": None}
    forbidden = set(card.get("forbidden", []))
    forbidden |= {normalize(w) for w in args.forbid or [] if normalize(w)}
    forbidden -= {normalize(w) for w in args.unforbid or []}
    card["forbidden"] = sorted(forbidden)
    journal(state, "карточка изменена")
    save(args.project, state)
    say(json.dumps(card, ensure_ascii=False, indent=1))


def cmd_fork(args):
    """Copy the list into a sibling project with another region: numbers for the options question."""
    source = load(args.project)
    target = Path(args.dest)
    if (target / STATE).exists():
        raise Fail(f"{target / STATE} уже есть — выбери другую папку")
    region_id, region_name = resolve_region(args.region_id, args.region)
    card = dict(source["project"], name=f"{source['project']['name']} — {region_name}", created_at=now(),
                region={"id": region_id, "name": region_name, "verified": None})
    state = empty_state(card)
    # The copy starts its own pipeline: the list is its step 1, junk is re-checked for the region.
    state.update(phrases=[dict(p, stage="1") for p in source["phrases"]], minus=source["minus"],
                 categories=source["categories"])
    journal(state, f"копия {args.project} для региона {region_name}")
    save(target, state)
    say(f"Копия для региона {region_name} ({region_id}): {target}. Дальше: kw.py js {target}")


def cmd_add(args):
    state = load(args.project)
    if evaluate(state)["code"] == "step2.1":
        raise Fail("идёт шаг 2.1: сейчас только чистка (kw.py audit / rm / minus). "
                   "Новые фразы добавляются на шаге 4 — после kw.py cleaned")
    category = args.category.strip()
    if not category:
        raise Fail("укажи категорию: --category НАЗВАНИЕ (одна категория — один вид фраз)")
    stage = "4" if state.get("cleaned") else "1"
    minus = {m["stem"]: m["word"] for m in state["minus"]}
    forbidden = {stem(w): w for w in state["project"].get("forbidden", [])}
    added, skipped, absorbed = [], [], []
    for raw in read_lines(args):
        text = normalize(raw)
        if len(significant_words(text)) < 2:
            skipped.append((raw, "меньше двух значимых слов"))
            continue
        if len(text.split()) > MAX_WORDS:
            skipped.append((raw, f"больше {MAX_WORDS} слов — Вордстат такие не принимает"))
            continue
        key = key_of(text)
        words = sig(key)
        known = next((p for p in state["phrases"] if p["key"] == key), None)
        if known and known["status"] == "active":
            skipped.append((raw, f"дубль «{known['text']}» (категория «{known['category']}»)"))
            continue
        if known and known["status"] == "removed" and not args.force:
            skipped.append((raw, f"ранее удалена: {known.get('reason')} (вернуть: --force)"))
            continue
        cut = sorted(minus[s] for s in words if s in minus)
        if cut:
            skipped.append((raw, "содержит минус-слово: " + ", ".join(cut)))
            continue
        banned = sorted(forbidden[s] for s in words if s in forbidden)
        if banned:
            skipped.append((raw, "запретная тема: " + ", ".join(banned)))
            continue
        cover = covering(state, words)
        if cover:
            skipped.append((raw, f"поглощена «{cover['text']}» — прироста не даст"))
            continue
        for phrase in active(state):
            if words < sig(phrase):
                phrase.update(status="absorbed", by=text, reason="поглощена более широкой фразой")
                absorbed.append((phrase["text"], text))
        entry = {"text": text, "key": key, "status": "active", "stage": stage, "category": category,
                 "checked": None, "source": args.source or "", "added_at": now(), "reason": None, "by": None}
        if known:  # --force on a removed phrase: reuse its history slot
            known.update(entry)
        else:
            state["phrases"].append(entry)
        added.append(text)
    registry = state["categories"].setdefault(category, {"definition": "", "status": "open", "note": ""})
    if args.define:
        registry["definition"] = args.define
    if added:
        journal(state, f"шаг {stage}: +{len(added)} фраз в «{category}»")
    save(args.project, state)
    say(f"Шаг {stage}, «{category}». Добавлено: {len(added)} · отклонено: {len(skipped)} · "
        f"в работе всего: {len(active(state))}")
    for raw, reason in skipped:
        say(f"  - {raw} — {reason}")
    for narrow, broad in absorbed:
        say(f"  ~ «{narrow}» снята: её покрывает новая «{broad}»")
    if added:
        say("  Новые фразы не проверены на двойной смысл — kw.py audit.")


def restore_absorbed(state, removed_text):
    restored = []
    for phrase in state["phrases"]:
        if phrase["status"] == "absorbed" and phrase.get("by") == removed_text:
            cover = covering(state, sig(phrase))
            if cover:
                phrase["by"] = cover["text"]
            else:
                phrase.update(status="active", by=None, reason=None)
                restored.append(phrase["text"])
    return restored


def cmd_rm(args):
    state = load(args.project)
    removed, remembered, restored = [], [], []
    for raw in read_lines(args):
        text = normalize(raw)
        if not text:
            continue
        key = key_of(text)
        known = next((p for p in state["phrases"] if p["key"] == key), None)
        if known is None:
            state["phrases"].append({"text": text, "key": key, "status": "removed", "stage": "1",
                                     "category": "без категории", "checked": None, "source": "rm",
                                     "added_at": now(), "reason": args.reason, "by": None})
            remembered.append(text)
            continue
        if known["status"] == "removed":
            continue
        was_active = known["status"] == "active"
        known.update(status="removed", reason=args.reason, by=None)
        removed.append(known["text"])
        if was_active:
            restored += restore_absorbed(state, known["text"])
    journal(state, f"−{len(removed)} фраз: {args.reason}")
    save(args.project, state)
    say(f"Удалено: {len(removed)} · запомнено как мусор (в списке не было): {len(remembered)} · "
        f"в работе: {len(active(state))}")
    for text in restored:
        say(f"  + вернулась уточнённая «{text}» (её перекрывала удалённая фраза) — проверь её двойной смысл")


def cmd_move(args):
    state = load(args.project)
    category = args.category.strip()
    moved, unknown = [], []
    for raw in read_lines(args):
        phrase = find_active(state, raw)
        if phrase is None:
            unknown.append(raw)
            continue
        phrase["category"] = category
        moved.append(phrase["text"])
    state["categories"].setdefault(category, {"definition": "", "status": "open", "note": ""})
    journal(state, f"{len(moved)} фраз перенесено в «{category}»")
    save(args.project, state)
    say(f"Перенесено в «{category}»: {len(moved)}")
    for raw in unknown:
        say(f"  - {raw} — нет среди фраз в работе")


def cmd_category(args):
    state = load(args.project)
    if args.rename:
        if args.rename in state["categories"]:
            raise Fail(f"категория «{args.rename}» уже есть")
        state["categories"][args.rename] = state["categories"].pop(
            args.name, {"definition": "", "status": "open", "note": ""})
        for phrase in state["phrases"]:
            if phrase["category"] == args.name:
                phrase["category"] = args.rename
        args.name = args.rename
    category = state["categories"].setdefault(args.name, {"definition": "", "status": "open", "note": ""})
    if args.define is not None:
        category["definition"] = args.define
    if args.status:
        category["status"] = args.status
    if args.note is not None:
        category["note"] = args.note
    journal(state, f"категория «{args.name}»: {category['status']}"
                   + (f" — {category['definition']}" if category["definition"] else ""))
    save(args.project, state)
    say(f"«{args.name}» · {category['status']} · {category['definition'] or 'без определения'}"
        + (f" · {category['note']}" if category["note"] else ""))


def cmd_minus(args):
    state = load(args.project)
    done, skipped = [], []
    for raw in read_lines(args):
        word = normalize(raw.lstrip("-!+ "))
        if len(word.split()) != 1:
            skipped.append((raw, "одно слово на строку — Вордстат минусует слова, а не фразы"))
            continue
        word_stem = stem(word)
        exists = next((m for m in state["minus"] if m["stem"] == word_stem), None)
        if args.action == "rm":
            if exists:
                state["minus"].remove(exists)
                done.append(word)
            continue
        if exists:
            skipped.append((raw, f"уже есть «{exists['word']}»"))
            continue
        if word in STOP_WORDS:
            skipped.append((raw, "стоп-слово: Вордстат его не учитывает"))
            continue
        conflicts = [p["text"] for p in active(state) if word_stem in sig(p)]
        if conflicts:
            skipped.append((raw, "отрежет фразы из списка: " + "; ".join(conflicts[:5])))
            continue
        state["minus"].append({"word": word, "stem": word_stem, "reason": args.reason or "",
                               "added_at": now()})
        done.append(word)
    if done:
        journal(state, ("+" if args.action == "add" else "−") + "минус: " + ", ".join(done))
    save(args.project, state)
    verb = "Добавлено" if args.action == "add" else "Убрано"
    say(f"{verb} минус-слов: {len(done)} · всего: {len(state['minus'])}")
    for raw, reason in skipped:
        say(f"  - {raw} — {reason}")


def cmd_audit(args):
    state = load(args.project)
    if args.list:
        pool = [normalize(line) for line in read_lines(args)]
        pool = [text for text in pool if text]
        waiting = len(pool)
    else:
        phrases = [p for p in active(state) if args.all or not p.get("checked")]
        pool = [p["text"] for p in phrases]
        waiting = len(pool)
    if not pool:
        say("Проверять нечего: все фразы в работе прошли проверку двойного смысла.")
        return 0
    batch = pool[:args.limit]
    region = state["project"]["region"]["id"]
    payload = {"region": region, "devices": DEVICES, "rows": AUDIT_ROWS, "similar": AUDIT_SIMILAR,
               "markers": MARKERS, "items": [{"id": f"a{i}", "expr": text} for i, text in enumerate(batch, 1)]}
    code = AUDIT_TEMPLATE.read_text(encoding="utf-8").replace("__REQ__", json.dumps(payload, ensure_ascii=False))
    say(f"# аудит двойного смысла: {len(batch)} из {waiting} фраз; регион {region}")
    say(f"# 1) вкладка Вордстата: https://wordstat.yandex.ru/?region={region}&view=table&words={quote(batch[0])}")
    say("# 2) javascript_tool в этой вкладке: весь код между строками ---8<---")
    say("# 3) get_page_text той же вкладки — отчёт по каждой фразе; ничего записывать не нужно.")
    say(f"# 4) решения: чистые — kw.py checked {args.project}; с двойным смыслом — kw.py rm ... --reason")
    say("---8<---")
    say(strip_comments(code).rstrip())
    say("---8<---")
    return 0


def cmd_checked(args):
    state = load(args.project)
    marked, unknown = [], []
    for raw in read_lines(args):
        phrase = find_active(state, raw)
        if phrase is None:
            unknown.append(raw)
            continue
        phrase["checked"] = {"at": now(), "note": args.note or ""}
        marked.append(phrase["text"])
    journal(state, f"двойной смысл проверен: {len(marked)} фраз" + (f" — {args.note}" if args.note else ""))
    save(args.project, state)
    left = sum(not p.get("checked") for p in active(state))
    say(f"Проверено: {len(marked)} · осталось без проверки: {left}")
    for raw in unknown:
        say(f"  - {raw} — нет среди фраз в работе")
    say("Дальше: " + status_line(state))


def cmd_js(args):
    root = Path(args.project)
    state = load(root)
    region = state["project"]["region"]["id"]
    texts = [p["text"] for p in active(state)]
    minus_words = [m["word"] for m in state["minus"]]
    items = []
    names = categories_in_use(state)
    if not args.no_group:
        if not texts:
            raise Fail("в списке нет фраз — шаг 1 (kw.py add)")
        chunks = packets(texts, minus_words, MAX_EXPR_CHARS)
        for index, chunk in enumerate(chunks, 1):
            items.append({"id": "g" if len(chunks) == 1 else f"g{index}", "kind": "group",
                          "expr": expression(chunk, minus_words), "rows": GROUP_ROWS,
                          "similar": SIMILAR_ROWS})
        if len(names) > 1 or state["project"].get("mode") == "rf":
            for index, name in enumerate(names, 1):
                expr = expression([p["text"] for p in category_phrases(state, name)], minus_words)
                if len(expr) > MAX_EXPR_CHARS:
                    say(f"# категория «{name}» длиннее {MAX_EXPR_CHARS} символов — её отдельный замер пропущен")
                    continue
                items.append({"id": f"c{index}", "kind": "category", "category": name, "expr": expr,
                              "snapshot": category_snapshot(state, name), "rows": CATEGORY_ROWS,
                              "similar": CATEGORY_SIMILAR})
    probes = [" ".join(p.split()) for p in args.probe or []]
    if args.probes:
        probes += [" ".join(p.split()) for p in read_lines(args)]
    for index, expr in enumerate((p for p in probes if p), 1):
        if len(expr) > MAX_EXPR_CHARS:
            raise Fail(f"проба длиннее {MAX_EXPR_CHARS} символов")
        items.append({"id": f"p{index}", "kind": "probe", "expr": expr, "rows": PROBE_ROWS,
                      "similar": SIMILAR_ROWS})
    if not items:
        raise Fail("нечего измерять: нет ни фраз, ни проб")
    state["seq"] += 1
    request_id = f"r{state['seq']}"
    write_json(root / PENDING, {
        "request_id": request_id, "created_at": now(), "region": region, "devices": DEVICES,
        "snapshot": snapshot(state), "keys": sorted(p["key"] for p in active(state)),
        "minus": minus_words, "items": items, "consumed": False,
    })
    save(root, state)
    payload = {"request_id": request_id, "region": region, "devices": DEVICES,
               "items": [{k: item[k] for k in ("id", "expr", "rows", "similar")} for item in items]}
    code = TEMPLATE.read_text(encoding="utf-8").replace("__REQ__", json.dumps(payload, ensure_ascii=False))
    groups = [item for item in items if item["kind"] == "group"]
    category_items = [item for item in items if item["kind"] == "category"]
    parts = f"список {len(texts)} фраз" + (f" в {len(groups)} пачках" if len(groups) > 1 else "") \
        if groups else "без списка"
    seed = (texts or probes)[0]
    say(f"# {request_id}: {parts}; категорий отдельно: {len(category_items)}; "
        f"проб: {len(items) - len(groups) - len(category_items)}; регион {region}")
    say(f"# 1) вкладка Вордстата (по ней сверяется регион): "
        f"https://wordstat.yandex.ru/?region={region}&view=table&words={quote(seed)}")
    say("# 2) javascript_tool в этой вкладке: весь код между строками ---8<---")
    say(f"# 3) get_page_text той же вкладки -> Write в <проект>/inbox.json -> kw.py record {args.project} --in ...")
    say("---8<---")
    say(strip_comments(code).rstrip())
    say("---8<---")


def new_measurement(state, pending, data, kind, results):
    return {
        "id": f"m{len(state['measurements']) + 1}", "request_id": pending["request_id"], "kind": kind,
        "region_id": pending["region"], "devices": pending["devices"],
        "period": period_of(results[0]), "retrieved_at": data.get("retrieved_at") or now(),
        "popular": merge_rows(clean_rows(r.get("popular")) for r in results),
        "similar": merge_rows(clean_rows(r.get("similar")) for r in results),
        "evidence": f"{EVIDENCE}/{pending['request_id']}.json",
    }


def describe_delta(state, measurement, previous):
    if previous is None:
        return "  первый замер списка"
    before, after = set(previous["keys"]), set(measurement["keys"])
    old_minus, new_minus = set(previous["minus"]), set(measurement["minus"])
    parts = [f"+{len(after - before)} фраз", f"−{len(before - after)} фраз"]
    if old_minus != new_minus:
        parts.append(f"минус-слова +{len(new_minus - old_minus)}/−{len(old_minus - new_minus)}")
    line = f"  было {total_text(previous)} ({previous['id']})"
    if measurement["exact"] and previous["exact"]:
        delta = measurement["total"] - previous["total"]
        line += f" → Δ {'+' if delta >= 0 else '−'}{num(abs(delta))}"
    line += " · состав: " + ", ".join(parts)
    if previous.get("period") != measurement.get("period"):
        line += " · данные Вордстата обновились между замерами, прирост приблизительный"
    return line


def cmd_record(args):
    root = Path(args.project)
    state = load(root)
    data = extract_json(read_text(args))
    pending_path = root / PENDING
    if not pending_path.is_file():
        raise Fail("нет ожидающего замера — сначала kw.py js")
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    if pending.get("consumed"):
        raise Fail(f"замер {pending['request_id']} уже записан; для нового — снова kw.py js")
    if data.get("request_id") != pending["request_id"]:
        raise Fail(f"это результат {data.get('request_id')!r}, а ждём {pending['request_id']!r}: "
                   "выполни свежий JS из kw.py js и снова get_page_text")
    results = data.get("results")
    if not isinstance(results, list):
        raise Fail("в результате нет списка results")
    if data.get("check") != checksum(data):
        raise Fail("контрольная сумма не сходится: JSON изменился при копировании. "
                   "Передай вывод get_page_text целиком, без правок")
    check_region(state, data.get("page") or {})
    items = {item["id"]: item for item in pending["items"]}
    good, problems = {}, []
    for result in results:
        item = items.get(result.get("id"))
        if item is None or result.get("h") != fnv1a(item["expr"]):
            raise Fail(f"результат {result.get('id')!r} не совпадает с запросом — используй JS из последнего kw.py js")
        total = result.get("total")
        if result.get("error"):
            problems.append(f"{item['id']}: {result['error']}")
        elif result.get("invalid"):
            problems.append(f"{item['id']}: Вордстат счёл выражение некорректным — {item['expr'][:80]}")
        elif type(total) is not int or total < 0:
            problems.append(f"{item['id']}: нет числа запросов")
        else:
            good[item["id"]] = result
    for item_id in items:
        if item_id not in good and not any(p.startswith(item_id + ":") for p in problems):
            problems.append(f"{item_id}: нет результата")

    write_json(root / EVIDENCE / f"{pending['request_id']}.json", data)
    previous = next((m for m in reversed(state["measurements"]) if m["kind"] == "group"), None)
    group_items = [item for item in pending["items"] if item["kind"] == "group"]
    group = None
    if group_items and all(item["id"] in good for item in group_items):
        parts = [good[item["id"]] for item in group_items]
        group = new_measurement(state, pending, data, "group", parts)
        totals = [part["total"] for part in parts]
        if len(parts) == 1:
            group.update(exact=True, total=totals[0], expr=group_items[0]["expr"])
        else:
            # Packets overlap: the union lies between the largest packet and their sum.
            group.update(exact=False, total_low=max(totals), total_high=sum(totals),
                         exprs=[item["expr"] for item in group_items])
        group.update(snapshot=pending["snapshot"], keys=pending["keys"], minus=pending["minus"],
                     phrase_count=len(pending["keys"]))
        state["measurements"].append(group)
        journal(state, f"замер {group['id']}: {total_text(group)} ({group['phrase_count']} фраз, "
                       f"{len(group['minus'])} минус)")
    elif group_items:
        problems.append("замер списка неполный — число списка не записано")
    recorded_categories, probes = [], []
    for item in pending["items"]:
        if item["id"] not in good or item["kind"] == "group":
            continue
        measurement = new_measurement(state, pending, data, item["kind"], [good[item["id"]]])
        measurement.update(exact=True, total=good[item["id"]]["total"], expr=item["expr"], item=item["id"])
        if item["kind"] == "category":
            measurement.update(category=item["category"], snapshot=item["snapshot"])
            recorded_categories.append(measurement)
        else:
            probes.append(measurement)
        state["measurements"].append(measurement)
    pending["consumed"] = True
    write_json(pending_path, pending)
    save(root, state)

    region = state["project"]["region"]
    say(f"Записан {pending['request_id']} · {region['name']} ({region['id']}) · доказательство: "
        f"{root / EVIDENCE / (pending['request_id'] + '.json')}")
    for problem in problems:
        say(f"  ОШИБКА {problem}")
    if group:
        say(f"\nСПИСОК: {group['phrase_count']} фраз, минус-слов {len(group['minus'])} → "
            f"{total_text(group)} запросов · данные {period_text(group)}")
        say(describe_delta(state, group, previous))
        purpose = ("материал для шага 2.1" if not state.get("cleaned")
                   else "проверь, что новые основы не принесли мусор")
        say(f"  Крупнейшие запросы внутри списка — {purpose}:")
        show_rows(state, group["popular"], SHOW_GROUP_ROWS, marks=False)
        if group["similar"]:
            say("  Похожие запросы (сырьё для шага 4):")
            show_rows(state, group["similar"], SHOW_SIMILAR_ROWS)
    if recorded_categories:
        say("\nКАТЕГОРИИ (каждая отдельно; вместе они пересекаются):")
        for measurement in sorted(recorded_categories, key=lambda m: -m["total"]):
            say(f"  {num(measurement['total']):>9}  {measurement['category']}")
    for probe in probes:
        say(f"\nПРОБА {probe['item']} «{probe['expr'][:90]}»: {num(probe['total'])}")
        show_rows(state, probe["popular"], SHOW_PROBE_ROWS)
        if probe["similar"]:
            say("  похожие:")
            show_rows(state, probe["similar"], SHOW_SIMILAR_ROWS)
    say("\nДальше: " + status_line(state))
    if problems:
        return 2
    return 0


def cmd_cleaned(args):
    state = load(args.project)
    groups = [m for m in state["measurements"] if m["kind"] == "group"]
    if not groups:
        raise Fail("шаг 2.1 идёт после замера — сначала шаг 2")
    unchecked = [p["text"] for p in active(state) if not p.get("checked")]
    if unchecked:
        raise Fail(f"двойной смысл не проверен у {len(unchecked)} фраз (например, «{unchecked[0]}»): "
                   "kw.py audit, затем kw.py checked / rm")
    state["cleaned"] = {"note": args.note, "at": now(), "measurement": groups[-1]["id"]}
    journal(state, f"шаг 2.1 выполнен: {args.note}")
    save(args.project, state)
    say("Дальше: " + status_line(state))


def cmd_expanded(args):
    state = load(args.project)
    if not state.get("cleaned"):
        raise Fail("шаг 4 идёт после шагов 2.1 и 3")
    state["expanded"] = {"note": args.note, "at": now()}
    journal(state, f"шаг 4 завершён: {args.note}")
    save(args.project, state)
    say("Дальше: " + status_line(state))


def cmd_status(args):
    state = load(args.project)
    card = state["project"]
    counts = {status: sum(p["status"] == status for p in state["phrases"])
              for status in ("active", "removed", "absorbed")}
    added_in_step4 = sum(p.get("stage") == "4" for p in active(state))
    unchecked = sum(not p.get("checked") for p in active(state))
    measurement = current_measurement(state)
    say(f"Проект: {card['name']} · регион: {card['region']['name']} ({card['region']['id']}) · "
        f"цель: {num(card['target'])} · режим: {MODES.get(card.get('mode', 'city'))}")
    if card.get("anchor"):
        say(f"Намерение: {card['anchor']}")
    say(f"Фразы: в работе {counts['active']} (из них с шага 4: {added_in_step4}; без проверки двойного "
        f"смысла: {unchecked}) · удалено {counts['removed']} · поглощено {counts['absorbed']} · "
        f"минус-слов {len(state['minus'])}")
    last = next((m for m in reversed(state["measurements"]) if m["kind"] == "group"), None)
    if measurement:
        say(f"Замер всего списка: {total_text(measurement)} · данные {period_text(measurement)} · "
            f"получен {measurement['retrieved_at'][:16].replace('T', ' ')} ({measurement['id']})")
    elif last:
        say(f"Список изменился после замера {last['id']} ({total_text(last)}) — нужен новый замер")
    if categories_in_use(state):
        say("Категории:")
        for line in category_table(state):
            say(line)
    cleaned = state.get("cleaned")
    say("Шаг 2.1: " + (f"выполнен — {cleaned['note']}" if cleaned else "не выполнен"))
    if state.get("expanded"):
        say(f"Шаг 4: закрыт — {state['expanded']['note']}")
    say("Дальше: " + status_line(state))
    history = [total_text(m) for m in state["measurements"] if m["kind"] == "group"]
    if history:
        say("Замеры списка: " + " → ".join(history[-12:]))
    for entry in state["journal"][-args.journal:]:
        say(f"  {entry['at'][:16].replace('T', ' ')}  {entry['text']}")
    rows_from = measurement or last
    if args.rows and rows_from:
        say(f"Крупнейшие запросы списка ({rows_from['id']}):")
        show_rows(state, rows_from["popular"], args.rows, marks=False)
    if args.phrases:
        for name in categories_in_use(state):
            say(f"  [{name}]")
            for phrase in category_phrases(state, name):
                say(f"    {phrase['text']}" + ("" if phrase.get("checked") else "   (не проверена)"))
    return 0


def cmd_list(args):
    state = load(args.project)
    for phrase in active(state):
        if args.category and phrase["category"] != args.category:
            continue
        say(phrase["text"] + (f"\t[{phrase['category']}]" if args.categories else ""))
    if args.all:
        for phrase in state["phrases"]:
            if phrase["status"] == "removed":
                say(f"# удалена: {phrase['text']} — {phrase.get('reason')}")
            elif phrase["status"] == "absorbed":
                say(f"# поглощена: {phrase['text']} — фразой «{phrase.get('by')}»")
        for minus in state["minus"]:
            say(f"# минус: {minus['word']}" + (f" — {minus['reason']}" if minus["reason"] else ""))


def cmd_candidates(args):
    state = load(args.project)
    if args.request:
        pool = [m for m in state["measurements"] if m["request_id"] == args.request]
    elif args.all:
        pool = list(state["measurements"])
    else:
        last = state["measurements"][-1]["request_id"] if state["measurements"] else None
        pool = [m for m in state["measurements"] if m["request_id"] == last]
    if not pool:
        raise Fail("нет замеров со строками — сначала kw.py js с --probe и kw.py record")
    known = {p["key"] for p in state["phrases"]}
    blocked = {m["stem"] for m in state["minus"]} | {stem(w) for w in state["project"].get("forbidden", [])}
    found = {}
    for measurement in pool:
        source = {"group": "список", "category": f"категория «{measurement.get('category')}»"}.get(
            measurement["kind"], f"«{measurement['expr'][:40]}»")
        for kind, rows in (("популярные", measurement["popular"]), ("похожие", measurement["similar"])):
            for text, count in rows:
                phrase = normalize(text)
                if len(significant_words(phrase)) < 2 or len(phrase.split()) > MAX_WORDS:
                    continue
                key = key_of(phrase)
                words = sig(key)
                if key in known or words & blocked or covering(state, words):
                    continue
                entry = found.setdefault(key, {"text": phrase, "count": 0, "sources": set()})
                entry["count"] = max(entry["count"], count)
                entry["sources"].add(f"{source} {kind}")
    rows = sorted(found.values(), key=lambda e: -e["count"])
    rows = [e for e in rows if e["count"] >= args.min][:args.limit]
    say(f"Новые основы (не поглощены списком, без минус/запретных слов): {len(rows)}")
    for entry in rows:
        broader = [p["text"] for p in active(state) if sig(key_of(entry["text"])) < sig(p)]
        notes = [", ".join(sorted(entry["sources"]))]
        if broader:
            notes.append("шире: " + "; ".join(broader[:3]))
        hints = junk_hints(entry["text"])
        if hints:
            notes.append("мусор? " + ", ".join(hints))
        say(f"  {num(entry['count']):>9}  {entry['text']}   [{' · '.join(notes)}]")
    say("Число у строки — её собственная частотность: верхняя граница прироста, не сам прирост.")


def cmd_log(args):
    state = load(args.project)
    journal(state, args.text)
    save(args.project, state)
    say("записано")


def build_report(state, final):
    card = state["project"]
    info = evaluate(state)
    measurement, low, high = info["measurement"], info["low"], info["high"]
    measured = current_category_measurements(state)
    minus = [m["word"] for m in state["minus"]]
    target = num(card["target"])
    rf = card.get("mode") == "rf"
    lines = []
    if not final:
        lines += [f"ЧЕРНОВИК — не для запуска. {status_line(state)}", ""]
    lines += [f"Проект: {card['name']}"]
    if card.get("offer"):
        lines.append(f"Что рекламируем: {card['offer']}")
    if card.get("anchor"):
        lines.append(f"Намерение: {card['anchor']}")
    lines += [f"Режим: {MODES.get(card.get('mode', 'city'))}",
              f"География: {card['region']['name']} (регион Вордстата {card['region']['id']})",
              f"Цель: {target} запросов в Вордстате", ""]
    if measurement:
        total = num(low) if measurement["exact"] else f"от {num(low)} до {num(high)} (мерилось пачками)"
        lines.append(f"Все категории вместе: {total} запросов")
    lines.append("По категориям (каждая отдельно; вместе они пересекаются, поэтому сумма больше общего числа):")
    for name in categories_in_use(state):
        m = measured.get(name)
        count = len(category_phrases(state, name))
        lines.append(f"- {name}: {count} фраз, {total_text(m) if m else 'не измерена'} запросов")
    for name in categories_in_use(state):
        definition = state["categories"].get(name, {}).get("definition")
        phrases = [p["text"] for p in category_phrases(state, name)]
        lines += ["", f"Категория «{name}»" + (f" — {definition}" for _ in [0]).__next__() if definition
                  else f"Категория «{name}»"]
        lines += ["```text", *phrases, "```"]
    lines += ["", f"Минус-слова ({len(minus)}), общие для всех категорий:", "```text",
              *(minus or ["— нет —"]), "```", ""]
    if measurement:
        lines += [
            "Источник: Яндекс Вордстат — «Общее число запросов» по полному OR-выражению с минус-словами, "
            "все устройства",
            f"Период: {period_text(measurement)} (последние {WINDOW_DAYS} дней)",
            f"Проверено: {measurement['retrieved_at'][:16].replace('T', ' ')}",
        ]
    if info["code"] == "done":
        lines.append(f"Статус: цель достигнута ({'каждая категория ≥ ' if rf else ''}{target})")
    elif info["code"] == "short":
        if rf:
            names = ", ".join(f"{n} ({num(v)})" for n, v in info["short"])
            lines.append(f"Статус: ниже цели после расширения: {names}")
        else:
            lines.append(f"Статус: недобор после расширения — не хватает {num(card['target'] - low)} до цели {target}")
    else:
        lines.append("Статус: черновик")
    checked = sum(bool(p.get("checked")) for p in active(state))
    lines.append(f"Двойной смысл: проверено {checked} из {len(active(state))} фраз")
    if state.get("cleaned"):
        lines.append(f"Чистка (шаг 2.1): {state['cleaned']['note']}")
    step4 = sum(p.get("stage") == "4" for p in active(state))
    if step4:
        lines.append(f"Добавлено на шаге 4: {step4} фраз")
    if state.get("expanded"):
        lines.append(f"Шаг 4: {state['expanded']['note']}")
    history = [total_text(m) for m in state["measurements"] if m["kind"] == "group"]
    if history:
        lines.append("Замеры списка: " + " → ".join(history[-12:]))
    return "\n".join(lines) + "\n"


def cmd_export(args):
    root = Path(args.project)
    state = load(root)
    final = evaluate(state)["code"] in ("done", "short")
    if not final and not args.draft:
        raise Fail("выгрузка открывается на итоге: " + status_line(state) + ". Черновик без гарантий: --draft")
    folder = root / EXPORT
    folder.mkdir(parents=True, exist_ok=True)
    phrases = [p["text"] for p in active(state)]
    minus = [m["word"] for m in state["minus"]]
    (folder / "keywords.txt").write_text("\n".join(phrases) + "\n", encoding="utf-8")
    (folder / "minus.txt").write_text("".join(word + "\n" for word in minus), encoding="utf-8")
    written = []
    for name in categories_in_use(state):
        path = folder / f"категория-{slug(name)}.txt"
        path.write_text("\n".join(p["text"] for p in category_phrases(state, name)) + "\n", encoding="utf-8")
        written.append(path.name)
    report = build_report(state, final)
    (folder / "report.md").write_text(report, encoding="utf-8")
    journal(state, "выгрузка" + ("" if final else " (черновик)"))
    save(root, state)
    say(report)
    say(f"Файлы в {folder}: keywords.txt (все фразы), minus.txt, report.md, " + ", ".join(written))


def cmd_region(args):
    if not load_regions():
        raise Fail("справочник регионов не найден — возьми id из адреса Вордстата (region=...)")
    matches, by_id = find_regions(args.query)
    if not matches:
        raise Fail(f"«{args.query}» не найден — выбери регион в Вордстате и возьми число из адреса (region=...)")
    for region in matches[:20]:
        say(f"{region[0]:>7}  {region[1]}  ({region_path(region, by_id)})")


# ---------- CLI ----------

def build_parser():
    parser = argparse.ArgumentParser(prog="kw.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name, func, help_text, project=True, reads=False):
        p = sub.add_parser(name, help=help_text, description=help_text)
        if project:
            p.add_argument("project", help="папка проекта")
        if reads:
            p.add_argument("--in", dest="infile", help="читать вход из файла вместо stdin")
        p.set_defaults(func=func)
        return p

    p = command("init", cmd_init, "создать проект")
    p.add_argument("--name", required=True, help="короткое название проекта")
    p.add_argument("--offer", help="что конкретно рекламируем")
    p.add_argument("--anchor", help="центральное намерение (с чем сравнивать категории и расширения)")
    p.add_argument("--region", help="регион по названию (ищется в справочнике)")
    p.add_argument("--region-id", type=int, help="id региона Вордстата (region=... в адресе)")
    p.add_argument("--target", type=int, default=TARGET_DEFAULT, help="цель по частотности в Вордстате")
    p.add_argument("--mode", choices=tuple(MODES), default="city",
                   help="city — категории объединяются; rf — аудитория на каждую категорию")
    p.add_argument("--forbid", nargs="*", help="слова запретных тем")

    p = command("card", cmd_card, "изменить карточку проекта")
    for flag in ("--name", "--offer", "--anchor", "--region"):
        p.add_argument(flag)
    p.add_argument("--region-id", type=int)
    p.add_argument("--target", type=int)
    p.add_argument("--mode", choices=tuple(MODES))
    p.add_argument("--forbid", nargs="*")
    p.add_argument("--unforbid", nargs="*")

    p = command("fork", cmd_fork, "копия списка в другой регион — посчитать вариант географии")
    p.add_argument("dest", help="папка копии")
    p.add_argument("--region", help="регион по названию")
    p.add_argument("--region-id", type=int, help="id региона Вордстата")

    p = command("add", cmd_add, "добавить фразы одной категории (по одной на строку): шаг 1 или 4", reads=True)
    p.add_argument("--category", "--branch", dest="category", required=True,
                   help="категория: один вид фраз (бизнес-обучение, авторы книг ...)")
    p.add_argument("--define", help="что входит в категорию (сохраняется при первом добавлении)")
    p.add_argument("--source", help="откуда: brief, wordstat, similar, brand ...")
    p.add_argument("--force", action="store_true", help="вернуть ранее удалённую фразу")

    p = command("rm", cmd_rm, "удалить фразы (по одной на строку)", reads=True)
    p.add_argument("--reason", required=True, help="почему: мусор, двойной смысл ... — видно в истории")

    p = command("move", cmd_move, "перенести фразы в другую категорию", reads=True)
    p.add_argument("--category", required=True)

    p = command("category", cmd_category, "определение и статус категории")
    p.add_argument("name")
    p.add_argument("--define", help="что входит в категорию")
    p.add_argument("--status", choices=("open", "exhausted", "rejected"))
    p.add_argument("--note")
    p.add_argument("--rename", help="новое название")

    p = command("minus", cmd_minus, "минус-слова: add или rm (по одному на строку)", reads=True)
    p.add_argument("action", choices=("add", "rm"))
    p.add_argument("--reason", help="какой чужой смысл отсекает")

    p = command("audit", cmd_audit, "JS для проверки двойного смысла каждой фразы", reads=True)
    p.add_argument("--limit", type=int, default=AUDIT_BATCH, help="фраз за один прогон")
    p.add_argument("--all", action="store_true", help="проверить и уже проверенные фразы")
    p.add_argument("--list", action="store_true", help="проверить фразы из stdin/--in (например, кандидатов)")

    p = command("checked", cmd_checked, "фразы прошли проверку двойного смысла (по одной на строку)", reads=True)
    p.add_argument("--note", help="что смотрели")

    p = command("js", cmd_js, "собрать JS для замера в Вордстате", reads=True)
    p.add_argument("--probe", action="append", help="доп. выражение для пробы (можно несколько)")
    p.add_argument("--probes", action="store_true", help="пробы из stdin/--in, по одной на строку")
    p.add_argument("--no-group", action="store_true", help="только пробы, без замера списка")

    command("record", cmd_record, "записать результат get_page_text", reads=True)

    p = command("cleaned", cmd_cleaned, "шаг 2.1 выполнен: мусор и двойной смысл вычищены")
    p.add_argument("--note", required=True, help="что нашли и что убрали")

    p = command("expanded", cmd_expanded, "шаг 4 закрыт (например, чистых кандидатов нет)")
    p.add_argument("--note", required=True, help="что добавлено или почему добавить нечего")

    p = command("status", cmd_status, "какой шаг следующий и цифры по проекту")
    p.add_argument("--rows", type=int, default=0, help="показать N крупнейших запросов списка")
    p.add_argument("--phrases", action="store_true", help="показать фразы по категориям")
    p.add_argument("--journal", type=int, default=5, help="сколько последних записей журнала")

    p = command("list", cmd_list, "фразы в работе (по одной на строку)")
    p.add_argument("--all", action="store_true", help="плюс удалённые, поглощённые и минус-слова")
    p.add_argument("--categories", action="store_true", help="с метками категорий")
    p.add_argument("--category", help="только эта категория")

    p = command("candidates", cmd_candidates, "новые основы из строк последнего замера")
    p.add_argument("--request", help="взять строки конкретного запроса rN")
    p.add_argument("--all", action="store_true", help="из всех замеров проекта")
    p.add_argument("--min", type=int, default=1, help="минимальная частотность строки")
    p.add_argument("--limit", type=int, default=60)

    p = command("log", cmd_log, "запись в журнал проекта")
    p.add_argument("text")

    p = command("export", cmd_export, "выгрузка для VK Ads по категориям и отчёт (на итоге)")
    p.add_argument("--draft", action="store_true", help="выгрузить черновик до итога")

    p = command("region", cmd_region, "найти id региона Вордстата", project=False)
    p.add_argument("query")
    return parser


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except Fail as error:
        sys.stderr.write(f"ОШИБКА: {error}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
