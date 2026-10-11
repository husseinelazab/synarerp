from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST


@login_required
@require_POST
def recurring_generate(request, pk):
    """إنشاء الفواتير المستحقة الآن دون انتظار المجدول اليومي."""
    from core.permissions import require_submit
    from selling.models import RecurringInvoice

    recurring = get_object_or_404(RecurringInvoice, pk=pk)
    try:
        if recurring.auto_submit:
            require_submit(request)
        created = recurring.generate_due(timezone.localdate(), request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect(recurring.get_absolute_url())
    if created:
        messages.success(request, f"أُنشئت {len(created)} فاتورة: " + "، ".join(inv.name for inv in created))
    else:
        messages.info(request, f"لا توجد فواتير مستحقة؛ القادمة بتاريخ {recurring.next_date:%Y-%m-%d}."
                      if recurring.next_date and not recurring.is_finished else "انتهت مدة العقد.")
    return redirect(recurring.get_absolute_url())
