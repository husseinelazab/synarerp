"""ترحيل الإهلاك واستبعاد الأصول عبر قيود يومية معتمدة (تخضع لقفل السنة المالية)."""
from collections import defaultdict

from django.core.exceptions import ValidationError
from django.db import transaction

from assets.models import Asset, AssetDepreciation, AssetStatus
from core.models import DocStatus
from core.utils import flt

ARABIC_MONTHS = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر",
                 "نوفمبر", "ديسمبر"]


def pending_rows(upto, asset=None):
    rows = AssetDepreciation.objects.filter(
        asset__company=_company(asset), asset__docstatus=DocStatus.SUBMITTED, journal_entry__isnull=True,
        schedule_date__lte=upto,
    ).exclude(asset__status=AssetStatus.DISPOSED).select_related(
        "asset", "asset__category", "asset__cost_center").order_by("schedule_date", "asset_id")
    return rows.filter(asset=asset) if asset is not None else rows


def _company(asset):
    from core.models import Company

    return asset.company if asset is not None else Company.current()


@transaction.atomic
def post_depreciation(upto, user=None, asset=None):
    """يرحّل أقساط الإهلاك المستحقة حتى التاريخ: قيد يومية واحد لكل شهر يشمل كل الأصول."""
    from accounts.models import JournalEntry, JournalEntryType

    by_date = defaultdict(list)
    for row in pending_rows(upto, asset):
        by_date[row.schedule_date].append(row)
    entries = []
    for date, rows in sorted(by_date.items()):
        je = JournalEntry.objects.create(
            posting_date=date, entry_type=JournalEntryType.DEPRECIATION, created_by=user,
            remarks=f"إهلاك الأصول الثابتة — {ARABIC_MONTHS[date.month - 1]} {date.year}")
        for row in rows:
            a = row.asset
            je.accounts.create(account=a.category.depreciation_expense_account, debit=row.amount,
                               cost_center=a.cost_center, description=f"إهلاك {a}")
            je.accounts.create(account=a.category.accumulated_depreciation_account, credit=row.amount,
                               description=f"إهلاك {a}")
        je.calculate_totals()
        je.save()
        je.submit()
        AssetDepreciation.objects.filter(pk__in=[r.pk for r in rows]).update(journal_entry=je)
        for a in {r.asset for r in rows}:
            a.refresh_status()
        entries.append(je)
    return entries


def gain_loss_account(company):
    from accounts.models import Account

    return (Account.objects.filter(name="أرباح وخسائر استبعاد الأصول", is_group=False).first()
            or company.require_account("default_income_account"))


@transaction.atomic
def dispose(asset, date, sale_amount, receive_account=None, customer=None, vat=False, user=None):
    """بيع أو استبعاد أصل: يرحّل الإهلاك حتى تاريخ الاستبعاد ثم يقفل الأصل ومجمعه ويثبت الربح أو الخسارة."""
    from accounts.models import AccountType, JournalEntry, JournalEntryType

    if not asset.is_submitted or asset.status == AssetStatus.DISPOSED:
        raise ValidationError("الأصل غير معتمد أو مستبعد مسبقاً.")
    if date < asset.purchase_date:
        raise ValidationError("تاريخ الاستبعاد يسبق تاريخ الشراء.")
    sale_amount = flt(sale_amount or 0)
    if sale_amount < 0:
        raise ValidationError("مبلغ البيع لا يكون سالباً.")
    if sale_amount and receive_account is None:
        raise ValidationError("حدد الحساب الذي استُلم فيه ثمن البيع.")
    if receive_account is not None and receive_account.account_type == AccountType.RECEIVABLE and customer is None:
        raise ValidationError("حساب العملاء يتطلب تحديد العميل.")
    post_depreciation(date, user=user, asset=asset)
    asset.refresh_from_db()
    company = asset.company
    category = asset.category
    tax = flt(sale_amount * company.default_tax_rate / 100) if vat and sale_amount else flt(0)
    gain = flt(sale_amount - asset.book_value)
    je = JournalEntry.objects.create(posting_date=date, entry_type=JournalEntryType.ADJUSTMENT, created_by=user,
                                     remarks=f"{'بيع' if sale_amount else 'استبعاد'} الأصل {asset}")
    lines = [
        (category.accumulated_depreciation_account, asset.accumulated_depreciation, 0, None, "إقفال مجمع الإهلاك"),
        (category.asset_account, 0, asset.gross_amount, None, "إقفال تكلفة الأصل"),
    ]
    if sale_amount:
        lines.append((receive_account, sale_amount + tax, 0, customer, "ثمن البيع"))
    if tax:
        lines.append((company.require_account("default_output_tax_account"), 0, tax, None, "ضريبة القيمة المضافة"))
    if gain > 0:
        lines.append((gain_loss_account(company), 0, gain, None, "ربح بيع أصل"))
    elif gain < 0:
        lines.append((gain_loss_account(company), -gain, 0, None, "خسارة استبعاد أصل"))
    for account, debit, credit, cust, text in lines:
        if debit or credit:
            je.accounts.create(account=account, debit=debit, credit=credit, customer=cust, description=text,
                               cost_center=asset.cost_center if account.root_type in ("income", "expense") else None)
    je.calculate_totals()
    je.save()
    je.submit()
    asset.status, asset.disposal_date, asset.disposal_entry = AssetStatus.DISPOSED, date, je
    asset.save(update_fields=["status", "disposal_date", "disposal_entry"])
    return je


def on_journal_cancel(je):
    """عند إلغاء قيد إهلاك أو استبعاد: إعادة الأقساط غير مرحّلة أو إعادة الأصل للخدمة."""
    assets = {row.asset for row in AssetDepreciation.objects.filter(journal_entry=je).select_related("asset")}
    if assets:
        AssetDepreciation.objects.filter(journal_entry=je).update(journal_entry=None)
    for asset in Asset.objects.filter(disposal_entry=je):
        asset.status, asset.disposal_date, asset.disposal_entry = AssetStatus.ACTIVE, None, None
        asset.save(update_fields=["status", "disposal_date", "disposal_entry"])
        assets.add(asset)
    for asset in assets:
        asset.refresh_status()


def check_journal_cancel(je):
    """لا يُلغى قيد إهلاك شهر تلاه إهلاك شهر لاحق مرحّل، ولا قيد إهلاك لأصل مُستبعد."""
    rows = AssetDepreciation.objects.filter(journal_entry=je).select_related("asset")
    for row in rows:
        if row.asset.status == AssetStatus.DISPOSED:
            raise ValidationError(f"الأصل {row.asset} مُستبعد؛ ألغِ قيد استبعاده أولاً.")
        later = AssetDepreciation.objects.filter(asset=row.asset, schedule_date__gt=row.schedule_date,
                                                 journal_entry__isnull=False).exclude(journal_entry=je).first()
        if later:
            raise ValidationError(f"رُحّل إهلاك لاحق للأصل {row.asset} في {later.journal_entry}؛ ألغِ القيود من الأحدث.")
