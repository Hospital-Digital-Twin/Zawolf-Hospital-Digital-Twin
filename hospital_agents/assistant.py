"""AI Assistant: answers questions about the hospital from the backend's own data.

How it stays trustworthy
  1. Understanding and numbers are deterministic. The question is routed to an intent (English or Arabic keywords),
     and the backend gathers *evidence*: numbered, localized sentences computed from the CSV data, the forecast
     module, the agents' baseline run, stored decisions, data-quality checks or the policy corpus, each with its
     source table and period. This works with no API key.
  2. If an LLM is configured it may only re-word and combine that evidence into an answer to the question. Every
     sentence must cite evidence ids and pass the same support check as the executive summary (numbers, wording,
     no clinical language). Otherwise the local answer is returned and the reason is reported.
  3. It never executes anything and never gives clinical advice; it says so when asked.
Conversation context (the last turns, the unit and topic) is kept per signed-in user for follow-up questions.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Any

from . import insights, llm
from .explain import parse_structured, validate_structured

MAX_TURNS = 16
UNITS = {"ICU": r"\bicu\b|intensive|عناية", "WARD": r"\bward\b|inpatient|داخلي|القسم الداخلي", "ER": r"\ber\b|emergency|طوارئ"}

INTENTS = [
    ("actions", r"\b(approve|apply|execute|roll ?back|reject|delete|confirm)\b|اعتمد|نفّ?ذ|طبّ?ق|تراجع عن|ارفض|احذف"),
    ("clinical", r"diagnos|treat|medicat|dose|dosage|prescri|symptom|تشخيص|علاج|دواء|جرعة|أعراض"),
    ("glossary", r"\bwhat (is|are|does)\b|\bdefine\b|\bmeaning\b|explain the (term|metric)|ما هو|ما هي|ماذا يعني|يعني إيه|معنى|اشرح لي"),
    ("sources", r"polic|source|guideline|protocol|escalat|nurse ratio|سياسة|مصدر|إرشاد|بروتوكول|تصعيد"),
    ("attention", r"attention|alert|problem|issue|urgent|critical|worr|wrong|تنبيه|مشكل|عاجل|حرج|انتباه|خطر"),
    ("recommend", r"recommend|suggest|what should|proposal|agents?\b|اقتراح|توصي|وكيل|الوكلاء|ماذا نفعل|ماذا يجب"),
    ("decisions", r"decision|pending|approved|sandbox|قرار|معلق|معتمد"),
    ("forecast", r"forecast|predict|accura|wape|mape|backtest|توقع|دقة|تنبؤ"),
    ("er", r"\ber\b|emergency|wait|queue|طوارئ|انتظار"),
    ("staff", r"staff|nurse|doctor|overtime|workload|shift|طاقم|تمريض|ممرض|طبيب|أطباء|إضافي|ورديات|وردية"),
    ("or", r"operating|surger|theat|\bor\b|عمليات|جراح"),
    ("beds", r"\bbeds?\b|occupan|\bicu\b|ward|capacity|bottleneck|أسرة|سرير|إشغال|عناية|داخلي|سعة|اختناق"),
    ("quality", r"quality|data pack|llm|groq|system|status|available|جودة|بيانات|النظام|حالة"),
]

DATA_INTENTS = {"attention", "recommend", "decisions", "forecast", "er", "staff", "or", "beds", "quality", "sources"}

GLOSSARY = {
    "wape": ("WAPE: total absolute forecast error divided by total actual value. Lower is better; above 1 means the forecast missed by more than the quantity itself. It inflates when actual values are tiny, as with ICU occupancy.",
             "WAPE: مجموع الخطأ المطلق للتوقع مقسومًا على مجموع القيم الفعلية. الأقل أفضل، وأكبر من 1 يعني أن الخطأ أكبر من الكمية نفسها. ويتضخم عندما تكون القيم الفعلية صغيرة جدًا كما في إشغال العناية المركزة."),
    "mape": ("MAPE: the average of each day's absolute error as a share of that day's actual value. Days with an actual value of zero are skipped.", "MAPE: متوسط الخطأ المطلق لكل يوم كنسبة من القيمة الفعلية لذلك اليوم. تُتخطى الأيام التي قيمتها الفعلية صفر."),
    "occupancy": ("Occupancy is occupied beds divided by the unit's beds, from the bed-stay intervals active at the chosen moment.", "الإشغال هو الأسرة المشغولة مقسومة على أسرة القسم، من فترات الإقامة النشطة في اللحظة المختارة."),
    "utilisation": ("OR utilisation is the share of operating-room time that was used on a day, as recorded in the daily metrics.", "استخدام غرف العمليات هو نسبة وقت الغرف المستخدم في اليوم كما تسجله المؤشرات اليومية."),
    "nurse": ("Occupied beds per nurse is an observed ratio at the chosen moment. It is not a policy limit; the limit comes from a nurse-ratio policy an Operations Manager enters.", "الأسرة المشغولة لكل ممرض نسبة مرصودة في اللحظة المختارة وليست حدًا تنظيميًا؛ الحد يأتي من سياسة نسبة التمريض التي يدخلها مدير التشغيل."),
    "replay": ("Replay moment: the dashboard rebuilds the hospital as it stood then and forecasts using only earlier data, so later days can check the forecast.", "لحظة إعادة التشغيل: تعيد اللوحة بناء المستشفى كما كان وقتها وتتوقع باستخدام بيانات أسبق فقط، فيمكن للأيام اللاحقة اختبار التوقع."),
    "baseline": ("The forecasts are weekday-average baselines: the average of the same weekday in earlier weeks. They are not trained models and have no confidence score.", "التوقعات خطوط أساس بمتوسط يوم الأسبوع: متوسط نفس اليوم في الأسابيع السابقة. ليست نماذج مدرَّبة وليس لها درجة ثقة."),
    "constraint": ("Hard constraints are checks no agent can override: pool limits, slot availability and nurse-ratio policy. A proposal that cannot be verified is rejected.", "القيود الصارمة فحوص لا يستطيع أي وكيل تجاوزها: حدود الموارد وتوفر المواعيد وسياسة نسبة التمريض. الاقتراح الذي لا يمكن التحقق منه يُرفض."),
    "sandbox": ("The sandbox is a copy of the twin where approved decisions are applied. Nothing is ever sent to a real hospital system.", "الـ sandbox نسخة من التوأم تُطبَّق عليها القرارات المعتمدة. لا يُرسل أي شيء إلى نظام مستشفى حقيقي."),
    "confidence": ("Confidence is shown only when the forecast supplies one and a simulator verified the impact. Otherwise it is unavailable; it is never estimated.", "درجة الثقة تظهر فقط عندما يوفرها التوقع ويتحقق المحاكي من الأثر. وإلا فهي غير متاحة ولا تُقدَّر."),
    "shortfall": ("Shortfall is the beds needed at the forecast peak minus the beds available.", "العجز هو الأسرة المطلوبة عند ذروة التوقع ناقص الأسرة المتاحة."),
    "lwbs": ("Left without being seen: ER visits where the patient left before being examined, as recorded in the visits table.", "غادر قبل أن يُفحص: زيارات الطوارئ التي غادر فيها المريض قبل الفحص كما في جدول الزيارات."),
}
GLOSSARY_KEYS = {"wape": r"wape", "mape": r"mape", "occupancy": r"occupan|إشغال", "utilisation": r"utili[sz]ation|استخدام", "nurse": r"per nurse|nurse ratio|لكل ممرض|نسبة التمريض",
                 "replay": r"replay|إعادة التشغيل", "baseline": r"baseline|خط الأساس|خطوط الأساس", "constraint": r"constraint|قيد|قيود", "sandbox": r"sandbox", "confidence": r"confidence|ثقة",
                 "shortfall": r"shortfall|عجز", "lwbs": r"lwbs|without being seen|قبل الفحص"}

SUGGESTIONS = {
    "en": ["What needs attention right now?", "How is ICU occupancy?", "How is the ER doing?", "Which staff are most loaded?", "How accurate are the forecasts?", "What do the agents recommend?", "What does WAPE mean?"],
    "ar": ["ما الذي يحتاج انتباهي الآن؟", "كيف حال إشغال العناية المركزة؟", "كيف حال الطوارئ؟", "من هو الطاقم الأكثر ضغطًا؟", "ما مدى دقة التوقعات؟", "ماذا يقترح الوكلاء؟", "ماذا يعني WAPE؟"],
}


class Assistant:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.sessions: dict[str, list[dict]] = {}

    # ── conversation ──
    def history(self, user: str) -> list[dict]:
        return self.sessions.get(user, [])

    def reset(self, user: str) -> None:
        self.sessions.pop(user, None)

    def _remember(self, user: str, **turn) -> None:
        h = self.sessions.setdefault(user, [])
        h.append(turn)
        del h[:-MAX_TURNS]

    # ── routing ──
    @staticmethod
    def route(message: str, last: dict | None) -> tuple[str, str | None]:
        m = message.lower()
        unit = next((u for u, rx in UNITS.items() if re.search(rx, m)), None)
        intent = next((name for name, rx in INTENTS if re.search(rx, m)), None)
        if intent == "glossary" and not any(re.search(rx, m) for rx in GLOSSARY_KEYS.values()):
            intent = next((name for name, rx in INTENTS[3:] if re.search(rx, m)), None)
        if intent is None and last and last.get("intent") in DATA_INTENTS and len(m.split()) <= 6:      # short follow-up: keep the topic
            intent = last["intent"]
        if unit is None and last and last.get("unit") and re.search(r"\b(and|what about|how about|it|there)\b|وماذا|و ?ال|عنها|بالنسبة", m):
            unit = last["unit"]
        return intent or "unknown", unit

    # ── public ──
    def ask(self, user: str, message: str, as_of: datetime, language: str) -> dict:
        message = message.strip()[:500]
        lang = language if language in ("ar", "en") else "ar"
        T = lambda en, ar: ar if lang == "ar" else en
        history = self.history(user)
        intent, unit = self.route(message, history[-1] if history else None)
        evidence: list[dict] = []
        notes: list[str] = []
        mode, answer = "local", ""
        if intent == "actions":
            answer = T("I cannot approve, apply, reject or roll back anything. An Operations Manager does that on the Decisions page, with a documented approval, and only in the sandbox.",
                       "لا أستطيع اعتماد أو تطبيق أو رفض أو التراجع عن أي شيء. مدير التشغيل يقوم بذلك من صفحة القرارات باعتماد موثق وداخل الـ sandbox فقط.")
        elif intent == "clinical":
            answer = T("I cannot give clinical advice or discuss individual patients. I only explain the operational data of the hospital.",
                       "لا أستطيع تقديم نصيحة طبية أو مناقشة مرضى بعينهم. أشرح فقط البيانات التشغيلية للمستشفى.")
        elif intent == "glossary":
            key = next(k for k, rx in GLOSSARY_KEYS.items() if re.search(rx, message.lower()))
            en, ar = GLOSSARY[key]
            evidence = [self._ev("E1", T("Built-in glossary", "قاموس المصطلحات المدمج"), T(en, ar), "glossary", None)]
            answer = evidence[0]["text"] + " [E1]"
        elif intent == "unknown":
            answer = T("I could not match that to the data in this system. Ask about beds and ICU, the ER, staff, operating rooms, forecasts, agents' recommendations, decisions, data quality or policies.",
                       "لم أستطع ربط السؤال بالبيانات الموجودة في النظام. اسأل عن الأسرة والعناية المركزة أو الطوارئ أو الطاقم أو غرف العمليات أو التوقعات أو توصيات الوكلاء أو القرارات أو جودة البيانات أو السياسات.")
        else:
            evidence = self._gather(intent, unit, message, as_of, lang)
            local = " ".join(f"{e['text']} [{e['id']}]" for e in evidence)
            answer = local or T("No data is available for that question at this moment.", "لا تتوفر بيانات لهذا السؤال في هذه اللحظة.")
            if evidence and llm.config()["configured"]:
                result = self._ask_llm(message, history, evidence, lang)
                if result["answer"]:
                    answer, mode = result["answer"], "llm"
                else:
                    mode = "llm_fallback"
                    notes.append(result["note"])
        out = {"answer": answer, "mode": mode, "intent": intent, "unit": unit, "evidence": evidence, "notes": notes,
               "suggestions": SUGGESTIONS[lang] if intent in ("unknown", "actions", "clinical") else [], "as_of": as_of.isoformat()}
        self._remember(user, role="user", text=message, intent=intent, unit=unit)
        self._remember(user, role="assistant", text=re.sub(r"\s*\[E\d+\]", "", answer), intent=intent, unit=unit)
        return out

    # ── LLM path ──
    def _ask_llm(self, message: str, history: list[dict], evidence: list[dict], lang: str) -> dict:
        by_id = {e["id"]: e["text"] for e in evidence}
        system = ("You answer questions for a hospital operations dashboard. Use ONLY the evidence sentences provided. Reply with ONLY JSON: "
                  '{"sentences":[{"text":"...","evidence":["E1"]}]} with at most 6 sentences. Every sentence must cite the evidence ids it uses, may only restate or combine them, '
                  "must not add facts or numbers, must not give clinical advice or assurances, and must not claim any action was taken. Use the conversation only to understand the question. "
                  f"Write in {'Arabic' if lang == 'ar' else 'English'}.")
        payload = json.dumps({"question": message, "conversation": [{"role": h["role"], "text": h["text"][:300]} for h in history[-4:]],
                              "evidence": [{"id": k, "text": v} for k, v in by_id.items()]}, ensure_ascii=False)
        try:
            text = asyncio.run(llm.completion()(system, payload))
        except llm.LLMError as e:
            return {"answer": None, "note": f"llm_{e.category}"}
        except Exception:
            return {"answer": None, "note": "llm_error"}
        items = parse_structured(text)
        sentences = validate_structured(items, by_id, max_sentences=6) if items else None
        if not sentences:
            return {"answer": None, "note": "llm_unsupported"}
        return {"answer": " ".join(f"{s} [{', '.join(i['evidence'])}]" for s, i in zip(sentences, items)), "note": None}

    # ── evidence ──
    @staticmethod
    def _ev(id_: str, label: str, text: str, table: str, period: str | None) -> dict:
        return {"id": id_, "label": label, "text": text, "table": table, "period": period}

    def _gather(self, intent: str, unit: str | None, message: str, as_of: datetime, lang: str) -> list[dict]:
        td, T = self.app.twin, (lambda en, ar: ar if lang == "ar" else en)
        items: list[tuple[str, str, str, str | None]] = []                    # label, text, table, period
        add = lambda label, text, table, period=None: items.append((label, text, table, period))
        period = lambda p: f"{p['from']} – {p['to']}"
        units = [unit] if unit else ["ER", "ICU", "WARD"]
        nm = {"ER": T("ER", "الطوارئ"), "ICU": T("ICU", "العناية المركزة"), "WARD": T("Ward", "القسم الداخلي")}
        if intent == "beds":
            cap = insights.capacity(td, as_of)
            for u in [x for x in units if x != "ER"] or ["ICU", "WARD"]:
                d = cap["units"][u]
                add(T(f"{u} beds", f"أسرة {nm[u]}"), T(f"{nm[u]}: {d['occupied_now']} of {d['beds_total']} beds are occupied ({d['occupancy_now_pct']}%), with {d['nurses_on_shift']} nurse(s) on shift.",
                    f"{nm[u]}: {d['occupied_now']} من {d['beds_total']} أسرة مشغولة ({d['occupancy_now_pct']}٪) مع {d['nurses_on_shift']} ممرض في الوردية."), "beds, bed_stays, staff_shifts", as_of.isoformat())
                if d["peak_forecast_upper_pct"] is not None:
                    add(T(f"{u} forecast", f"توقع {nm[u]}"), T(f"The forecast peak for {nm[u]} over the next days is {d['peak_forecast_upper_pct']:.1f}% occupancy (upper bound of a weekday-average baseline).",
                        f"ذروة التوقع لـ{nm[u]} خلال الأيام القادمة {d['peak_forecast_upper_pct']:.1f}٪ إشغال (الحد الأعلى لخط أساس بمتوسط يوم الأسبوع)."), "daily_metrics", None)
            unmet = self.app._alerts_for(as_of)["unmet"]
            add(T("Capacity check", "فحص السعة"), (T("The agents found a shortfall: " + ", ".join(f"{k} {v}" for k, v in unmet.items()) + ".", "وجد الوكلاء عجزًا: " + "، ".join(f"{k} {v}" for k, v in unmet.items()) + ".") if unmet
                else T("No bed shortfall is forecast at the baseline.", "لا يوجد عجز متوقع في الأسرة في الوضع الأساسي.")), "agents baseline run", as_of.isoformat())
        elif intent == "er":
            cap = insights.capacity(td, as_of)["units"]["ER"]
            f = cap["flow"]
            add(T("ER waiting", "انتظار الطوارئ"), T(f"On the last complete day the ER averaged {f['wait_avg_history'][-1]} minutes of waiting (90th percentile {f['wait_p90_history'][-1]}) with {int(f['arrivals_history'][-1])} arrivals.",
                f"في آخر يوم كامل بلغ متوسط الانتظار في الطوارئ {f['wait_avg_history'][-1]} دقيقة (النسبة المئوية ٩٠: {f['wait_p90_history'][-1]}) مع {int(f['arrivals_history'][-1])} وافدًا."), "daily_metrics", None)
            busiest = max(range(24), key=lambda h: f["arrivals_by_hour"][h])
            add(T("ER arrivals by hour", "الوافدون حسب الساعة"), T(f"Arrivals peak around {busiest:02d}:00, about {f['arrivals_by_hour'][busiest]} patients per hour on average.", f"تبلغ ذروة الوافدين قرابة الساعة {busiest:02d}:00 بمتوسط {f['arrivals_by_hour'][busiest]} مريض في الساعة."), "visits", period(f["window"]))
            add(T("ER outcomes", "نتائج الطوارئ"), T(f"{f['left_without_being_seen_pct']}% of ER visits ended with the patient leaving before being seen.", f"{f['left_without_being_seen_pct']}٪ من زيارات الطوارئ انتهت بمغادرة المريض قبل الفحص."), "visits", period(f["window"]))
        elif intent == "staff":
            s = insights.staff(td, as_of)
            on = {(x["unit"], x["kind"]): x["count"] for x in s["on_shift_now"]}
            for u in units:
                add(T(f"{u} staff", f"طاقم {nm[u]}"), T(f"{nm[u]} has {on.get((u, 'nurses'), 0)} nurse(s) and {on.get((u, 'doctors'), 0)} doctor(s) on shift at this moment.", f"في {nm[u]} {on.get((u, 'nurses'), 0)} ممرض و{on.get((u, 'doctors'), 0)} طبيب في الوردية في هذه اللحظة."), "staff_shifts", as_of.isoformat())
            add(T("Overtime", "العمل الإضافي"), T(f"Overtime on the last complete day was {s['overtime_hours']['values'][-1]} hours.", f"بلغ العمل الإضافي في آخر يوم كامل {s['overtime_hours']['values'][-1]} ساعة."), "daily_metrics", period(s["history_period"]))
            top = max(s["workload_by_unit"], key=lambda w: w["mean_pct"])
            add(T("Workload", "الحمل"), T(f"The most loaded department by contracted hours is {top['unit']} at {top['mean_pct']}% on average; {top['at_or_above_90_pct']} staff are at 90% or more.", f"أكثر الأقسام حملًا بحسب الساعات التعاقدية هو {top['unit']} بمتوسط {top['mean_pct']}٪ ويوجد {top['at_or_above_90_pct']} من الطاقم عند ٩٠٪ أو أكثر."), "staff, staff_shifts", None)
        elif intent == "or":
            o = insights.operating_rooms(td, as_of)
            add(T("OR rates", "معدلات العمليات"), T(f"Over the period {o['delay_rate_pct']}% of planned surgeries were delayed and {o['cancel_rate_pct']}% cancelled, with an expected margin of {o['cancelled_expected_margin_egp']:,.0f} EGP on the cancelled cases.",
                f"خلال الفترة تأخرت {o['delay_rate_pct']}٪ من العمليات المخططة وأُلغيت {o['cancel_rate_pct']}٪ بهامش متوقع {o['cancelled_expected_margin_egp']:,.0f} جنيه للحالات الملغاة."), "surgeries", period(o["history_period"]))
            add(T("OR utilisation", "استخدام الغرف"), T(f"OR utilisation on the last complete day was {o['utilisation_history_pct'][-1]}%.", f"بلغ استخدام غرف العمليات في آخر يوم كامل {o['utilisation_history_pct'][-1]}٪."), "daily_metrics", None)
            add(T("Booked ahead", "المحجوز قادمًا"), T(f"{sum(o['upcoming']['bookings'])} surgeries are booked after this moment, {sum(o['upcoming']['movable_elective'])} of them elective.", f"يوجد {sum(o['upcoming']['bookings'])} عملية محجوزة بعد هذه اللحظة منها {sum(o['upcoming']['movable_elective'])} اختيارية."), "surgeries", None)
        elif intent == "forecast":
            f = insights.forecasts(td, as_of)
            wanted = {"ICU": "icu_occ", "WARD": "ward_occ", "ER": "er_wait"}.get(unit or "", None)
            for s in f["series"]:
                if wanted and s["key"] != wanted:
                    continue
                r = s["rolling"]
                best = r["methods"][r["best_by_mae"]]
                add(T(s["title"], s["title"]), T(f"{s['title']}: over the 28 days before this moment the best method by absolute error was {r['best_by_mae'].replace('_', ' ')} (mean error {best['mae']} {s['unit']}). The forecast is an uncalibrated baseline.",
                    f"{s['title']}: خلال الـ٢٨ يومًا قبل هذه اللحظة كانت أفضل طريقة بحسب الخطأ المطلق هي {r['best_by_mae'].replace('_', ' ')} (متوسط الخطأ {best['mae']} {s['unit']}). التوقع خط أساس غير معاير."), "daily_metrics", period(r["window"]))
        elif intent in ("attention", "recommend"):
            al = self.app._alerts_for(as_of)
            if intent == "attention":
                for a in al["alerts"]:
                    if a["severity"] != "info":
                        add(T(a["source"], a["source"]), (a["title_ar"] + ". " + a["detail_ar"]) if lang == "ar" else (a["title"] + ". " + a["detail"]), "alerts", as_of.isoformat())
                if not items:
                    add(T("Alerts", "التنبيهات"), T("Nothing needs attention at this moment.", "لا شيء يحتاج انتباهك في هذه اللحظة."), "alerts", as_of.isoformat())
            else:
                d = al["baseline"]
                add(T("Agents", "الوكلاء"), T(f"At the baseline the agents proposed {d['accepted']} action(s) that pass every hard constraint and {d['rejected']} that were rejected.", f"في الوضع الأساسي اقترح الوكلاء {d['accepted']} إجراء يجتاز كل القيود الصارمة ورُفض {d['rejected']}."), "agents baseline run", as_of.isoformat())
                for r in al["recommendations"][:3]:
                    add(T(r["agent"], r["agent"]), T(f"The {r['agent']} agent proposes: {r['summary']}.", f"يقترح وكيل {r['agent']}: {r['summary']}."), "agents baseline run", as_of.isoformat())
        elif intent == "decisions":
            ds = self.app.decisions()
            from collections import Counter
            c = Counter(d["status"] for d in ds)
            add(T("Decisions", "القرارات"), T("Stored decisions: " + (", ".join(f"{n} {k.lower().replace('_', ' ')}" for k, n in c.items()) or "none") + f". The sandbox holds {len(self.app.applied)} applied decision(s).",
                "القرارات المحفوظة: " + ("، ".join(f"{n} {k}" for k, n in c.items()) or "لا شيء") + f". يحتوي الـ sandbox على {len(self.app.applied)} قرار مطبق."), "decisions store", None)
        elif intent == "quality":
            q = self.app.quality()
            add(T("Data quality", "جودة البيانات"), T(f"Data checks: {q['summary'].get('pass', 0)} passed, {q['summary'].get('warn', 0)} to watch, {q['summary'].get('fail', 0)} failed. The data is a synthetic demo pack.",
                f"فحوص البيانات: {q['summary'].get('pass', 0)} ناجح و{q['summary'].get('warn', 0)} للمتابعة و{q['summary'].get('fail', 0)} فاشل. البيانات حزمة تجريبية اصطناعية."), "data/*.csv", period(q["period"]))
            cfg = llm.config()
            add(T("LLM", "النموذج اللغوي"), T("The language model is " + ("connected." if cfg["configured"] else "not connected, so answers come from local rules."), "النموذج اللغوي " + ("متصل." if cfg["configured"] else "غير متصل، لذلك تأتي الإجابات من القواعد المحلية.")), "system", None)
        elif intent == "sources":
            k = self.app.knowledge({"q": [message]})
            for i, p in enumerate(k["passages"][:3], 1):
                add(p["source"], p["text"][:280], "policy corpus", None)
            if not items:
                add(T("Sources", "المصادر"), T("No passage in the policy corpus matches that question.", "لا يوجد مقطع في مكتبة السياسات يطابق هذا السؤال."), "policy corpus", None)
        return [self._ev(f"E{i}", label, text, table, per) for i, (label, text, table, per) in enumerate(items, 1)]
