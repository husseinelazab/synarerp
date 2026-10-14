"""معيار حوكمة الجمعيات الأهلية (المركز الوطني لتنمية القطاع غير الربحي): تحميل القالب المُصدَّر وتقرير الجاهزية.

التحميل idempotent ومراعٍ للإصدار: إعادة تحميل الإصدار نفسه لا تغيّر تعديلات المستخدم (الأوزان والنصوص)،
والإصدار الأحدث يحدّث المؤشرات ويضيف الجديد ويوقف ما حُذف من الدليل دون حذفه (لحفظ التقييمات السابقة).
"""
import json
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import NoReverseMatch, reverse

DATA_FILE = Path(__file__).resolve().parent / "data" / "ncnp_framework.json"
SOURCE = "ncnp"


@lru_cache(maxsize=1)
def _load_file():
    with open(DATA_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def framework_data():
    return _load_file()


def version_key(version):
    """'2025.1' → (2025, 1) للمقارنة؛ الإصدار الفارغ هو الأقدم."""
    parts = []
    for piece in str(version or "").split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _dec(value):
    return Decimal(str(value or 0))


def load_framework(company, data=None):
    """ينشئ/يحدّث معايير ومؤشرات المركز للجهة. يعيد إحصاءً: created, updated, deactivated, unchanged, version."""
    from core.tenancy import tenant_context
    from governance.models import FrameworkSource, GovernanceIndicator, GovernanceStandard

    data = data or framework_data()
    version = str(data["version"])
    new_key = version_key(version)
    stats = {"created": 0, "updated": 0, "deactivated": 0, "unchanged": 0, "version": version, "skipped_newer": False}
    with tenant_context(company), transaction.atomic():
        standards = GovernanceStandard.all_objects.filter(company=company)
        indicators = GovernanceIndicator.all_objects.filter(company=company)
        loaded = [s.version for s in standards.filter(source=FrameworkSource.NCNP)]
        if loaded and max(version_key(v) for v in loaded) > new_key:
            stats["skipped_newer"] = True  # لا نُرجع الجهة إلى إصدار أقدم
            return stats
        seen = set()
        for s_data in data["standards"]:
            standard = standards.filter(code=s_data["code"]).first()
            fields = {"name": s_data["title"], "description": s_data.get("description", ""),
                      "weight_percent": _dec(s_data.get("weight_percent")),
                      "is_estimated": not s_data.get("weight_verified", False)}
            if standard is None:
                standard = GovernanceStandard.all_objects.create(company=company, code=s_data["code"],
                                                                 source=FrameworkSource.NCNP, version=version, **fields)
            elif standard.source != FrameworkSource.NCNP:
                raise ValidationError(f"الرمز {standard.code} مستخدم لمعيار خاص بالجهة؛ غيّر رمزه ثم أعد التحميل.")
            elif version_key(standard.version) < new_key:
                for key, value in fields.items():
                    setattr(standard, key, value)
                standard.version = version
                standard.save()
            for i_data in s_data["indicators"]:
                seen.add(i_data["code"])
                result = _upsert_indicator(company, indicators, standard, i_data, version, new_key)
                stats[result] += 1
        stale = indicators.filter(standard__source=FrameworkSource.NCNP, is_active=True).exclude(code__in=seen)
        stats["deactivated"] = stale.update(is_active=False)
    return stats


def _upsert_indicator(company, indicators, standard, i_data, version, new_key):
    from governance.models import FrameworkSource, GovernanceIndicator

    notes = i_data.get("note", "")
    fields = {"standard": standard, "group": i_data.get("group", "")[:150], "text": i_data["title"],
              "description": i_data.get("description", ""), "weight": _dec(i_data.get("max_points", 1)),
              "evidence_hint": i_data.get("evidence", "")[:255], "check_key": i_data.get("auto_check", "")[:40],
              "auto_target": int(i_data.get("target") or 0), "fix_link": i_data.get("fix", "")[:100],
              "is_estimated": not i_data.get("points_verified", False), "source_note": notes[:255],
              "is_active": True, "version": version}
    indicator = indicators.filter(code=i_data["code"]).first()
    if indicator is None:
        GovernanceIndicator.all_objects.create(company=company, code=i_data["code"], **fields)
        return "created"
    if indicator.standard.source != FrameworkSource.NCNP:
        raise ValidationError(f"الرمز {indicator.code} مستخدم لمؤشر خاص بالجهة؛ غيّر رمزه ثم أعد التحميل.")
    if version_key(indicator.version) < new_key or not indicator.is_active:
        for key, value in fields.items():
            setattr(indicator, key, value)
        indicator.save()
        return "updated"
    return "unchanged"


def loaded_version():
    from governance.models import FrameworkSource, GovernanceStandard

    versions = [s.version for s in GovernanceStandard.objects.filter(source=FrameworkSource.NCNP)]
    return max(versions, key=version_key) if versions else ""


# ───────────────────────── التقدير والتقرير ─────────────────────────

def rating_band(percent, bands=None):
    if percent is None:
        return ""
    bands = bands or framework_data()["rating_bands"]
    for band in sorted(bands, key=lambda b: -b["min"]):
        if Decimal(str(percent)) >= Decimal(str(band["min"])):
            return band["label"]
    return bands[-1]["label"]


def resolve_fix(spec, indicator=None):
    """'doc_list:published-document' أو 'website_settings' → رابط؛ عند التعذر رابط صفحة المؤشر لرفع الشواهد."""
    from governance.checks import CHECKS

    if not spec and indicator is not None and indicator.check_key in CHECKS:
        spec = CHECKS[indicator.check_key].fix
    if spec:
        from core.registry import REGISTRY

        name, _, arg = spec.partition(":")
        if name in ("doc_list", "doc_new") and arg not in REGISTRY:
            arg, name = "", "__invalid__"
        try:
            return reverse(name, args=[arg] if arg else [])
        except NoReverseMatch:
            pass
    return indicator.get_absolute_url() if indicator is not None else ""


def readiness(assessment):
    """بيانات تقرير الجاهزية: لكل معيار الدرجة والنسبة، والنتيجة الكلية وتقديرها، والفجوات وروابط معالجتها."""
    from django.db.models import Count, Q

    from governance.models import SCORE_FACTOR, GovernanceIndicator, ScoreStatus

    rows = list(assessment.scores.select_related("indicator__standard").order_by("indicator__standard__code",
                                                                                 "indicator__code"))
    evidence = dict(GovernanceIndicator.objects.filter(pk__in=[r.indicator_id for r in rows])
                    .annotate(n=Count("evidence", filter=Q(evidence__year=assessment.year)))
                    .values_list("pk", "n"))
    standards, gaps = {}, []
    for row in rows:
        standard = row.indicator.standard
        entry = standards.setdefault(standard.pk, {"standard": standard, "max": Decimal(0), "score": Decimal(0),
                                                   "count": 0, "met": 0, "evidence": 0, "unscored": 0})
        entry["count"] += 1
        entry["evidence"] += evidence.get(row.indicator_id, 0)
        if row.status == ScoreStatus.NA:
            continue
        entry["max"] += row.weight
        if not row.status:
            entry["unscored"] += 1
        entry["score"] += row.weight * SCORE_FACTOR.get(row.status, Decimal(0))
        if row.status == ScoreStatus.MET:
            entry["met"] += 1
        else:
            lost = row.weight * (1 - SCORE_FACTOR.get(row.status, Decimal(0)))
            gaps.append({"row": row, "indicator": row.indicator, "lost": lost,
                         "status": row.get_status_display() if row.status else "لم يُقيَّم",
                         "evidence_count": evidence.get(row.indicator_id, 0),
                         "fix_url": resolve_fix(row.indicator.fix_link, row.indicator)})
    parts = list(standards.values())
    for entry in parts:
        entry["percent"] = (entry["score"] * 100 / entry["max"]).quantize(Decimal("0.1")) if entry["max"] else None
        entry["band"] = rating_band(entry["percent"])
    weighted = all(p["standard"].weight_percent for p in parts) and parts
    if weighted:
        scored = [p for p in parts if p["percent"] is not None]
        total_weight = sum((p["standard"].weight_percent for p in scored), Decimal(0))
        overall = (sum(p["percent"] * p["standard"].weight_percent for p in scored) / total_weight
                   ).quantize(Decimal("0.1")) if total_weight else Decimal(0)
    else:
        total_max = sum((p["max"] for p in parts), Decimal(0))
        overall = (sum((p["score"] for p in parts), Decimal(0)) * 100 / total_max).quantize(Decimal("0.1")) \
            if total_max else Decimal(0)
    gaps.sort(key=lambda g: (-g["lost"], g["indicator"].code))
    return {"standards": parts, "overall": overall, "band": rating_band(overall), "weighted": bool(weighted),
            "gaps": gaps, "evidence_total": sum(evidence.values()),
            "estimated": any(p["standard"].is_estimated for p in parts) or any(r.indicator.is_estimated for r in rows)}
