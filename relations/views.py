import datetime

from django import forms
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_date

from core.forms import style_form
from core.tenancy import tenant_context
from core.utils import flt
from relations.models import (EventStatus, MediaChannel, MediaCoverage, Partnership, PartnershipStatus, PREvent,
                              QuestionType, Survey, SurveyResponse, Visit, VisitDirection, VisitStatus)


@login_required
def relations_dashboard(request):
    today = timezone.localdate()
    year = today.year
    soon = today + datetime.timedelta(days=30)
    done_events = PREvent.objects.filter(status=EventStatus.DONE, start_date__year=year)
    return render(request, "relations/dashboard.html", {
        "title": "لوحة العلاقات العامة والإعلام", "year": year,
        "active_partnerships": Partnership.objects.filter(status=PartnershipStatus.ACTIVE).count(),
        "events_count": PREvent.objects.filter(start_date__year=year).exclude(status=EventStatus.CANCELLED).count(),
        "attendees": done_events.aggregate(t=Sum("actual_attendees"))["t"] or 0,
        "coverage_count": MediaCoverage.objects.filter(published_on__year=year).count(),
        "upcoming_events": PREvent.objects.filter(status=EventStatus.PLANNED, start_date__range=(today, soon))
        .order_by("start_date"),
        "upcoming_visits": Visit.objects.filter(status=VisitStatus.SCHEDULED, visit_date__range=(today, soon))
        .order_by("visit_date"),
        "expiring": Partnership.objects.filter(status=PartnershipStatus.ACTIVE, end_date__isnull=False,
                                               end_date__lte=today + datetime.timedelta(days=60)).order_by("end_date"),
        "follow_ups": Visit.objects.exclude(status=VisitStatus.CANCELLED).filter(follow_up_date__lte=today)
        .order_by("follow_up_date")[:50],
        "surveys": Survey.objects.filter(is_open=True).filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
        .annotate(n=Count("responses")),
    })


def _period(request):
    today = timezone.localdate()
    from_date = parse_date(request.GET.get("from_date") or "") or datetime.date(today.year, 1, 1)
    to_date = parse_date(request.GET.get("to_date") or "") or today
    return from_date, to_date


@login_required
def relations_report(request):
    """تقرير نشاط العلاقات العامة لفترة: الفعاليات المنفذة، الزيارات، الشراكات الموقعة، والتغطيات حسب الوسيلة."""
    from_date, to_date = _period(request)
    events = PREvent.objects.filter(status=EventStatus.DONE, start_date__range=(from_date, to_date)).order_by("start_date")
    event_totals = events.aggregate(attendees=Sum("actual_attendees"), cost=Sum("actual_cost"), budget=Sum("budget"))
    visits = Visit.objects.filter(status=VisitStatus.DONE, visit_date__range=(from_date, to_date)).order_by("visit_date")
    partnerships = (Partnership.objects.filter(start_date__range=(from_date, to_date))
                    .exclude(status__in=[PartnershipStatus.DRAFT, PartnershipStatus.CANCELLED]).order_by("start_date"))
    coverages = MediaCoverage.objects.filter(published_on__range=(from_date, to_date))
    by_channel = {row["channel"]: row for row in coverages.values("channel").annotate(n=Count("id"), reach=Sum("reach"))}
    channels = [{"label": label, "n": by_channel.get(value, {}).get("n", 0),
                 "reach": by_channel.get(value, {}).get("reach") or 0} for value, label in MediaChannel.choices]
    return render(request, "relations/report.html", {
        "title": "تقرير نشاط العلاقات العامة والإعلام", "from_date": from_date, "to_date": to_date,
        "events": events, "event_attendees": event_totals["attendees"] or 0,
        "event_cost": flt(event_totals["cost"]), "event_budget": flt(event_totals["budget"]),
        "visits": visits, "incoming_visits": visits.filter(direction=VisitDirection.INCOMING).count(),
        "outgoing_visits": visits.filter(direction=VisitDirection.OUTGOING).count(),
        "partnerships": partnerships, "partnerships_value": flt(partnerships.aggregate(t=Sum("value"))["t"]),
        "channels": channels, "coverage_count": coverages.count(), "coverage_reach": sum(c["reach"] for c in channels),
    })


@login_required
def survey_results(request, pk):
    survey = get_object_or_404(Survey.objects.all(), pk=pk)
    responses = list(survey.responses.order_by("-submitted_at"))
    results = []
    for question in survey.questions.all():
        values = [r.answers.get(str(question.pk)) for r in responses]
        values = [v for v in values if v not in (None, "")]
        row = {"question": question, "answered": len(values)}
        if question.question_type == QuestionType.RATING:
            ratings = [int(v) for v in values if str(v).isdigit() and 1 <= int(v) <= 5]
            row["average"] = round(sum(ratings) / len(ratings), 2) if ratings else None
            row["distribution"] = [(i, ratings.count(i), round(ratings.count(i) * 100 / len(ratings)) if ratings else 0)
                                   for i in range(1, 6)]
        elif question.question_type == QuestionType.CHOICE:
            options = question.choice_list
            # الخيارات التي حُذفت من السؤال بعد استلام ردود تظهر أيضاً حتى لا تضيع أصواتها
            options += [v for v in dict.fromkeys(values) if v not in options]
            row["counts"] = [(o, values.count(o), round(values.count(o) * 100 / len(values)) if values else 0)
                             for o in options]
        else:
            row["texts"] = values[:50]
        results.append(row)
    return render(request, "relations/survey_results.html", {
        "title": f"نتائج الاستبيان: {survey}", "survey": survey, "count": len(responses), "results": results})


def build_survey_form(survey, data=None):
    """نموذج ديناميكي من أسئلة الاستبيان: حقل لكل سؤال باسم q_<معرّفه>."""
    form = forms.Form(data)
    for question in survey.questions.all():
        name = f"q_{question.pk}"
        if question.question_type == QuestionType.RATING:
            field = forms.TypedChoiceField(choices=[("", "— اختر —")] + [(i, str(i)) for i in range(1, 6)], coerce=int,
                                           error_messages={"invalid_choice": "التقييم يجب أن يكون من 1 إلى 5."})
        elif question.question_type == QuestionType.CHOICE:
            field = forms.ChoiceField(choices=[("", "— اختر —")] + [(c, c) for c in question.choice_list],
                                      error_messages={"invalid_choice": "اختر أحد الخيارات المتاحة."})
        else:
            field = forms.CharField(max_length=2000, widget=forms.Textarea(attrs={"rows": 3}))
        field.label = question.text
        field.required = question.required
        field.error_messages["required"] = "هذا السؤال إجباري."
        form.fields[name] = field
    form.fields["website"] = forms.CharField(required=False, widget=forms.HiddenInput)  # مصيدة للبرامج الآلية
    return style_form(form)


def survey_respond(request, token):
    """صفحة عامة بلا تسجيل دخول: يجيب المستفيد أو الشريك على الاستبيان برابطه المنشور."""
    survey = get_object_or_404(Survey.all_objects.select_related("company"), public_token=token)
    subscription = getattr(survey.company, "subscription", None)
    closed = (not survey.accepting or not survey.company.is_nonprofit or subscription is None
              or not subscription.is_valid)
    form = build_survey_form(survey, request.POST or None)
    done = False
    if request.method == "POST" and not closed and form.is_valid():
        data = form.cleaned_data
        if not data["website"]:  # البوت لا يُحفظ رده، ويرى رسالة الشكر كالمعتاد
            answers = {name[2:]: value for name, value in data.items()
                       if name.startswith("q_") and value not in (None, "")}
            with tenant_context(survey.company):
                SurveyResponse.all_objects.create(company=survey.company, survey=survey, answers=answers)
        done = True
    return render(request, "relations/survey_respond.html",
                  {"survey": survey, "form": form, "closed": closed, "done": done})
