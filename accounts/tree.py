"""بناء صفوف شجرة الحسابات: الحسابات الرئيسية (الرؤوس) بمجاميع فروعها، والفرعية تحتها بإزاحة."""
from collections import defaultdict

from accounts.models import Account
from core.utils import flt


def tree_rows(leaf_values, width, root_types=None, max_depth=None, keep=None):
    """leaf_values: {account_id: [قيم]} للحسابات الفرعية. تعيد صفوفاً بترتيب الشجرة:
    {"account", "depth", "is_group", "values"} حيث قيم الحساب الرئيسي = مجموع فروعه.
    """
    keep = keep or (lambda values: any(values))
    children = defaultdict(list)
    for account in Account.objects.all().order_by("number", "name"):
        children[account.parent_id].append(account)

    def walk(account, depth):
        totals = list(leaf_values.get(account.id, [flt(0)] * width))
        descendants = []
        for child in children[account.id]:
            child_totals, child_rows = walk(child, depth + 1)
            totals = [a + b for a, b in zip(totals, child_totals)]
            descendants.extend(child_rows)
        node = {"account": account, "depth": depth, "is_group": account.is_group, "values": totals}
        return totals, ([node] + descendants if keep(totals) else [])

    rows = []
    for root in children[None]:
        if root_types and root.root_type not in root_types:
            continue
        rows.extend(walk(root, 0)[1])
    if max_depth:
        rows = [row for row in rows if row["depth"] < max_depth]
    return rows


def max_tree_depth():
    depth, level = 0, list(Account.objects.filter(parent__isnull=True).values_list("id", flat=True))
    while level:
        depth += 1
        level = list(Account.objects.filter(parent_id__in=level).values_list("id", flat=True))
    return depth
