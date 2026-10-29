"""Explanations are written *from verified results only*.

The LLM is optional and untrusted. It never sees free data: it receives a short list of numbered
evidence sentences built by the backend, and must answer in JSON where every sentence cites the
evidence ids it relies on. A sentence is accepted only if
  * every number in it appears in the evidence it cites,
  * every word is either in that evidence or in a small neutral operational glossary
    (so an invented claim such as "all patients can be discharged" has nowhere to hide),
  * it contains no clinical, safety-assurance or guarantee language.
Anything else falls back to the deterministic template. Number matching alone is not enough:
a fabricated sentence with no numbers would pass it.
"""
from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable, Mapping

from .ports import Explanation

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,")
_NUM = re.compile(r"\d+(?:[.,]\d+)*")
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)

BANNED = re.compile(
    r"\b(discharg\w*|safe|safely|safety|clinical\w*|diagnos\w*|treat\w*|therap\w*|medic\w*|prescrib\w*|cure\w*|prognos\w*|surviv\w*|"
    r"guarantee\w*|risk[- ]free|harmless|symptom\w*|dose|dosage)\b|\bno\b[^.]{0,24}\brisks?\b|"
    r"تشخيص|علاج\w*|آمن\w*|امن\w*|مضمون\w*|دواء|أدوية|سريري\w*|أعراض|جرعة|خروج المرضى|بدون مخاطر|لا مخاطر", re.I)

FUNCTION_EN = set("a an the of in on at to for and or is are be was were it its this that with by as from not than then there which who can may must after before any each per into over under about also more less most least both either only still yet".split())
FUNCTION_AR = set("في من إلى على عن مع أو و ثم هذا هذه ذلك التي الذي كل بعد قبل أي لا لم لن قد هو هي هم كان تم يتم".split())
GLOSSARY_EN = set("""demand expected expect now rise rises rising increase increases decrease decreases fall falls percent percentage beds bed capacity short shortage
shortfall unmet covered cover proposal proposals action actions recommended recommend accepted rejected reject constraint constraints hard human approval required requires
sandbox applied apply change changes execution confidence unavailable available waiting wait time minutes minute simulation forecast observed data critical bottleneck
found none unit units nurses nurse doctors doctor staff decision review operations manager peak units hospital replay moment verified unverified policy source sources
sources ratio utilisation occupancy occupied arrivals patients overtime hours rooms room surgeries elective reschedule rescheduled moved move total number level higher lower
current latest day days week period compared against between among three two one four five six seven eight nine ten few several many""".split())
GLOSSARY_AR = set("""الطلب المتوقع متوقع الآن يرتفع ارتفاع ينخفض انخفاض المئة بالمئة نسبة الأسرة سرير سرة السعة نقص عجز غير مغطى مغطى اقتراح اقتراحات إجراء إجراءات
موصى مقبول مقبولة مرفوض مرفوضة قيد قيود صارمة بشري اعتماد مطلوب يتطلب الاعتماد الـ sandbox مطبق تطبيق تغيير تغييرات تنفيذ ثقة الثقة غير متاح متاحة متاح انتظار الانتظار وقت زمن دقيقة دقائق محاكاة توقع
مرصود بيانات حرج حرجة اختناق نقطة لم يوجد الأقسام قسم وحدة وحدات الممرضون التمريض الأطباء الطاقم قرار مراجعة التشغيل مدير ذروة المستشفى لحظة متحقق سياسة مصدر المصادر
إشغال مشغولة الوافدون العمل الإضافي ساعات غرف العمليات الاختيارية إعادة جدولة نقل إجمالي عدد مستوى أعلى أقل الحالي آخر يوم أيام أسبوع فترة مقارنة بين""".split())


def numbers_in(text: str) -> set[float]:
    out = set()
    for m in _NUM.findall(text.translate(_AR_DIGITS)):
        try:
            out.add(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def words_in(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text.translate(_AR_DIGITS))}


def sentences_of(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?؟])\s+|\n+", text) if s.strip()]


def _numbers_match(sentence: str, allowed: set[float]) -> bool:
    return all(any(abs(n - a) <= 0.051 for a in allowed) for n in numbers_in(sentence))


def claim_problem(sentence: str, evidence_text: str) -> str | None:
    """Why a sentence is not supported by the given evidence, or None when it is."""
    if BANNED.search(sentence):
        return "clinical or assurance language"
    if not _numbers_match(sentence, numbers_in(evidence_text)):
        return "a number that is not in the evidence"
    allowed = words_in(evidence_text) | FUNCTION_EN | FUNCTION_AR | GLOSSARY_EN | GLOSSARY_AR
    extra = sorted(w for w in words_in(sentence) if w not in allowed and len(w) > 1)
    return f"unsupported words: {', '.join(extra[:4])}" if extra else None


def is_grounded(text: str, facts: Mapping[str, Any]) -> bool:
    """True only if every sentence is supported by `facts` (numbers, wording and safety language)."""
    evidence = json.dumps(facts, ensure_ascii=False)
    sentences = sentences_of(text)
    return bool(sentences) and all(claim_problem(s, evidence) is None for s in sentences)


# ───────────────────────── deterministic wording ─────────────────────────

_T = {
    "en": {
        "head": "Decision {id}: {n} action(s) recommended, {r} rejected.", "bottleneck": "Critical bottleneck: {b}.",
        "none": "No critical bottleneck was found.", "demand": "Expected demand is {p}% compared with now.",
        "unmet": "Still unmet after the accepted actions: {list}.", "wait": "Simulation estimates {m:g} minutes of waiting saved.",
        "nowait": "The waiting-time effect is unavailable because no simulation engine is connected.",
        "conf": "Confidence {c:.2f}.", "noconf": "Confidence is unavailable (missing forecast confidence or unverified impact).",
        "approval": "Requires human approval before any execution.",
    },
    "ar": {
        "head": "القرار {id}: {n} إجراء مقترح و{r} مرفوض.", "bottleneck": "نقطة الاختناق الحرجة: {b}.",
        "none": "لم يتم العثور على نقطة اختناق حرجة.", "demand": "الطلب المتوقع {p}٪ مقارنة بالآن.",
        "unmet": "ما زال غير مغطى بعد الإجراءات المقبولة: {list}.", "wait": "تقدّر المحاكاة توفير {m:g} دقيقة انتظار.",
        "nowait": "أثر زمن الانتظار غير متاح لعدم وجود محرك محاكاة متصل.",
        "conf": "درجة الثقة {c:.2f}.", "noconf": "درجة الثقة غير متاحة (لا توجد ثقة للتوقع أو الأثر غير مُتحقق منه).",
        "approval": "يتطلب اعتماد بشري قبل أي تنفيذ.",
    },
}


def build_evidence(facts: Mapping[str, Any], language: str) -> dict[str, str]:
    """Numbered evidence sentences derived from the executive card. The only text the LLM may rely on."""
    t = _T.get(language, _T["en"])
    ev = {"E1": t["head"].format(id=facts["decision_id"], n=len(facts["recommended_actions"]), r=facts["rejected_count"])}
    ev["E2"] = t["bottleneck"].format(b=facts["critical_bottleneck"]) if facts.get("critical_bottleneck") else t["none"]
    if facts.get("expected_demand_change_pct") is not None:
        ev["E3"] = t["demand"].format(p=facts["expected_demand_change_pct"])
    if facts.get("unmet"):
        ev["E4"] = t["unmet"].format(list=", ".join(f"{k} {v}" for k, v in facts["unmet"].items()))
    ev["E5"] = t["wait"].format(m=facts["waiting_minutes_saved"]) if facts.get("waiting_minutes_saved") is not None else t["nowait"]
    ev["E6"] = t["conf"].format(c=facts["confidence"]) if facts.get("confidence") is not None else t["noconf"]
    ev["E7"] = t["approval"]
    return ev


class TemplateExplainer:
    """Deterministic wording; also the fallback whenever the LLM is absent or its answer is not supported."""

    async def explain(self, facts: Mapping[str, Any], language: str) -> Explanation:
        ev = build_evidence(facts, language)
        return Explanation(" ".join(ev[k] for k in sorted(ev)), "template", True)


Completion = Callable[[str, str], Awaitable[str]]    # (system_prompt, user_content) -> text

_SYSTEM = (
    "You rewrite a short operations summary for a hospital operations manager. "
    "You are given numbered evidence sentences in JSON. Reply with ONLY a JSON object: "
    '{"sentences":[{"text":"...","evidence":["E1","E2"]}]} with at most 4 sentences. '
    "Every sentence must cite the evidence ids it uses and may only restate them: no new facts, no numbers that are not in the cited evidence, "
    "no clinical statements, no assurances, no predictions of outcomes. Write in {lang}."
)


def parse_structured(text: str) -> list[dict] | None:
    body = text.strip()
    if body.startswith("```"):
        body = re.sub(r"^```(?:json)?\s*|\s*```$", "", body, flags=re.S).strip()
    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    items = obj.get("sentences") if isinstance(obj, dict) else None
    return items if isinstance(items, list) else None


def validate_structured(items: list[dict], evidence: Mapping[str, str], max_sentences: int = 4) -> list[str] | None:
    """The accepted sentences, or None if any sentence is unsupported, uncited or the shape is wrong."""
    if not items or len(items) > max_sentences:
        return None
    out = []
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("text"), str) or not isinstance(it.get("evidence"), list):
            return None
        ids = it["evidence"]
        if not ids or any(i not in evidence for i in ids) or len(it["text"]) > 400:
            return None
        if claim_problem(it["text"], " ".join(evidence[i] for i in ids)):
            return None
        out.append(it["text"].strip())
    return out


class LLMExplainer:
    def __init__(self, complete: Completion, fallback: TemplateExplainer | None = None) -> None:
        self._complete = complete
        self._fallback = fallback or TemplateExplainer()

    async def explain(self, facts: Mapping[str, Any], language: str) -> Explanation:
        evidence = build_evidence(facts, language)
        payload = json.dumps({"language": language, "evidence": [{"id": k, "text": v} for k, v in evidence.items()]}, ensure_ascii=False)
        try:
            text = await self._complete(_SYSTEM.replace("{lang}", "Arabic" if language == "ar" else "English"), payload)
        except Exception:
            return await self._fallback.explain(facts, language)
        items = parse_structured(text)
        sentences = validate_structured(items, evidence) if items else None
        if not sentences:
            return await self._fallback.explain(facts, language)
        return Explanation(" ".join([*sentences, evidence["E7"]]), "llm", True)
