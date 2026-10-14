"""القوائم المالية للجهات غير الربحية بالشكل الذي يصدره المحاسب القانوني (مع المقارنة بالسنة السابقة):

قائمة المركز المالي (صافي الأصول بفئاته الثلاث وأصول الأوقاف مجموعة مستقلة)، قائمة الأنشطة (غير مقيدة / مقيدة / أوقاف
وصافي الأصول أول السنة وآخرها)، وقائمة التدفقات النقدية بالطريقة غير المباشرة. تُبنى من فروع الدليل المحاسبي الموحد
للجهات غير الربحية: الحساب الفرعي يبدأ رمزه برمز أبيه، فرصيد أي بند = مجموع أرصدة الحسابات التي تبدأ برمزه.
"""
import datetime
from collections import OrderedDict

from django.db.models import Sum

from accounts.funds import CLOSING_VOUCHER, WAQF_DEPRECIATION, branch_class, row_class
from accounts.models import Account, GLEntry
from core.utils import flt

CLASSES = [("unrestricted", "غير مقيدة"), ("restricted", "مقيدة"), ("endowment", "أوقاف")]
CLASS_KEYS = [key for key, _ in CLASSES]
CASH_PREFIXES = ("111", "131")

# حسابات المخصصات ومجمعات الإهلاك التي تُطرح من أصولها (تظهر الأصول بالصافي)
CONTRAS = {"112": ["22402"], "113": ["22403"], "114": ["22404"], "117": ["22405"], "121": ["22501"],
           "122": ["22502"], "125": ["22503"], "134": ["227"]}
CONTRA_PREFIXES = sorted({c for contras in CONTRAS.values() for c in contras})
NET_LABELS = {"121": "الممتلكات والآلات والمعدات - بالصافي", "122": "صافي الأصول غير الملموسة",
              "134": "الأصول الثابتة الوقفية - بالصافي", "125": "الموجودات الحيوية - بالصافي"}


def previous_period(start, end):
    """السنة السابقة للمقارنة: نفس الطول وتنتهي قبل بداية السنة الحالية بيوم."""
    prev_end = start - datetime.timedelta(days=1)
    try:
        prev_start = start.replace(year=start.year - 1)
    except ValueError:  # 29 فبراير
        prev_start = start.replace(year=start.year - 1, day=28)
    return prev_start, prev_end


_OFFICIAL = {}


def official_names():
    if not _OFFICIAL:
        import csv

        from accounts.nonprofit_chart import DATA_FILE, SUPPLEMENTARY

        with DATA_FILE.open(encoding="utf-8") as f:
            _OFFICIAL.update({r["code"]: r["name"] for r in csv.DictReader(f)})
        _OFFICIAL.update({code: name for code, name, _ in SUPPLEMENTARY})
    return _OFFICIAL


class Ledger:
    """أرصدة وحركات الحسابات بأرقامها، مجمعة على البادئات (الفروع)."""

    def __init__(self):
        self.accounts = {a.id: a for a in Account.objects.filter(is_group=False)}

    def _rows(self, **filters):
        return GLEntry.objects.filter(is_cancelled=False, **filters)

    def balances(self, date):
        """{رقم الحساب: مدين − دائن} حتى التاريخ."""
        result = {}
        for row in self._rows(posting_date__lte=date).values("account_id").annotate(d=Sum("debit"), c=Sum("credit")):
            account = self.accounts.get(row["account_id"])
            if account is not None and account.number:
                result[account.number] = flt(row["d"]) - flt(row["c"])
        return result

    def movements(self, start, end, include_closing=True):
        """{رقم الحساب: دائن − مدين} خلال الفترة (موجب = مصدر نقد / إيراد / زيادة التزام)."""
        qs = self._rows(posting_date__gte=start, posting_date__lte=end)
        if not include_closing:
            qs = qs.exclude(voucher_type=CLOSING_VOUCHER)
        result = {}
        for row in qs.values("account_id").annotate(d=Sum("debit"), c=Sum("credit")):
            account = self.accounts.get(row["account_id"])
            if account is not None and account.number:
                result[account.number] = flt(row["c"]) - flt(row["d"])
        return result

    def class_amounts(self, start, end, include_closing=False, roots=("income", "expense")):
        """{(رقم الحساب، الفئة): دائن − مدين} للإيرادات والمصروفات خلال الفترة موزعة على فئات صافي الأصول."""
        qs = self._rows(posting_date__gte=start, posting_date__lte=end, account__root_type__in=roots)
        if not include_closing:
            qs = qs.exclude(voucher_type=CLOSING_VOUCHER)
        result = {}
        for row in qs.values("account__number", "fund__fund_type").annotate(d=Sum("debit"), c=Sum("credit")):
            key = (row["account__number"], row_class(row["account__number"], row["fund__fund_type"]))
            result[key] = result.get(key, flt(0)) + flt(row["c"]) - flt(row["d"])
        return result

    def name(self, prefix):
        """الاسم الرسمي في الدليل الموحد (دون تمييز الأسماء المكررة)، وإلا اسم الحساب في الشركة."""
        official = official_names().get(prefix)
        if official:
            return official
        account = Account.objects.filter(number=prefix).first()
        return account.name if account else prefix


def total(values, prefixes, exclude=()):
    """مجموع قيم الحسابات التي تبدأ بإحدى البادئات وليست ضمن المستثنى."""
    prefixes = (prefixes,) if isinstance(prefixes, str) else tuple(prefixes)
    exclude = tuple(exclude)
    return sum((v for number, v in values.items()
                if number.startswith(prefixes) and not (exclude and number.startswith(exclude))), flt(0))


def net_assets_by_class(ledger, date):
    """صافي الأصول لكل فئة في تاريخ: حسابات صافي الأصول + نتائج الأنشطة (بما فيها قيود الإقفال)."""
    result = OrderedDict((key, flt(0)) for key in CLASS_KEYS)
    for row in (GLEntry.objects.filter(is_cancelled=False, posting_date__lte=date,
                                       account__root_type__in=["equity", "income", "expense"])
                .values("account__number", "account__root_type", "fund__fund_type")
                .annotate(d=Sum("debit"), c=Sum("credit"))):
        number = row["account__number"] or ""
        cls = branch_class(number) if row["account__root_type"] == "equity" else row_class(number,
                                                                                            row["fund__fund_type"])
        result[cls] += flt(row["c"]) - flt(row["d"])
    return result


# ───────────────────────────── قائمة المركز المالي ─────────────────────────────

def _lines(ledger, now, before, parents, debit_nature, skip=()):
    """بنود المجموعات المباشرة تحت كل أب (مستوى الرمز الأطول بخانة)، بالصافي بعد طرح المخصصات المرتبطة."""
    lines = []
    for parent in parents:
        children = (Account.objects.filter(number__startswith=parent, is_group=True).exclude(number=parent)
                    .order_by("number"))
        direct = [c for c in children if len(c.number) == len(parent) + 1] or \
            [c for c in Account.objects.filter(parent__number=parent).order_by("number")]
        for child in direct:
            code = child.number
            if code.startswith(tuple(skip)):
                continue
            contras = CONTRAS.get(code, [])
            sign = 1 if debit_nature else -1
            value = sign * (total(now, code) + sum((total(now, c) for c in contras), flt(0)))
            prior = sign * (total(before, code) + sum((total(before, c) for c in contras), flt(0)))
            if value or prior:
                lines.append({"code": code, "label": NET_LABELS.get(code, ledger.name(code)), "value": value,
                              "prior": prior, "contras": contras})
    return lines


def financial_position(ledger, end, prior_end):
    now, before = ledger.balances(end), ledger.balances(prior_end)
    sections = [
        ("الموجودات المتداولة", _lines(ledger, now, before, ["11"], True)),
        ("الموجودات غير المتداولة", _lines(ledger, now, before, ["12"], True)),
        ("أصول الأوقاف", _lines(ledger, now, before, ["13"], True)),
    ]
    assets = [{"title": t, "lines": ls, "value": sum((l["value"] for l in ls), flt(0)),
               "prior": sum((l["prior"] for l in ls), flt(0))} for t, ls in sections if ls]
    liability_sections = [
        ("الالتزامات المتداولة", _lines(ledger, now, before, ["21"], False)),
        ("الالتزامات غير المتداولة", _lines(ledger, now, before, ["22"], False, skip=["225", "227"])),
    ]
    # المخصصات التي تُطرح من الأصول لا تُعد التزامات
    for _, lines in liability_sections:
        for line in lines:
            if line["code"] == "224":
                line["value"] = -(total(now, "224", exclude=CONTRA_PREFIXES))
                line["prior"] = -(total(before, "224", exclude=CONTRA_PREFIXES))
                line["label"] = "المخصصات (مكافأة نهاية الخدمة وغيرها)"
    liabilities = [{"title": t, "lines": [l for l in ls if l["value"] or l["prior"]],
                    "value": sum((l["value"] for l in ls), flt(0)), "prior": sum((l["prior"] for l in ls), flt(0))}
                   for t, ls in liability_sections if ls]
    net_now, net_before = net_assets_by_class(ledger, end), net_assets_by_class(ledger, prior_end)
    labels = {"unrestricted": "صافي الأصول غير المقيدة", "restricted": "صافي الأصول المقيدة",
              "endowment": "صافي أصول الأوقاف"}
    net_assets = [{"key": key, "label": labels[key], "value": net_now[key], "prior": net_before[key]}
                  for key in CLASS_KEYS]
    total_assets = sum((s["value"] for s in assets), flt(0))
    total_assets_prior = sum((s["prior"] for s in assets), flt(0))
    total_liabilities = sum((s["value"] for s in liabilities), flt(0))
    total_liabilities_prior = sum((s["prior"] for s in liabilities), flt(0))
    total_net = sum(net_now.values(), flt(0))
    total_net_prior = sum(net_before.values(), flt(0))
    return {
        "assets": assets, "liabilities": liabilities, "net_assets": net_assets,
        "total_assets": total_assets, "total_assets_prior": total_assets_prior,
        "total_liabilities": total_liabilities, "total_liabilities_prior": total_liabilities_prior,
        "total_net": total_net, "total_net_prior": total_net_prior,
        "total_liabilities_and_net": total_liabilities + total_net,
        "total_liabilities_and_net_prior": total_liabilities_prior + total_net_prior,
        "balanced": total_assets == total_liabilities + total_net
        and total_assets_prior == total_liabilities_prior + total_net_prior,
    }


# ───────────────────────────── قائمة الأنشطة ─────────────────────────────

TRANSFER_PREFIXES = ("314", "47")


# عرض المصروفات حسب وظيفتها (معيار العرض والإفصاح): البرامج والأنشطة، ثم الأوقاف، ثم الأنشطة المساندة
FUNCTION_ROWS = {"44": "مصروفات جمع الأموال", "45": "مصروفات الاستثمار", "46": "مصروفات الحوكمة"}
EXPENSE_ORDER = ["42", "43", "44", "41", "46", "45", "413"]


def activity_group(number):
    """بند قائمة الأنشطة الذي يُجمع فيه الحساب: المجموعة ذات الخمس خانات للإيرادات والبرامج والأوقاف،
    وكل وظيفة مساندة (العمومية والإدارية، جمع الأموال، الحوكمة، الاستثمار) بند واحد، والإهلاك بند مستقل."""
    if number.startswith(TRANSFER_PREFIXES):
        return "transfers", "التحويلات وإعادة التصنيف بين فئات صافي الأصول"
    if number.startswith("413"):
        return "413", "مصاريف الإهلاك والاستنفاذ"
    if number.startswith("41"):
        return "41", "المصروفات العمومية والإدارية"
    if number[:2] in FUNCTION_ROWS:
        return number[:2], FUNCTION_ROWS[number[:2]]
    return number[:5], None


def _expense_order(code):
    for index, prefix in enumerate(EXPENSE_ORDER[::-1]):
        if code.startswith(prefix):
            return (len(EXPENSE_ORDER) - 1 - index, code)
    return (len(EXPENSE_ORDER), code)


def statement_of_activities(ledger, start, end, prior_start, prior_end):
    current = ledger.class_amounts(start, end)
    prior = ledger.class_amounts(prior_start, prior_end)
    groups = OrderedDict()
    for (number, cls), amount in list(current.items()) + [(k, None) for k in prior]:
        code, label = activity_group(number)
        root = "transfer" if code == "transfers" else "income" if number.startswith("3") else "expense"
        group = groups.setdefault((root, code), {"code": code, "label": label or ledger.name(code),
                                                 "values": OrderedDict((k, flt(0)) for k in CLASS_KEYS),
                                                 "prior": flt(0)})
        if amount is not None:
            group["values"][cls] += -amount if root == "expense" else amount
    for (number, cls), amount in prior.items():
        code, _ = activity_group(number)
        root = "transfer" if code == "transfers" else "income" if number.startswith("3") else "expense"
        groups[(root, code)]["prior"] += -amount if root == "expense" else amount
    sections = []
    for root, title in (("income", "الإيرادات والمكاسب"), ("expense", "المصروفات والخسائر")):
        order = _expense_order if root == "expense" else (lambda code: code)
        lines = [g for (r, _), g in sorted(groups.items(), key=lambda kv: order(kv[0][1])) if r == root
                 and (any(g["values"].values()) or g["prior"])]
        for line in lines:
            line["total"] = sum(line["values"].values(), flt(0))
        totals = OrderedDict((k, sum((l["values"][k] for l in lines), flt(0))) for k in CLASS_KEYS)
        sections.append({"title": title, "lines": lines, "totals": totals,
                         "total": sum(totals.values(), flt(0)), "prior": sum((l["prior"] for l in lines), flt(0))})
    income, expense = sections
    transfers = []
    transfer = groups.get(("transfer", "transfers"))
    if transfer is not None:
        transfer["total"] = sum(transfer["values"].values(), flt(0))
        if any(transfer["values"].values()):
            transfers.append(transfer)
    # استهلاك أصول الأوقاف يُعرض في المصروفات غير المقيدة، ويُعاد تصنيف مبلغ مماثل من صافي أصول الأوقاف
    depreciation = groups.get(("expense", WAQF_DEPRECIATION))
    if depreciation is not None and depreciation["values"]["endowment"]:
        amount = depreciation["values"]["endowment"]
        depreciation["values"]["unrestricted"] += amount
        depreciation["values"]["endowment"] = flt(0)
        expense["totals"]["unrestricted"] += amount
        expense["totals"]["endowment"] -= amount
        values = OrderedDict((k, flt(0)) for k in CLASS_KEYS)
        values["unrestricted"], values["endowment"] = amount, -amount
        transfers.append({"label": "إعادة تصنيف: استهلاك أصول الأوقاف من صافي أصول الأوقاف", "values": values,
                          "total": flt(0), "prior": flt(0)})
    change = OrderedDict((k, income["totals"][k] - expense["totals"][k]
                          + sum((t["values"][k] for t in transfers), flt(0))) for k in CLASS_KEYS)
    change_prior = income["prior"] - expense["prior"] + sum((t["prior"] for t in transfers), flt(0))
    # قيود مباشرة على حسابات صافي الأصول خلال السنة (غير الإقفال)، مثل الأرصدة الافتتاحية
    direct, direct_prior = OrderedDict((k, flt(0)) for k in CLASS_KEYS), flt(0)
    for period, target in (((start, end), "now"), ((prior_start, prior_end), "prior")):
        for (number, cls), amount in ledger.class_amounts(*period, roots=("equity",)).items():
            cls = branch_class(number)
            if target == "now":
                direct[cls] += amount
            else:
                direct_prior += amount
    opening = net_assets_by_class(ledger, start - datetime.timedelta(days=1))
    opening_prior = sum(net_assets_by_class(ledger, prior_start - datetime.timedelta(days=1)).values(), flt(0))
    closing = OrderedDict((k, opening[k] + change[k] + direct[k]) for k in CLASS_KEYS)
    return {
        "columns": [label for _, label in CLASSES], "sections": sections, "transfers": transfers,
        "change": change, "change_total": sum(change.values(), flt(0)), "change_prior": change_prior,
        "direct": direct, "direct_total": sum(direct.values(), flt(0)), "direct_prior": direct_prior,
        "opening": opening, "opening_total": sum(opening.values(), flt(0)), "opening_prior": opening_prior,
        "closing": closing, "closing_total": sum(closing.values(), flt(0)),
        "closing_prior": opening_prior + change_prior + direct_prior,
    }


# ───────────────────────────── قائمة التغيرات في صافي الأصول ─────────────────────────────

def _changes_block(activities, label):
    def row(title, values, kind=""):
        values = OrderedDict((k, values[k]) for k in CLASS_KEYS)
        return {"label": title, "values": values, "total": sum(values.values(), flt(0)), "kind": kind}

    income, expense = activities["sections"]
    rows = [row("الرصيد في بداية السنة", activities["opening"], "subtotal"),
            row("الإيرادات والمكاسب", income["totals"]),
            row("المصروفات والخسائر", OrderedDict((k, -v) for k, v in expense["totals"].items()))]
    for transfer in activities["transfers"]:
        rows.append(row(transfer["label"], transfer["values"]))
    rows.append(row("التغير في صافي الأصول خلال السنة", activities["change"], "subtotal"))
    if any(activities["direct"].values()):
        rows.append(row("قيود مباشرة على صافي الأصول (أرصدة افتتاحية وتسويات)", activities["direct"]))
    rows.append(row("الرصيد في نهاية السنة", activities["closing"], "grand"))
    return {"label": label, "rows": rows}


def changes_in_net_assets(ledger, activities, start, end, prior_start, prior_end):
    """قائمة التغيرات في صافي الأصول لكل فئة (غير مقيدة، مقيدة، أوقاف) للسنة الحالية والسابقة."""
    before_start, before_end = previous_period(prior_start, prior_end)
    prior = statement_of_activities(ledger, prior_start, prior_end, before_start, before_end)
    return {"columns": [label for _, label in CLASSES],
            "blocks": [_changes_block(prior, f"السنة المنتهية في {prior_end:%Y/%m/%d}"),
                       _changes_block(activities, f"السنة المنتهية في {end:%Y/%m/%d}")]}


# ───────────────────────────── قائمة التدفقات النقدية (غير مباشرة) ─────────────────────────────

OPERATING_ASSETS = ("112", "114", "115", "116", "117", "118", "132")
OPERATING_LIABILITIES = ("212", "213", "214", "215", "216")
INVESTING = ("113", "121", "122", "123", "124", "125", "133", "134", "135", "136", "137")
FINANCING = ("211", "221", "222", "223")
DEPRECIATION = ("22501", "22502", "22503", "227")
PROVISIONS = ("224", "226")


def _flow_lines(ledger, moves, prefixes, label_prefix=""):
    lines = []
    for prefix in prefixes:
        value = total(moves, prefix)
        if value:
            lines.append({"label": f"{label_prefix}{ledger.name(prefix)}", "value": value})
    return lines


def cash_flows(ledger, start, end):
    moves = ledger.movements(start, end)
    # التغير في صافي الأصول = نتيجة الأنشطة + القيود المباشرة على صافي الأصول (الإقفال ينقل بينهما فلا يتكرر)
    change_in_net_assets = total(moves, ("23", "3", "4"))
    adjustments = [{"label": "الإهلاك والاستنفاذ", "value": total(moves, DEPRECIATION)},
                   {"label": "المخصصات (مكافأة نهاية الخدمة وغيرها)", "value": total(moves, PROVISIONS)}]
    adjustments = [a for a in adjustments if a["value"]]
    working = (_flow_lines(ledger, moves, OPERATING_ASSETS, "التغير في ") +
               _flow_lines(ledger, moves, OPERATING_LIABILITIES, "التغير في "))
    operating = change_in_net_assets + sum((l["value"] for l in adjustments + working), flt(0))
    investing_lines = _flow_lines(ledger, moves, INVESTING)
    financing_lines = _flow_lines(ledger, moves, FINANCING)
    investing = sum((l["value"] for l in investing_lines), flt(0))
    financing = sum((l["value"] for l in financing_lines), flt(0))
    net = operating + investing + financing
    opening = total(ledger.balances(start - datetime.timedelta(days=1)), CASH_PREFIXES)
    closing = total(ledger.balances(end), CASH_PREFIXES)
    return {"change_in_net_assets": change_in_net_assets, "adjustments": adjustments, "working": working,
            "operating": operating, "investing_lines": investing_lines, "investing": investing,
            "financing_lines": financing_lines, "financing": financing, "net": net, "opening": opening,
            "closing": closing, "reconciles": opening + net == closing}


# ───────────────────────────── الإيضاحات والحزمة ─────────────────────────────

DEFAULT_POLICIES = """أسس الإعداد: أُعدت القوائم المالية وفقاً للمعايير المحاسبية للمنشآت غير الهادفة للربح الصادرة عن الهيئة السعودية للمراجعين والمحاسبين (معيار العرض والإفصاح للقوائم المالية ومعيار التبرعات)، وعلى أساس الاستحقاق ومبدأ التكلفة التاريخية، وتُعرض بالريال السعودي.
القوائم المالية: تتكون من قائمة المركز المالي، وقائمة الأنشطة، وقائمة التغيرات في صافي الأصول، وقائمة التدفقات النقدية (بالطريقة غير المباشرة)، والإيضاحات المتممة لها.
فئات صافي الأصول: تُصنف صافي الأصول إلى غير مقيدة، ومقيدة بقيود من المتبرعين على الغرض أو الوقت، وأوقاف. وعند انتهاء القيد بتحقق الغرض أو مضي الوقت يُعاد تصنيف المبلغ إلى صافي الأصول غير المقيدة، وتُعرض التحويلات بين الفئات بقيمها الإجمالية.
استخدام التقديرات: يتطلب إعداد القوائم المالية استخدام تقديرات وافتراضات تؤثر على مبالغ الأصول والالتزامات والإيرادات والمصروفات، وقد تختلف النتائج الفعلية عنها.
التبرعات والمنح: تُثبت إيراداً عند تلقيها أو عند الحصول على وعد غير مشروط بها يمكن قياسه بدرجة معقولة من الثقة، وتُصنف حسب قيود المتبرع. ولا تُثبت الوعود المشروطة حتى يتحقق شرطها، ويُفصح عنها.
الزكاة: تُثبت ضمن التبرعات المقيدة، ولا تُصرف إلا في مصارفها الشرعية من صندوق مستقل.
الأوقاف: يُصنف أصل الوقف (التبرعات لإنشاء الأوقاف أو شرائها ومكاسب وخسائر أصولها) ضمن صافي أصول الأوقاف، وتُصنف غلته (الريع وأرباح الاستثمار) غير مقيدة ما لم يشترط الواقف صرفها في مصارف محددة فتُصنف مقيدة حتى صرفها، ويُثبت استهلاك أصول الأوقاف ضمن المصروفات غير المقيدة مع إعادة تصنيف مبلغ مماثل من صافي أصول الأوقاف إلى صافي الأصول غير المقيدة.
التبرعات العينية: تُثبت بقيمتها العادلة في تاريخ استلامها، وإذا تعذر قياسها بموثوقية يُفصح عنها دون إثبات.
الخدمات التطوعية: تُثبت بقيمتها العادلة إيراداً ومصروفاً متساويين متى أنشأت أو حسّنت أصولاً غير مالية أو تطلبت مهارات متخصصة كانت الجمعية ستشتريها لو لم يُتبرع بها.
المصروفات: تُثبت وفق مبدأ الاستحقاق وتُعرض في قائمة الأنشطة حسب وظيفتها (البرامج والأنشطة، والأوقاف، وجمع الأموال، والعمومية والإدارية، والحوكمة)، ويُفصح عنها حسب طبيعتها، وتُحمل مصروفات الأموال المقيدة على الصناديق المقيدة الخاصة بها.
الأصول الثابتة: تُثبت بالتكلفة مطروحاً منها مجمع الإهلاك، ويُحسب الإهلاك بطريقة القسط الثابت على العمر الإنتاجي المقدر، وتُثبت الأصول المتبرع بها بقيمتها العادلة في تاريخ التبرع.
مكافأة نهاية الخدمة: يُكوّن لها مخصص وفق نظام العمل في المملكة العربية السعودية.
النقد وما في حكمه: يشمل النقد في الصناديق والحسابات الجارية لدى البنوك والنقدية الموقوفة.
سياسة قبول التبرعات: تقبل الجمعية التبرعات النقدية والعينية والزكاة والهبات والمنح والوصايا والأوقاف بما يتوافق مع أهدافها ونظامها الأساسي والأنظمة ذات العلاقة، وتلتزم بشروط المتبرعين والواقفين في صرفها."""


def _breakdown(ledger, now, before, code, contras, debit_nature):
    """تفصيل بند بحساباته الفرعية للسنتين."""
    sign = 1 if debit_nature else -1
    rows = []
    for prefix in [code] + list(contras):
        for account in Account.objects.filter(number__startswith=prefix, is_group=False).order_by("number"):
            value, prior = sign * now.get(account.number, flt(0)), sign * before.get(account.number, flt(0))
            if value or prior:
                rows.append({"label": ledger.name(account.number), "value": value, "prior": prior})
    return rows


def fixed_assets_schedule(ledger, start, end, prior_end):
    """جدول حركة الأصول الثابتة لكل فئة أصول: التكلفة والإهلاك أول السنة وحركتها وآخرها والصافي."""
    from assets.models import AssetCategory

    now, before = ledger.balances(end), ledger.balances(prior_end)
    rows = []
    for category in AssetCategory.objects.select_related("asset_account", "accumulated_depreciation_account"):
        cost, dep = category.asset_account.number, category.accumulated_depreciation_account.number
        moves = GLEntry.objects.filter(is_cancelled=False, posting_date__gte=start, posting_date__lte=end)
        cost_moves = moves.filter(account__number=cost).aggregate(d=Sum("debit"), c=Sum("credit"))
        dep_moves = moves.filter(account__number=dep).aggregate(d=Sum("debit"), c=Sum("credit"))
        row = {"label": category.name,
               "rate": round(flt(1200) / category.useful_life_months, 2) if category.useful_life_months else None,
               "cost_open": before.get(cost, flt(0)), "additions": flt(cost_moves["d"]),
               "disposals": flt(cost_moves["c"]), "cost_close": now.get(cost, flt(0)),
               "dep_open": -before.get(dep, flt(0)), "dep_charge": flt(dep_moves["c"]),
               "dep_disposals": flt(dep_moves["d"]), "dep_close": -now.get(dep, flt(0))}
        row["nbv"], row["nbv_prior"] = row["cost_close"] - row["dep_close"], row["cost_open"] - row["dep_open"]
        if any(row[k] for k in ("cost_open", "cost_close", "dep_close")):
            rows.append(row)
    keys = ("cost_open", "additions", "disposals", "cost_close", "dep_open", "dep_charge", "dep_disposals",
            "dep_close", "nbv", "nbv_prior")
    return {"rows": rows, "totals": {k: sum((r[k] for r in rows), flt(0)) for k in keys}}


def provision_movement(ledger, number, start, end, prior_end):
    moves = GLEntry.objects.filter(is_cancelled=False, account__number=number, posting_date__gte=start,
                                   posting_date__lte=end).aggregate(d=Sum("debit"), c=Sum("credit"))
    opening = -ledger.balances(prior_end).get(number, flt(0))
    return {"opening": opening, "additions": flt(moves["c"]), "used": flt(moves["d"]),
            "closing": opening + flt(moves["c"]) - flt(moves["d"])}


def by_purpose(start, end, prior_start, prior_end, root, cls, prefixes):
    """الإيرادات أو المصروفات لفئة صافي أصول حسب الغرض (الصندوق، وإلا الحساب) للسنتين."""
    result = OrderedDict()
    for period, key in (((start, end), "value"), ((prior_start, prior_end), "prior")):
        rows = (GLEntry.objects.filter(is_cancelled=False, posting_date__gte=period[0], posting_date__lte=period[1],
                                       account__root_type=root, account__number__startswith=prefixes)
                .exclude(voucher_type=CLOSING_VOUCHER).exclude(account__number__startswith="314")
                .exclude(account__number__startswith="47")  # التحويلات بين الفئات ليست وارداً ولا صرفاً لغرض
                .values("fund__name", "fund__fund_type", "account__number").annotate(d=Sum("debit"), c=Sum("credit")))
        for row in rows:
            if row_class(row["account__number"], row["fund__fund_type"]) != cls:
                continue
            label = row["fund__name"] or official_names().get(row["account__number"]) or row["account__number"]
            amount = flt(row["c"]) - flt(row["d"]) if root == "income" else flt(row["d"]) - flt(row["c"])
            entry = result.setdefault(label, {"label": label, "value": flt(0), "prior": flt(0)})
            entry[key] += amount
    return [r for r in result.values() if r["value"] or r["prior"]]


def accounts_detail(ledger, start, end, prior_start, prior_end, prefixes, exclude=()):
    """تفصيل مصروفات فرع بحساباته للسنتين (مثل المصروفات العمومية والإدارية)."""
    now = ledger.movements(start, end, include_closing=False)
    before = ledger.movements(prior_start, prior_end, include_closing=False)
    rows = []
    for account in Account.objects.filter(number__startswith=prefixes, is_group=False).order_by("number"):
        if exclude and account.number.startswith(exclude):
            continue
        value, prior = -now.get(account.number, flt(0)), -before.get(account.number, flt(0))
        if value or prior:
            rows.append({"label": ledger.name(account.number), "value": value, "prior": prior})
    return rows


FUNCTIONS = [("42", "البرامج والأنشطة"), ("43", "الأوقاف"), ("44", "جمع الأموال"), ("41", "العمومية والإدارية"),
             ("46", "الحوكمة"), ("45", "الاستثمار")]
NATURES = [
    ("salaries", "الرواتب والأجور", ("الرواتب", "الأجور", "تكاليف العاملين")),
    ("benefits", "المزايا والحوافز", ("مزايا", "حوافز", "بدل", "تأمينات")),
    ("volunteer", "الخدمات التطوعية", ("تطوعية",)),
    ("aid", "المساعدات والبرامج المباشرة", ("مصارف الزكاة", "برامج", "توزيعات", "المنح", "مستفيدي")),
    ("supplies", "المستهلكات", ("المستهلكات",)),
    ("maintenance", "الصيانة والإصلاح", ("الصيانة",)),
    ("utilities", "المنافع والخدمات والتأمين", ("المنافع",)),
    ("depreciation", "الإهلاك والاستنفاذ", ("الاستهلاك", "الإستهلاك", "إهلاك", "الاستنفاذ")),
    ("other", "مصروفات أخرى", ()),
]


def expense_nature(number, name):
    """طبيعة المصروف من اسم مجموعته في الدليل الموحد (الرواتب، المستهلكات، الإهلاك...)."""
    if number.startswith(("413", "43103")):
        return "depreciation"
    for key, _, words in NATURES:
        if any(word in name for word in words):
            return key
    return "other"


def expenses_by_nature(ledger, start, end):
    """إيضاح المصروفات حسب طبيعتها ووظيفتها: صف لكل طبيعة وعمود لكل وظيفة."""
    names = official_names()
    moves = ledger.movements(start, end, include_closing=False)
    columns = [(prefix, label) for prefix, label in FUNCTIONS]
    cells = {}
    for number, amount in moves.items():
        function = next((prefix for prefix, _ in columns if number.startswith(prefix)), None)
        if function is None or not amount:
            continue
        group = names.get(number[:5]) or names.get(number[:3]) or ""
        nature = expense_nature(number, f"{group} {names.get(number, '')}")
        cells[(nature, function)] = cells.get((nature, function), flt(0)) - amount
    used = [prefix for prefix, _ in columns if any(cells.get((n, prefix)) for n, _, _ in NATURES)]
    rows = []
    for key, label, _ in NATURES:
        values = [cells.get((key, prefix), flt(0)) for prefix in used]
        if any(values):
            rows.append({"label": label, "values": values, "total": sum(values, flt(0))})
    totals = [sum((r["values"][i] for r in rows), flt(0)) for i in range(len(used))]
    return {"columns": [label for prefix, label in columns if prefix in used], "rows": rows, "totals": totals,
            "total": sum(totals, flt(0))}


def conditional_pledges(end):
    """الوعود المشروطة القائمة في نهاية السنة (غير مثبتة، يُفصح عنها)."""
    from funds.models import Pledge

    pledges = (Pledge.objects.filter(docstatus=1, is_conditional=True, posting_date__lte=end)
               .exclude(condition_met_on__lte=end).select_related("donor", "fund").order_by("posting_date"))
    return [{"donor": str(p.donor), "condition": p.condition, "fund": str(p.fund or "غير مقيد"),
             "amount": flt(p.amount), "date": p.posting_date} for p in pledges]


def awqaf_note(start, end):
    """الإفصاح الإضافي عن الأوقاف: لكل وقف قيمته وغلته وتوزيعاتها خلال السنة ورصيد غلته في نهايتها."""
    from awqaf.models import Waqf, WaqfDistribution

    rows = []
    for waqf in Waqf.objects.select_related("revenue_fund").order_by("name"):
        fund = waqf.revenue_fund
        income = (GLEntry.objects.filter(fund=fund, is_cancelled=False, account__root_type="income",
                                         posting_date__gte=start, posting_date__lte=end)
                  .exclude(voucher_type=CLOSING_VOUCHER).aggregate(c=Sum("credit"), d=Sum("debit")))
        distributed = WaqfDistribution.objects.filter(waqf=waqf, docstatus=1, posting_date__gte=start,
                                                      posting_date__lte=end).aggregate(t=Sum("amount"))["t"]
        rows.append({"name": waqf.name, "type": waqf.get_waqf_type_display(), "deed": waqf.deed_number,
                     "classification": waqf.get_revenue_restriction_display().split(" (")[0],
                     "asset_value": flt(waqf.asset_value), "revenue": flt(income["c"]) - flt(income["d"]),
                     "distributed": flt(distributed), "balance": fund.totals(to_date=end)["balance"] if fund else 0})
    return rows


def restricted_funds_note(end, prior_end):
    from accounts.models import Fund, FundType

    rows = []
    for fund in Fund.objects.filter(fund_type=FundType.RESTRICTED):
        value, prior = fund.totals(to_date=end)["balance"], fund.totals(to_date=prior_end)["balance"]
        if value or prior:
            rows.append({"label": fund.name, "value": value, "prior": prior})
    return rows


def build_package(company, fiscal_year):
    """حزمة القوائم المالية لسنة مالية: القوائم الثلاث مع المقارنة، والإيضاحات مرقمة ومربوطة ببنودها."""
    start, end = fiscal_year.start_date, fiscal_year.end_date
    prior_start, prior_end = previous_period(start, end)
    ledger = Ledger()
    position = financial_position(ledger, end, prior_end)
    activities = statement_of_activities(ledger, start, end, prior_start, prior_end)
    flows, flows_prior = cash_flows(ledger, start, end), cash_flows(ledger, prior_start, prior_end)
    changes = changes_in_net_assets(ledger, activities, start, end, prior_start, prior_end)
    now, before = ledger.balances(end), ledger.balances(prior_end)

    notes = [
        {"number": 1, "title": "معلومات عن الجمعية", "kind": "info"},
        {"number": 2, "title": "أسس الإعداد وأهم السياسات المحاسبية", "kind": "text",
         "text": company.fs_policies or DEFAULT_POLICIES},
    ]

    def add(title, kind, **data):
        notes.append({"number": len(notes) + 1, "title": title, "kind": kind, **data})
        return len(notes)

    for section in position["assets"] + position["liabilities"]:
        debit = section in position["assets"]
        for line in section["lines"]:
            if line["code"] == "121":
                line["note"] = add(line["label"], "fixed_assets",
                                   schedule=fixed_assets_schedule(ledger, start, end, prior_end),
                                   rows=_breakdown(ledger, now, before, line["code"], line["contras"], debit))
            elif line["code"] == "224":
                line["note"] = add(line["label"], "provision",
                                   movement=provision_movement(ledger, "22401001", start, end, prior_end),
                                   rows=_breakdown(ledger, now, before, "224", [], False))
            else:
                rows = _breakdown(ledger, now, before, line["code"], line["contras"], debit)
                if rows:
                    line["note"] = add(line["label"], "table", rows=rows)
    purpose_notes = [
        ("311", "التبرعات والإيرادات المقيدة حسب الغرض", "income", "restricted", "3"),
        ("421", "مصروفات البرامج والأنشطة المقيدة حسب الغرض", "expense", "restricted", "4"),
    ]
    for prefix, title, root, cls, branch in purpose_notes:
        rows = by_purpose(start, end, prior_start, prior_end, root, cls, branch)
        if rows:
            number = add(title, "table", rows=rows)
            for section in activities["sections"]:
                for line in section["lines"]:
                    if line["code"].startswith(prefix):
                        line["note"] = number
    for code_prefix, title, prefixes, exclude in (
            ("422", "مصروفات البرامج والأنشطة غير المقيدة", "422", ()),
            ("41", "المصروفات العمومية والإدارية", "41", ("413",))):
        rows = accounts_detail(ledger, start, end, prior_start, prior_end, prefixes, exclude)
        if rows:
            number = add(title, "table", rows=rows)
            for line in activities["sections"][1]["lines"]:
                if line["code"].startswith(code_prefix) and line["code"] != "413":
                    line["note"] = number
    waqf_rows = awqaf_note(start, end)
    if waqf_rows:
        add("الأوقاف", "awqaf", awqaf=waqf_rows)
    pledges = conditional_pledges(end)
    if pledges:
        add("وعود التبرع المشروطة (غير مثبتة)", "pledges", pledges=pledges,
            pledges_total=sum((p["amount"] for p in pledges), flt(0)))
    matrix = expenses_by_nature(ledger, start, end)
    if matrix["rows"]:
        add("المصروفات حسب طبيعتها ووظيفتها", "matrix", matrix=matrix)
    rows = restricted_funds_note(end, prior_end)
    restricted = next(n for n in position["net_assets"] if n["key"] == "restricted")
    residual = (restricted["value"] - sum((r["value"] for r in rows), flt(0)),
                restricted["prior"] - sum((r["prior"] for r in rows), flt(0)))
    if any(residual):
        rows.append({"label": "مبالغ مقيدة مسجلة على حسابات الفروع المقيدة دون صندوق", "value": residual[0],
                     "prior": residual[1]})
    if rows:
        number = add("صافي الأصول المقيدة حسب الصندوق", "table", rows=rows)
        for item in position["net_assets"]:
            if item["key"] == "restricted":
                item["note"] = number
    for note in notes:
        if note.get("rows") is not None:
            note["total"] = sum((r["value"] for r in note["rows"]), flt(0))
            note["total_prior"] = sum((r["prior"] for r in note["rows"]), flt(0))
    return {"company": company, "year": fiscal_year, "start": start, "end": end, "prior_start": prior_start,
            "prior_end": prior_end, "position": position, "activities": activities, "changes": changes, "flows": flows,
            "flows_prior": flows_prior, "notes": notes,
            "checks": {"balanced": position["balanced"], "cash": flows["reconciles"] and flows_prior["reconciles"],
                       "net_assets": [round(activities["closing"][k], 2) for k in CLASS_KEYS]
                       == [round(n["value"], 2) for n in position["net_assets"]]}}
