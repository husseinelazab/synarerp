"""بوابة الخدمة الذاتية للموظف: بياناته، إجازاته، سلفه، قسائم رواتبه، وحضوره."""
import datetime
from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.forms import formfield_callback, style_form
from hr.models import (MONTHS, Attendance, DocStatus, Employee, EmployeeLoan, LeaveApplication, LeaveType, PayrollLine,
                       month_bounds)


def _employee(request):
    return Employee.objects.filter(user=request.user).first()


def portal_view(view):
    @login_required
    def wrapper(request, *args, **kwargs):
        employee = _employee(request)
        if employee is None:
            return render(request, "hr/portal/no_profile.html", status=403)
        return view(request, employee, *args, **kwargs)
    wrapper.__name__ = view.__name__
    return wrapper


class LeaveRequestForm(forms.ModelForm):
    class Meta:
        model = LeaveApplication
        fields = ["leave_type", "from_date", "to_date", "remarks"]
        labels = {"remarks": "السبب / ملاحظات"}
        formfield_callback = formfield_callback


class LoanRequestForm(forms.Form):
    amount = forms.DecimalField(label="المبلغ المطلوب", min_value=Decimal("1"), max_digits=18, decimal_places=2)
    installment_amount = forms.DecimalField(label="القسط الشهري المقترح", min_value=Decimal("1"), max_digits=18,
                                            decimal_places=2)
    start_month = forms.TypedChoiceField(label="يبدأ الخصم من شهر", choices=MONTHS, coerce=int)
    start_year = forms.IntegerField(label="سنة", min_value=2000, max_value=2100)
    reason = forms.CharField(label="سبب الطلب", required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def clean(self):
        data = super().clean()
        if data.get("amount") and data.get("installment_amount") and data["installment_amount"] > data["amount"]:
            raise ValidationError("القسط لا يتجاوز مبلغ السلفة.")
        return data


@portal_view
def portal_home(request, employee):
    today = datetime.date.today()
    payslips = PayrollLine.objects.filter(employee=employee, parent__docstatus=DocStatus.SUBMITTED).select_related(
        "parent").order_by("-parent__year", "-parent__month")[:12]
    return render(request, "hr/portal/home.html", {
        "employee": employee, "h": employee.hr_summary(),
        "leaves": LeaveApplication.objects.filter(employee=employee).select_related("leave_type")[:10],
        "loans": EmployeeLoan.objects.filter(employee=employee)[:10],
        "payslips": payslips, "today": today,
        "appraisals": employee.appraisals.filter(docstatus=DocStatus.SUBMITTED).select_related("template")[:5],
        # الأهداف الجارية (مسودة تُتابع خلال الفترة) والمعتمدة
        "kpi_sheets": employee.kpi_sheets.exclude(docstatus=DocStatus.CANCELLED).prefetch_related("lines")[:4],
    })


@portal_view
def portal_leave(request, employee):
    form = style_form(LeaveRequestForm(request.POST or None))
    form.fields["leave_type"].queryset = LeaveType.objects.filter(disabled=False)
    if request.method == "POST" and form.is_valid():
        leave = form.save(commit=False)
        leave.employee, leave.created_by, leave.requested_via_portal = employee, request.user, True
        try:
            with transaction.atomic():
                leave.save()
                leave.calculate_totals()
                # فحص الرصيد والتداخل الآن ليعرف الموظف المشكلة قبل وصول الطلب للمدير
                sid = transaction.savepoint()
                try:
                    leave.validate_for_submit()
                finally:
                    transaction.savepoint_rollback(sid)
                leave.save()
        except ValidationError as exc:
            form.add_error(None, " ".join(exc.messages))
        else:
            leave.notify_managers_of_request()
            messages.success(request, f"تم إرسال طلب الإجازة ({leave.total_days} يوم) وهو بانتظار اعتماد المدير.")
            return redirect("portal_home")
    return render(request, "hr/portal/form.html", {"form": form, "title": "طلب إجازة", "employee": employee})


@portal_view
def portal_loan(request, employee):
    today = datetime.date.today()
    nxt = today.replace(day=1) + datetime.timedelta(days=32)
    form = LoanRequestForm(request.POST or None, initial={"start_month": nxt.month, "start_year": nxt.year})
    style_form(form)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        loan = EmployeeLoan(employee=employee, amount=data["amount"], installment_amount=data["installment_amount"],
                            start_month=data["start_month"], start_year=data["start_year"], remarks=data["reason"],
                            created_by=request.user, requested_via_portal=True)
        try:
            loan.clean()
            loan.save()
        except ValidationError as exc:
            form.add_error(None, " ".join(exc.messages))
        else:
            loan.notify_managers_of_request()
            messages.success(request, "تم إرسال طلب السلفة وهو بانتظار اعتماد المدير.")
            return redirect("portal_home")
    return render(request, "hr/portal/form.html", {"form": form, "title": "طلب سلفة", "employee": employee})


@portal_view
@require_POST
def portal_withdraw(request, employee, kind, pk):
    """سحب طلب لم يُعتمد بعد."""
    model = {"leave": LeaveApplication, "loan": EmployeeLoan}.get(kind)
    if model is None:
        return redirect("portal_home")
    obj = get_object_or_404(model, pk=pk, employee=employee, docstatus=DocStatus.DRAFT)
    obj.delete()
    messages.success(request, "تم سحب الطلب.")
    return redirect("portal_home")


@portal_view
def portal_payslip(request, employee, pk):
    line = get_object_or_404(PayrollLine, pk=pk, employee=employee, parent__docstatus=DocStatus.SUBMITTED)
    return render(request, "hr/payslips.html", {"run": line.parent, "lines": [line], "company": request.company})


@portal_view
def portal_attendance(request, employee):
    today = datetime.date.today()
    try:
        year, month = int(request.GET.get("year", today.year)), int(request.GET.get("month", today.month))
        start, end = month_bounds(year, month)
    except (ValueError, InvalidOperation):
        year, month = today.year, today.month
        start, end = month_bounds(year, month)
    records = Attendance.objects.filter(employee=employee, attendance_date__range=(start, end)).order_by("attendance_date")
    return render(request, "hr/portal/attendance.html", {
        "employee": employee, "records": records, "year": year, "month": month, "months": MONTHS,
        "years": range(today.year - 2, today.year + 1),
    })
