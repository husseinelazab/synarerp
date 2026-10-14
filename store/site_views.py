"""صفحات المتجر الإلكتروني للتبرعات على موقع الجمعية."""
import functools
import hashlib
import hmac
import secrets
from decimal import Decimal

from django.conf import settings as django_settings
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.utils import flt
from store import moyasar
from store.forms import CheckoutForm, CodeForm, HoneypotForm, PhoneForm
from store.models import (DonationOrder, DonationOrderLine, DonorLogin, Marketer,
                          OpportunityKind, OrderStatus, RecurringDonation, StoreSettings)
from store.public import attach_raised, open_opportunities
from store.service import fetch_and_finalize, finalize, phone_variants, to_decimal
from website.site import site_url, site_view

MIN_AMOUNT = Decimal("1")
MAX_AMOUNT = Decimal("1000000")
MAX_QTY = 1000
OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_SECONDS = 60


# ─────────────────────────── أدوات الجلسة ───────────────────────────

def _key(request, name):
    return f"store_{name}_{request.site.company_id}"


def store_page(view):
    """صفحة متجر داخل موقع الجمعية: تحفظ رمز المسوّق (?ref=) في الجلسة وتحمّل إعدادات المتجر."""

    @site_view
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        code = (request.GET.get("ref") or "").strip().lower()[:40]
        if code and Marketer.objects.filter(code=code, is_active=True).exists():
            request.session[_key(request, "ref")] = code
        request.store = StoreSettings.for_company(request.site.company)
        request.store_ready = bool(request.site.fundraising_licensed and request.store.ready)
        return view(request, *args, **kwargs)

    return wrapper


def _cart(request):
    return list(request.session.get(_key(request, "cart"), []))


def _save_cart(request, cart):
    request.session[_key(request, "cart")] = cart


def _cart_lines(request):
    """أسطر السلة الصالحة: [(الفرصة، الكمية، المبلغ)]."""
    cart = _cart(request)
    opportunities = {o.pk: o for o in attach_raised(open_opportunities().filter(pk__in=[c["op"] for c in cart]))}
    lines = []
    for item in cart:
        opportunity = opportunities.get(item["op"])
        amount = to_decimal(item.get("amount"))
        if opportunity is not None and amount and amount > 0:
            lines.append((opportunity, int(item.get("qty", 1)), amount))
    return lines


def _owned_orders(request):
    return list(request.session.get(_key(request, "orders"), []))


def _donor_phone(request):
    return request.session.get(_key(request, "donor"), "")


def _can_view(request, order):
    phone = _donor_phone(request)
    return order.reference in _owned_orders(request) or (phone and order.donor_phone == phone)


def _base(request, **context):
    context.setdefault("store", request.store)
    context.setdefault("store_ready", request.store_ready)
    context.setdefault("cart_count", len(_cart(request)))
    return context


def _unavailable(request):
    return render(request, "store/site/unavailable.html", _base(request))


def _line_amount(request, opportunity, data):
    """يتحقق من مبلغ التبرع المختار لفرصة. يعيد (الكمية، المبلغ، الخطأ)."""
    if opportunity.fixed_amount:
        try:
            qty = int(data.get("quantity") or 1)
        except ValueError:
            qty = 0
        if not 1 <= qty <= MAX_QTY:
            return 0, None, f"اختر عدداً من 1 إلى {MAX_QTY}."
        return qty, flt(opportunity.fixed_amount) * qty, ""
    amount = to_decimal(data.get("custom_amount") or data.get("amount") or "")
    if amount is None:
        return 0, None, "اختر مبلغ التبرع."
    amount = amount.quantize(Decimal("0.01"))
    if not opportunity.allow_custom_amount and int(amount) not in request.store.amounts:
        return 0, None, "اختر أحد المبالغ المقترحة."
    if not MIN_AMOUNT <= amount <= MAX_AMOUNT:
        return 0, None, f"المبلغ من {MIN_AMOUNT} إلى {MAX_AMOUNT:,} ريال."
    return 1, amount, ""


# ─────────────────────────── الصفحات ───────────────────────────

@store_page
def store_home(request):
    kind = request.GET.get("kind", "")
    qs = open_opportunities()
    kinds = [(value, label) for value, label in OpportunityKind.choices if qs.filter(kind=value).exists()]
    if kind in dict(OpportunityKind.choices):
        qs = qs.filter(kind=kind)
    else:
        kind = ""
    return render(request, "store/site/home.html", _base(
        request, opportunities=attach_raised(qs), kinds=kinds, kind=kind, amounts=request.store.amounts[:4]))


@store_page
def store_opportunity(request, pk):
    opportunity = get_object_or_404(open_opportunities(), pk=pk)
    attach_raised([opportunity])
    error = ""
    if request.method == "POST":
        if not request.store_ready:
            return _unavailable(request)
        form = HoneypotForm(request.POST)
        if form.is_valid() and form.cleaned_data["website"]:
            return redirect(site_url(request, "store_home"))
        qty, amount, error = _line_amount(request, opportunity, request.POST)
        if not error:
            cart = [c for c in _cart(request) if c["op"] != opportunity.pk]
            cart.append({"op": opportunity.pk, "qty": qty, "amount": str(amount)})
            _save_cart(request, cart)
            target = "store_checkout" if request.POST.get("now") else "store_cart"
            return redirect(site_url(request, target))
    return render(request, "store/site/opportunity.html", _base(
        request, o=opportunity, error=error, amounts=request.store.amounts,
        selected=request.POST.get("amount") or request.GET.get("amount", "")))


@store_page
def store_cart(request):
    if request.method == "POST":
        cart = _cart(request)
        try:
            pk = int(request.POST.get("op", 0))
        except ValueError:
            pk = 0
        if request.POST.get("action") == "remove":
            cart = [c for c in cart if c["op"] != pk]
            _save_cart(request, cart)
        elif request.POST.get("action") == "update":
            opportunity = open_opportunities().filter(pk=pk).first()
            if opportunity is not None:
                qty, amount, error = _line_amount(request, opportunity, request.POST)
                if not error:
                    for c in cart:
                        if c["op"] == pk:
                            c["qty"], c["amount"] = qty, str(amount)
                    _save_cart(request, cart)
        return redirect(site_url(request, "store_cart"))
    lines = _cart_lines(request)
    return render(request, "store/site/cart.html", _base(
        request, lines=lines, total=sum((amount for _, _, amount in lines), Decimal(0))))


@store_page
def store_checkout(request):
    if not request.store_ready:
        return _unavailable(request)
    lines = _cart_lines(request)
    if not lines:
        return redirect(site_url(request, "store_cart"))
    total = sum((amount for _, _, amount in lines), Decimal(0))
    allow_monthly = request.store.allow_recurring
    initial = {"is_monthly": any(o.is_monthly_default for o, _, _ in lines)}
    if _donor_phone(request):
        initial["donor_phone"] = _donor_phone(request)
    form = CheckoutForm(request.POST or None, allow_monthly=allow_monthly, initial=initial)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        if data["website"]:
            return redirect(site_url(request, "store_home"))
        code = request.session.get(_key(request, "ref"))
        marketer = Marketer.objects.filter(code=code, is_active=True).first() if code else None
        with transaction.atomic():
            order = DonationOrder.objects.create(
                donor_name=data["donor_name"].strip(), donor_phone=data["donor_phone"],
                donor_email=data["donor_email"], is_anonymous=data["is_anonymous"],
                gift_to_name=data["gift_to_name"], gift_to_phone=data["gift_to_phone"],
                gift_message=data["gift_message"], marketer=marketer, total=total,
                is_monthly=bool(data.get("is_monthly")))
            for opportunity, qty, amount in lines:
                DonationOrderLine.objects.create(order=order, opportunity=opportunity, quantity=qty, amount=amount)
        owned = _owned_orders(request)[-19:] + [order.reference]
        request.session[_key(request, "orders")] = owned
        _save_cart(request, [])
        return redirect(site_url(request, "store_pay", order.number))
    return render(request, "store/site/checkout.html", _base(request, form=form, lines=lines, total=total))


@store_page
def store_pay(request, number):
    if not request.store_ready:
        return _unavailable(request)
    order = get_object_or_404(DonationOrder, number=number)
    if not _can_view(request, order):
        raise Http404
    if order.status == OrderStatus.PAID:
        return redirect(site_url(request, "store_thanks", order.number))
    config = {
        "element": ".mysr-form", "amount": order.amount_halalas, "currency": "SAR",
        "description": order.description, "publishable_api_key": request.store.publishable_key,
        "callback_url": request.build_absolute_uri(site_url(request, "store_callback")),
        "methods": ["creditcard", "applepay", "stcpay"], "supported_networks": ["mada", "visa", "mastercard"],
        "metadata": {"order": order.number, "ref": order.reference},
        "apple_pay": {"country": "SA", "label": request.site.company.name,
                      "validate_merchant_url": "https://api.moyasar.com/v1/applepay/initiate"},
        "language": "ar",
    }
    if order.is_monthly:
        config["credit_card"] = {"save_card": True}
        config["methods"] = ["creditcard"]  # الخصم الشهري بالبطاقة المحفوظة فقط
    return render(request, "store/site/pay.html", _base(
        request, order=order, config=config, js_url=moyasar.JS_URL, css_url=moyasar.CSS_URL))


@store_page
def store_callback(request):
    """عودة المتبرع من ميسّر: لا نثق بالحالة في الرابط، بل نجلب العملية بالمفتاح السري."""
    payment_id = (request.GET.get("id") or "").strip()
    if not payment_id:
        return redirect(site_url(request, "store_home"))
    order = DonationOrder.objects.filter(payment_id=payment_id).first()
    try:
        payment = moyasar.fetch_payment(request.store.secret_key, payment_id)
    except moyasar.MoyasarError:
        payment = None
    if order is None and payment:
        metadata = payment.get("metadata") or {}
        order = (DonationOrder.objects.filter(reference=str(metadata.get("ref", ""))).first()
                 or DonationOrder.objects.filter(number=str(metadata.get("order", ""))).first())
    if order is None:
        return render(request, "store/site/thanks.html", _base(request, order=None, pending=payment is None))
    if payment is not None:
        order, _ = finalize(order, payment)
    return redirect(site_url(request, "store_thanks", order.number))


@store_page
def store_thanks(request, number):
    order = DonationOrder.objects.filter(number=number).first()
    if order is None or not _can_view(request, order):
        return render(request, "store/site/thanks.html", _base(request, order=None))
    if order.status == OrderStatus.PENDING and order.payment_id and request.GET.get("check"):
        try:
            order, _ = fetch_and_finalize(order, order.payment_id)
        except moyasar.MoyasarError:
            pass
    return render(request, "store/site/thanks.html", _base(request, order=order, lines=order.lines.select_related(
        "opportunity")))


@store_page
def store_certificate(request, number):
    order = get_object_or_404(DonationOrder, number=number, status=OrderStatus.PAID)
    code = (request.GET.get("c") or "").strip().upper()
    if not (_can_view(request, order) or (code and hmac.compare_digest(code, order.verify_code))):
        raise Http404
    return render(request, "store/site/certificate.html", {
        "order": order, "lines": order.lines.select_related("opportunity"),
        "verify_url": request.build_absolute_uri(
            site_url(request, "store_certificate", order.number) + f"?c={order.verify_code}")})


# ─────────────────────────── حساب المتبرع ───────────────────────────

def _otp_hash(company_id, phone, code):
    return hashlib.sha256(f"{django_settings.SECRET_KEY}:store:{company_id}:{phone}:{code}".encode()).hexdigest()


@store_page
def store_account(request):
    company = request.site.company
    phone = _donor_phone(request)
    message = error = ""
    pending_phone = request.session.get(_key(request, "otp_phone"), "")
    phone_form, code_form = PhoneForm(), CodeForm()
    if request.method == "POST":
        action = request.POST.get("action")
        if request.POST.get("website"):
            return redirect(site_url(request, "store_account"))
        if action == "send":
            phone_form = PhoneForm(request.POST)
            if phone_form.is_valid():
                target = phone_form.cleaned_data["phone"]
                last = DonorLogin.objects.filter(phone=target).order_by("-sent_at").first()
                if last and (timezone.now() - last.sent_at).total_seconds() < OTP_RESEND_SECONDS:
                    error = "انتظر دقيقة قبل طلب رمز جديد."
                else:
                    has_history = (DonationOrder.objects.filter(donor_phone=target).exists()
                                   or RecurringDonation.objects.filter(donor_phone=target).exists())
                    if has_history:
                        code = f"{secrets.randbelow(10 ** 6):06d}"
                        DonorLogin.objects.filter(phone=target).delete()
                        DonorLogin.objects.create(phone=target, code_hash=_otp_hash(company.pk, target, code))
                        try:
                            from messaging.service import send

                            send(company, target, f"رمز الدخول لحساب تبرعاتك لدى {company.name}: {code}\n"
                                                  f"صالح {OTP_TTL_MINUTES} دقائق.", event="store_otp",
                                 reference="store-login", mode="provider")
                        except Exception:  # noqa: BLE001
                            error = "تعذر إرسال الرمز الآن. حاول لاحقاً."
                    if not error:
                        request.session[_key(request, "otp_phone")] = target
                        pending_phone = target
                        message = "إن كان للرقم تبرعات لدينا فسيصله رمز تحقق من 6 أرقام."
        elif action == "verify" and pending_phone:
            code_form = CodeForm(request.POST)
            if code_form.is_valid():
                login = DonorLogin.objects.filter(phone=pending_phone).order_by("-sent_at").first()
                if login is None:
                    error = "اطلب رمزاً جديداً."
                elif login.attempts >= OTP_MAX_ATTEMPTS:
                    error = "تجاوزت عدد المحاولات. اطلب رمزاً جديداً."
                elif timezone.now() - login.sent_at > timezone.timedelta(minutes=OTP_TTL_MINUTES):
                    error = "انتهت صلاحية الرمز. اطلب رمزاً جديداً."
                elif not hmac.compare_digest(_otp_hash(company.pk, pending_phone, code_form.cleaned_data["code"]),
                                             login.code_hash):
                    login.attempts += 1
                    login.save(update_fields=["attempts"])
                    error = "رمز التحقق غير صحيح."
                else:
                    DonorLogin.objects.filter(phone=pending_phone).delete()
                    request.session.cycle_key()
                    request.session[_key(request, "donor")] = pending_phone
                    request.session.pop(_key(request, "otp_phone"), None)
                    return redirect(site_url(request, "store_account"))
        elif action == "change":
            request.session.pop(_key(request, "otp_phone"), None)
            return redirect(site_url(request, "store_account"))
        elif action == "logout":
            request.session.pop(_key(request, "donor"), None)
            return redirect(site_url(request, "store_account"))
        elif action == "cancel_recurring" and phone:
            rd = RecurringDonation.objects.filter(pk=request.POST.get("id") or 0,
                                                  donor_phone__in=phone_variants(phone)).first()
            if rd is not None:
                rd.deactivate()
                message = "أُوقف التبرع الشهري. جزاك الله خيراً."
    context = _base(request, phone=phone, pending_phone=pending_phone, phone_form=phone_form, code_form=code_form,
                    message=message, error=error)
    if phone:
        context["orders"] = DonationOrder.objects.filter(donor_phone=phone, status=OrderStatus.PAID).prefetch_related(
            "lines__opportunity")
        context["recurring"] = RecurringDonation.objects.filter(donor_phone=phone).select_related("opportunity")
        context["total"] = sum((o.total for o in context["orders"]), Decimal(0))
    return render(request, "store/site/account.html", context)

