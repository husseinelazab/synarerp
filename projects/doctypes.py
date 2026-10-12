from core.registry import DocType, register
from projects.models import Project, ProjectTask

register(DocType(
    slug="project", model=Project, module="projects",
    list_fields=["name", "customer", "status", "start_date", "end_date", "contract_value", "manager"],
    search_fields=["name", "customer__name"], detail_template="projects/_project_detail.html",
))
register(DocType(
    slug="project-task", model=ProjectTask, module="projects",
    list_fields=["title", "project", "assigned_to", "due_date", "status", "progress"],
    search_fields=["title", "project__name"],
))
