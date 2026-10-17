"""Shared visibility checks for cross-module employee workspace pages."""
from core.permissions import path_allowed, plan_allows_path, scope_queryset


def can_read(request, slug):
    path = f"/app/{slug}/"
    subscription = getattr(request.company, "subscription", None)
    return path_allowed(request.membership, path) and plan_allows_path(subscription.plan if subscription else None, path)


def can_module(request, module):
    path = f"/module/{module}/"
    subscription = getattr(request.company, "subscription", None)
    return path_allowed(request.membership, path) and plan_allows_path(subscription.plan if subscription else None, path)


def scoped(request, model):
    return scope_queryset(request, model, model.objects.filter(company=request.company))
