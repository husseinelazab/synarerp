from django.db import models

from core.models import TenantModel
from core.tenancy import tenant_unique


class Department(TenantModel):
    name = models.CharField("اسم القسم", max_length=100)
    parent = models.ForeignKey("self", models.PROTECT, null=True, blank=True, verbose_name="القسم الأم")

    class Meta:
        verbose_name = "قسم"
        verbose_name_plural = "الأقسام"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="department")]

    def __str__(self):
        return self.name


class Designation(TenantModel):
    name = models.CharField("المسمى الوظيفي", max_length=100)

    class Meta:
        verbose_name = "مسمى وظيفي"
        verbose_name_plural = "المسميات الوظيفية"
        ordering = ["name"]
        constraints = [tenant_unique("name", model_label="designation")]

    def __str__(self):
        return self.name


class Gender(models.TextChoices):
    MALE = "male", "ذكر"
    FEMALE = "female", "أنثى"


class EmployeeStatus(models.TextChoices):
    ACTIVE = "active", "على رأس العمل"
    LEAVE = "leave", "في إجازة"
    LEFT = "left", "منتهية خدماته"


class Employee(TenantModel):
    employee_number = models.CharField("الرقم الوظيفي", max_length=20)
    full_name = models.CharField("الاسم الكامل", max_length=200)
    gender = models.CharField("الجنس", max_length=10, choices=Gender.choices)
    nationality = models.CharField("الجنسية", max_length=50, blank=True)
    national_id = models.CharField("رقم الهوية / الإقامة", max_length=20, blank=True)
    date_of_birth = models.DateField("تاريخ الميلاد", null=True, blank=True)
    date_of_joining = models.DateField("تاريخ التعيين")
    department = models.ForeignKey(Department, models.PROTECT, null=True, blank=True, verbose_name="القسم")
    designation = models.ForeignKey(Designation, models.PROTECT, null=True, blank=True, verbose_name="المسمى الوظيفي")
    basic_salary = models.DecimalField("الراتب الأساسي", max_digits=18, decimal_places=2, default=0)
    housing_allowance = models.DecimalField("بدل السكن", max_digits=18, decimal_places=2, default=0)
    transport_allowance = models.DecimalField("بدل النقل", max_digits=18, decimal_places=2, default=0)
    iban = models.CharField("رقم الآيبان", max_length=34, blank=True)
    phone = models.CharField("الجوال", max_length=30, blank=True)
    email = models.EmailField("البريد الإلكتروني", blank=True)
    status = models.CharField("الحالة", max_length=10, choices=EmployeeStatus.choices, default=EmployeeStatus.ACTIVE)

    class Meta:
        verbose_name = "موظف"
        verbose_name_plural = "الموظفون"
        ordering = ["employee_number"]
        constraints = [tenant_unique("employee_number", model_label="employee")]

    def __str__(self):
        return f"{self.employee_number} - {self.full_name}"

    @property
    def gross_salary(self):
        return self.basic_salary + self.housing_allowance + self.transport_allowance
