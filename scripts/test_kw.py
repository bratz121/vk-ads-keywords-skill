"""Run: python -m unittest discover -s scripts -p "test_*.py" -v   (from the skill folder)."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import kw
from ru_stem import signature, stem

END_MS = 1788814800000  # 2026-09-08 00:00 MSK, as getTable reports it


class StemTest(unittest.TestCase):
    def test_snowball_reference_pairs(self):
        pairs = {
            "важнейшие": "важн", "важничаешь": "важнича", "важности": "важност",
            "важностью": "важност", "важны": "важн", "валандался": "валанда",
            "валериановых": "валерианов", "валерию": "валер", "вали": "вал", "валил": "вал",
            "валился": "вал", "валов": "вал", "валяется": "валя", "валять": "валя",
            "грамотности": "грамотн", "курсов": "курс", "обучению": "обучен",
            "предпринимателей": "предпринимател", "начинающих": "начина",
        }
        for word, expected in pairs.items():
            with self.subTest(word=word):
                self.assertEqual(stem(word), expected)

    def test_signature_ignores_order_forms_and_stop_words(self):
        self.assertEqual(signature("бизнес обучение"), signature("обучение бизнесу"))
        self.assertEqual(signature("семинар предпринимателей"), signature("семинар для предпринимателей"))
        self.assertLess(signature("бизнес курсы"), signature("бизнес курсы для начинающих"))


class ProjectTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = str(self.root / "proj")
        self.inputs = 0
        self.ok("init", self.project, "--name", "Тест", "--region-id", "75", "--region", "Владивосток",
                "--target", "1000")

    def run_kw(self, *argv, stdin=None):
        argv = list(argv)
        if stdin is not None:
            self.inputs += 1
            path = self.root / f"input{self.inputs}.txt"
            path.write_text(stdin, encoding="utf-8")
            argv += ["--in", str(path)]
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = kw.main(argv)
        return code, buffer.getvalue()

    def ok(self, *argv, stdin=None):
        code, output = self.run_kw(*argv, stdin=stdin)
        self.assertEqual(code, 0, output)
        return output

    def state(self):
        return json.loads((Path(self.project) / "state.json").read_text(encoding="utf-8"))

    def active(self):
        return sorted(p["text"] for p in self.state()["phrases"] if p["status"] == "active")

    def pending(self):
        return json.loads((Path(self.project) / "pending.json").read_text(encoding="utf-8"))

    def status(self):
        return self.ok("status", self.project)

    def add(self, stdin, category="бизнес-обучение", *extra):
        return self.ok("add", self.project, "--category", category, *extra, stdin=stdin)

    def check_all(self):
        """Mark every active phrase as audited, as the agent does after kw.py audit."""
        phrases = "\n".join(p["text"] for p in self.state()["phrases"] if p["status"] == "active")
        return self.ok("checked", self.project, "--note", "строки чистые", stdin=phrases + "\n")

    def result(self, totals, label="Владивосток", popular=None, similar=None, exprs=None, **overrides):
        """What get_page_text returns after wordstat_batch.js: a header, then the article JSON."""
        pending = self.pending()
        results = []
        for item in pending["items"]:
            expr = (exprs or {}).get(item["id"], item["expr"])
            results.append({"id": item["id"], "h": kw.fnv1a(expr), "total": totals[item["id"]],
                            "invalid": False, "period_ms": {"startDate": 0, "endDate": END_MS},
                            "popular": popular or [["обучение бизнесу", 105]],
                            "similar": similar or [["школа бизнеса", 50]]})
        data = {"request_id": pending["request_id"], "retrieved_at": "2026-09-10T10:00:00Z",
                "page": {"region_param": "75", "region_label": label}, "results": results}
        data.update(overrides)
        data["check"] = kw.checksum(data)
        return "Title: Вордстат\nSource element: <article>\n---\n" + json.dumps(data, ensure_ascii=False)

    def measure(self, total, categories=None, **kwargs):
        """kw.py js + record of the current list with a fake Wordstat answer."""
        self.ok("js", self.project)
        totals = {"g": total}
        for item in self.pending()["items"]:
            if item["kind"] == "category":
                totals[item["id"]] = (categories or {}).get(item["category"], total)
        return self.ok("record", self.project, stdin=self.result(totals, **kwargs))

    def test_checksum_matches_the_javascript_side(self):
        # Reference values computed by fnv() from wordstat_batch.js in Chrome on 2026-09-10.
        self.assertEqual(kw.fnv1a(""), "811c9dc5")
        self.assertEqual(kw.fnv1a("a"), "e40c292c")
        self.assertEqual(kw.fnv1a("бизнес обучение"), "d4828081")
        self.assertEqual(kw.fnv1a("(бизнес обучение|курсы по бизнесу) -колледж"), "e246aa56")

    def test_add_rejects_duplicates_absorbed_and_single_words(self):
        output = self.add("бизнес курсы\nкурсы по бизнесу\n"
                          "бизнес курсы для начинающих\nобучение\nкурсы бизнеса владивосток\n")
        self.assertEqual(self.active(), ["бизнес курсы"])
        self.assertIn("дубль", output)
        self.assertIn("поглощена", output)
        self.assertIn("меньше двух", output)

    def test_phrases_live_in_categories(self):
        self.add("бизнес обучение\n", "бизнес-обучение", "--define", "все варианты бизнес-обучения")
        self.add("курсы финансовой грамотности\n", "финансовое обучение")
        output = self.add("бизнес обучение\n", "авторы книг")
        self.assertIn("категория «бизнес-обучение»", output)  # duplicates name their home category
        self.ok("move", self.project, "--category", "авторы книг", stdin="курсы финансовой грамотности\n")
        state = self.state()
        by_category = {p["text"]: p["category"] for p in state["phrases"] if p["status"] == "active"}
        self.assertEqual(by_category, {"бизнес обучение": "бизнес-обучение",
                                       "курсы финансовой грамотности": "авторы книг"})
        self.assertEqual(state["categories"]["бизнес-обучение"]["definition"], "все варианты бизнес-обучения")

    def test_minus_conflicts_are_refused(self):
        self.add("курсы по бизнесу\n")
        self.assertIn("отрежет", self.ok("minus", self.project, "add", stdin="курс\n"))
        self.ok("minus", self.project, "add", "--reason", "валюта", stdin="-доллар\n")
        self.assertEqual([m["word"] for m in self.state()["minus"]], ["доллар"])
        self.assertIn("минус-слово", self.add("бизнес доллар курс\n"))

    def test_audit_gates_cleaned_and_result(self):
        self.add("бизнес обучение\nклуб миллионеров\n")
        self.measure(600)
        code, output = self.run_kw("cleaned", self.project, "--note", "чисто")
        self.assertEqual(code, 2)
        self.assertIn("двойной смысл не проверен", output)
        audit = self.ok("audit", self.project)
        self.assertIn("бизнес обучение", audit)
        self.assertIn("markers", audit)
        self.ok("checked", self.project, stdin="бизнес обучение\n")
        self.ok("rm", self.project, "--reason", "фильм и лотерея", stdin="клуб миллионеров\n")
        self.assertIn("ШАГ 3", self.ok("cleaned", self.project, "--note", "клуб миллионеров — фильм"))
        self.assertIn("Проверять нечего", self.ok("audit", self.project))

    def test_steps_run_once_in_order(self):
        self.assertIn("ШАГ 1", self.status())
        self.add("бизнес обучение\nшкола бизнеса\n")
        self.assertIn("ШАГ 2 ", self.status())
        js = self.ok("js", self.project)
        self.assertIn("(бизнес обучение|школа бизнеса)", js)
        self.assertIn("region=75", js)
        output = self.ok("record", self.project, stdin=self.result({"g": 600}))
        self.assertIn("600", output)
        self.assertIn("ШАГ 2.1", output)
        code, output = self.run_kw("add", self.project, "--category", "форматы", stdin="бизнес тренинг\n")
        self.assertEqual(code, 2)
        self.assertIn("шаг 2.1", output)
        self.assertEqual(self.run_kw("export", self.project)[0], 2)
        self.ok("rm", self.project, "--reason", "вузовские программы", stdin="школа бизнеса\n")
        self.check_all()
        self.assertIn("ШАГ 3", self.ok("cleaned", self.project, "--note", "убрал вузы"))
        self.assertIn("ШАГ 4 — запросов недостаточно: 500 из 1 000", self.measure(500))
        self.assertEqual(self.run_kw("export", self.project)[0], 2)
        self.assertIn("Шаг 4", self.add("бизнес тренинг\n", "форматы"))
        self.assertIn("проверить двойной смысл", self.status())
        self.ok("checked", self.project, stdin="бизнес тренинг\n")
        self.assertIn("ШАГ 4 — замерить итоговый", self.status())
        output = self.measure(1200)
        self.assertIn("Δ +700", output)
        self.assertIn("ИТОГ — цель достигнута", output)
        report = self.ok("export", self.project)
        self.assertIn("1 200", report)
        self.assertIn("цель достигнута", report)
        keywords = (Path(self.project) / "export" / "keywords.txt").read_text(encoding="utf-8")
        self.assertEqual(keywords.splitlines(), ["бизнес обучение", "бизнес тренинг"])
        per_category = (Path(self.project) / "export" / "категория-форматы.txt").read_text(encoding="utf-8")
        self.assertEqual(per_category.splitlines(), ["бизнес тренинг"])

    def test_short_result_after_expansion_is_reported_not_looped(self):
        self.add("бизнес обучение\nшкола бизнеса\n")
        self.measure(300)
        self.check_all()
        self.assertIn("ШАГ 4", self.ok("cleaned", self.project, "--note", "чисто"))
        self.assertEqual(self.run_kw("export", self.project)[0], 2)
        self.assertIn("ИТОГ — недобор", self.ok("expanded", self.project, "--note", "чистых кандидатов нет"))
        report = self.ok("export", self.project)
        self.assertIn("недобор после расширения", report)
        self.assertIn("не хватает 700", report)

    def test_rf_mode_requires_every_category(self):
        self.ok("card", self.project, "--mode", "rf")
        self.add("бизнес обучение\n", "бизнес-обучение")
        self.add("курсы финансовой грамотности\n", "финансовое обучение")
        self.measure(2000, categories={"бизнес-обучение": 1500, "финансовое обучение": 400})
        self.check_all()
        output = self.ok("cleaned", self.project, "--note", "чисто")
        self.assertIn("«финансовое обучение» 400", output)
        self.assertNotIn("«бизнес-обучение»", output.split("Дальше:")[-1])
        self.assertEqual(self.run_kw("export", self.project)[0], 2)
        self.add("обучение личным финансам\n", "финансовое обучение")
        self.ok("checked", self.project, stdin="обучение личным финансам\n")
        output = self.measure(2400, categories={"бизнес-обучение": 1500, "финансовое обучение": 1100})
        self.assertIn("ИТОГ — все категории", output)
        report = self.ok("export", self.project)
        self.assertIn("финансовое обучение: 2 фраз, 1 100", report)

    def test_record_fails_closed(self):
        self.add("бизнес обучение\nшкола бизнеса\n")
        self.ok("js", self.project)
        good = self.result({"g": 500})
        self.assertEqual(self.run_kw("record", self.project,
                                     stdin=self.result({"g": 500}, request_id="r99"))[0], 2)
        self.assertEqual(self.run_kw("record", self.project,
                                     stdin=self.result({"g": 500}, label="Хабаровск"))[0], 2)
        other_expr = self.result({"g": 500}, exprs={"g": "(бизнес обучение|бизнес книги)"})
        self.assertEqual(self.run_kw("record", self.project, stdin=other_expr)[0], 2)
        mistyped = good.replace('"total": 500', '"total": 5000')
        self.assertNotEqual(mistyped, good)
        self.assertIn("контрольная сумма", self.run_kw("record", self.project, stdin=mistyped)[1])
        self.assertEqual(self.state()["measurements"], [])
        self.ok("record", self.project, stdin=good)
        self.assertEqual(self.run_kw("record", self.project, stdin=good)[0], 2)

    def test_packets_report_a_range(self):
        original = kw.MAX_EXPR_CHARS
        kw.MAX_EXPR_CHARS = 40
        self.addCleanup(setattr, kw, "MAX_EXPR_CHARS", original)
        self.add("бизнес обучение\nшкола бизнеса\nбизнес тренинг\n")
        self.ok("js", self.project)
        ids = [item["id"] for item in self.pending()["items"] if item["kind"] == "group"]
        self.assertGreater(len(ids), 1)
        totals = {item["id"]: 0 for item in self.pending()["items"]}
        totals.update({item_id: 300 + 100 * index for index, item_id in enumerate(ids)})
        self.ok("record", self.project, stdin=self.result(totals))
        group = next(m for m in self.state()["measurements"] if m["kind"] == "group")
        self.assertFalse(group["exact"])
        packet_totals = [totals[i] for i in ids]
        self.assertEqual((group["total_low"], group["total_high"]),
                         (max(packet_totals), sum(packet_totals)))

    def test_fork_copies_the_list_for_another_region(self):
        self.add("бизнес обучение\nшкола бизнеса\n")
        self.ok("minus", self.project, "add", stdin="вакансии\n")
        self.measure(500)
        self.check_all()
        self.ok("cleaned", self.project, "--note", "чисто")
        self.add("бизнес тренинг\n", "форматы")
        copy = str(self.root / "copy")
        self.ok("fork", self.project, copy, "--region-id", "11409", "--region", "Приморский край")
        forked = json.loads((Path(copy) / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(forked["project"]["region"], {"id": 11409, "name": "Приморский край", "verified": None})
        self.assertEqual(sorted(p["text"] for p in forked["phrases"]),
                         ["бизнес обучение", "бизнес тренинг", "школа бизнеса"])
        self.assertEqual({p["stage"] for p in forked["phrases"]}, {"1"})
        self.assertEqual({p["category"] for p in forked["phrases"] if p["status"] == "active"},
                         {"бизнес-обучение", "форматы"})
        self.assertEqual([m["word"] for m in forked["minus"]], ["вакансии"])
        self.assertEqual((forked["measurements"], forked["cleaned"]), ([], None))
        self.assertEqual(self.run_kw("fork", self.project, copy, "--region-id", "225", "--region", "Россия")[0], 2)

    def test_candidates_are_new_bases_only(self):
        self.add("бизнес обучение\n")
        self.ok("minus", self.project, "add", stdin="вакансии\n")
        self.ok("js", self.project, "--probe", "бизнес")
        rows = [["обучение бизнесу онлайн", 90], ["бизнес школа", 80], ["бизнес вакансии", 70],
                ["бизнес", 60], ["курсы предпринимателей", 40]]
        self.ok("record", self.project, stdin=self.result({"g": 200, "p1": 900}, popular=rows, similar=rows))
        output = self.ok("candidates", self.project)
        self.assertIn("бизнес школа", output)
        self.assertIn("курсы предпринимателей", output)
        self.assertNotIn("обучение бизнесу онлайн", output)
        self.assertNotIn("бизнес вакансии", output)

    def test_projects_from_the_first_version_migrate(self):
        self.add("бизнес обучение\n")
        self.measure(300)
        legacy = self.state()
        for key in ("cleaned", "expanded", "categories"):
            legacy.pop(key)
        for phrase in legacy["phrases"]:
            phrase.pop("stage")
            phrase.pop("category")
            phrase.pop("checked")
            phrase["branch"] = "прямой спрос"
        legacy["reviews"] = {"abc": {"note": "старая чистка", "at": "2026-09-10T10:00:00+03:00",
                                     "measurement": "m1"}}
        legacy["branches"] = {"прямой спрос": {"status": "open", "note": "", "updated_at": "x"}}
        (Path(self.project) / "state.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        output = self.status()
        self.assertIn("Шаг 2.1: выполнен — старая чистка", output)
        self.assertIn("прямой спрос", output)
        self.assertIn("проверить двойной смысл", output)


if __name__ == "__main__":
    unittest.main()
