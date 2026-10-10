from core.registry import DocType, register
from hr.models import Department, Designation, Employee

register(DocType(
    slug="employee", model=Employee, module="hr",
    list_fields=["employee_number", "full_name", "department", "designation", "status"],
    search_fields=["employee_number", "full_name", "national_id", "phone"],
))
register(DocType(
    slug="department", model=Department, module="hr", list_fields=["name", "parent"], search_fields=["name"],
))
register(DocType(
    slug="designation", model=Designation, module="hr", list_fields=["name"], search_fields=["name"],
))
