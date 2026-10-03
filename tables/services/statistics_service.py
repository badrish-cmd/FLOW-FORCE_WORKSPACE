import datetime
from collections import defaultdict
from django.core.cache import cache
from django.utils import timezone
from django.db.models import Count, Q
from django.db.models.functions import ExtractYear

from tables.models import CellValue
from tasks.models import Task


class TableStatisticsService:
    """
    Dedicated service for calculating, aggregating, and caching table statistics.
    Extracted from tables/views.py in Phase 3A to isolate statistics business logic.
    """

    CACHE_TTL = 86400  # 24 hours

    @classmethod
    def get_cache_key(cls, table_id, today_str=None):
        """Constructs the standard Redis cache key for table statistics."""
        if not today_str:
            today_str = timezone.localdate().isoformat()
        return f"table_stats_{table_id}_{today_str}"

    @classmethod
    def get_default_statistics(cls):
        """Returns default empty statistics when include_stats is false or table is empty."""
        return {
            'unique_pids': [],
            'unique_column_values': {},
            'unique_years': [],
            'status_counts': {},
            'priority_counts': {'Urgent': 0, 'High': 0, 'Med': 0, 'Low': 0},
            'project_counts': {},
            'due_today_count': 0,
            'overdue_count': 0,
            'total_qty': 0.0,
            'completion_stats': {'completed': 0, 'total': 0, 'percent': 0},
            'week_actuals': {
                'calls': 0,
                'visits': 0,
                'enquiries': 0,
                'quotes': 0,
                'orders': 0,
                'achievementPercent': 0.0,
            },
        }

    @classmethod
    def invalidate_cache(cls, table_id, today_str=None):
        """Invalidates the Redis cache entry for table statistics."""
        cache_key = cls.get_cache_key(table_id, today_str)
        cache.delete(cache_key)

    @classmethod
    def get_table_statistics(cls, table, today_date=None, use_cache=True):
        """
        Calculates and returns complete statistics, unique PIDs, unique years,
        and filterable column options for a given Table instance.
        """
        if not table or not table.id:
            return cls.get_default_statistics()

        if today_date is None:
            today_date = timezone.localdate()
        today_str = today_date.isoformat()
        cache_key = cls.get_cache_key(table.id, today_str)

        if use_cache:
            cached_data = cache.get(cache_key)
            if cached_data:
                defaults = cls.get_default_statistics()
                return {
                    'unique_pids': cached_data.get('unique_pids', defaults['unique_pids']),
                    'unique_column_values': cached_data.get('unique_column_values', defaults['unique_column_values']),
                    'unique_years': cached_data.get('unique_years', defaults['unique_years']),
                    'status_counts': cached_data.get('status_counts', defaults['status_counts']),
                    'priority_counts': cached_data.get('priority_counts', defaults['priority_counts']),
                    'project_counts': cached_data.get('project_counts', defaults['project_counts']),
                    'due_today_count': cached_data.get('due_today_count', defaults['due_today_count']),
                    'overdue_count': cached_data.get('overdue_count', defaults['overdue_count']),
                    'total_qty': cached_data.get('total_qty', defaults['total_qty']),
                    'completion_stats': cached_data.get('completion_stats', defaults['completion_stats']),
                    'week_actuals': cached_data.get('week_actuals', defaults['week_actuals']),
                }

        # 1. Fetch all columns once to eliminate repeated schema lookups
        all_columns = list(table.columns.all())
        pid_col = next((c for c in all_columns if c.name.upper() == 'PID'), None)
        project_col = next((c for c in all_columns if c.name.upper() == 'PROJECT'), None)
        qty_col = next((c for c in all_columns if c.name.upper() == 'QTY'), None)
        filterable_cols = [c for c in all_columns if c.is_filterable]
        date_cols = [c for c in all_columns if c.name in ['FOLLOW - UP DATE', 'FOLLOW-UP DATE', 'DATE']]

        # 2. Unique PIDs
        if pid_col:
            unique_pids = list(CellValue.objects.filter(
                column_id=pid_col.id,
                row__table=table,
                row__is_archived=False
            ).exclude(value=None).values_list('value', flat=True).distinct().order_by('value'))
        else:
            unique_pids = []

        # 3. Unique Column values for all filterable columns (Consolidated single batch query)
        unique_column_values = {}
        non_dropdown_cols = [c for c in filterable_cols if c.data_type != 'DROPDOWN']
        for col in filterable_cols:
            if col.data_type == 'DROPDOWN':
                opts = [o.strip() for o in (col.options or '').split(',') if o.strip()]
                unique_column_values[col.id] = opts
            else:
                unique_column_values[col.id] = []

        if non_dropdown_cols:
            col_ids = [c.id for c in non_dropdown_cols]
            cell_vals = CellValue.objects.filter(
                column_id__in=col_ids,
                row__table=table,
                row__is_archived=False
            ).exclude(
                value__isnull=True
            ).exclude(
                value=""
            ).values('column_id', 'value').distinct()

            col_val_sets = {c_id: set() for c_id in col_ids}
            for item in cell_vals:
                v = item['value']
                if v is not None and str(v).strip():
                    col_val_sets[item['column_id']].add(str(v).strip())

            for c_id, v_set in col_val_sets.items():
                unique_column_values[c_id] = sorted(list(v_set))

        # 4. Unique Years
        years_qs = Task.objects.filter(
            row__table=table,
            row__is_archived=False
        ).annotate(year=ExtractYear('due_date')).values_list('year', flat=True).distinct().order_by('-year')
        unique_years = [str(y) for y in years_qs if y]

        # 5. Status counts
        status_counts = {}
        s_counts = Task.objects.filter(
            row__table=table,
            row__is_archived=False
        ).values('status').annotate(count=Count('id'))
        for item in s_counts:
            val = item['status'] or 'PENDING'
            status_counts[val] = item['count']

        # 6. Priority counts
        priority_counts = {'Urgent': 0, 'High': 0, 'Med': 0, 'Low': 0}
        p_counts = Task.objects.filter(
            row__table=table,
            row__is_archived=False
        ).values('priority').annotate(count=Count('id'))
        for item in p_counts:
            priority = item['priority']
            pl = str(priority).lower()
            if pl.startswith('med'):
                priority_counts['Med'] += item['count']
            elif pl.startswith('urg'):
                priority_counts['Urgent'] += item['count']
            elif pl.startswith('hi'):
                priority_counts['High'] += item['count']
            elif pl.startswith('lo'):
                priority_counts['Low'] += item['count']

        # 7. Project counts (for List PID)
        project_counts = {}
        if project_col:
            pr_counts = CellValue.objects.filter(
                column_id=project_col.id,
                row__table=table,
                row__is_archived=False
            ).values('value').annotate(count=Count('id'))
            for item in pr_counts:
                val = item['value'] or 'No Project'
                project_counts[val] = item['count']

        # 8. Consolidated Task Aggregates (due_today, overdue, total, completed)
        task_aggs = Task.objects.filter(
            row__table=table,
            row__is_archived=False
        ).aggregate(
            total=Count('id'),
            completed=Count('id', filter=Q(status__in=['COMPLETED', 'COMPLETE'])),
            due_today=Count('id', filter=Q(due_date=today_date)),
            overdue=Count('id', filter=Q(due_date__lt=today_date) & ~Q(status__in=['COMPLETED', 'APPROVED', 'COMPLETE']))
        )
        total_tasks = task_aggs['total'] or 0
        completed_tasks = task_aggs['completed'] or 0
        due_today_count = task_aggs['due_today'] or 0
        overdue_count = task_aggs['overdue'] or 0

        # 9. Total QTY (computed in Python to prevent database-specific JSONB casting crashes in PostgreSQL)
        total_qty = 0.0
        if qty_col:
            qty_cells = CellValue.objects.filter(
                column_id=qty_col.id,
                row__table=table,
                row__is_archived=False
            ).exclude(value=None).values_list('value', flat=True)

            for val in qty_cells:
                try:
                    if val is not None and str(val).strip():
                        total_qty += float(str(val).strip())
                except ValueError:
                    pass

        # 10. Completion stats
        completion_percent = round((completed_tasks / total_tasks) * 100) if total_tasks > 0 else 0
        completion_stats = {
            'completed': completed_tasks,
            'total': total_tasks,
            'percent': completion_percent
        }

        # 11. Week actuals for SALES followups
        monday = today_date - datetime.timedelta(days=today_date.weekday())
        sunday = monday + datetime.timedelta(days=6)

        # Filter row IDs first to avoid loading all cell values
        if date_cols:
            date_col_ids = [c.id for c in date_cols]
            row_ids_in_week = list(CellValue.objects.filter(
                column_id__in=date_col_ids,
                row__table=table,
                row__is_archived=False,
                value__range=[monday.isoformat(), sunday.isoformat()]
            ).values_list('row_id', flat=True).distinct())
        else:
            row_ids_in_week = []

        calls = 0
        visits = 0
        enquiries = 0
        quotes = 0
        orders = 0

        if row_ids_in_week:
            activity_col_ids = [c.id for c in all_columns if c.name in ['FOLLOW - UP DATE', 'FOLLOW-UP DATE', 'DATE', 'ACTIVITY TYPE', 'ACTIVITY_TYPE', 'STATUS']]
            cells_qs = CellValue.objects.filter(
                row_id__in=row_ids_in_week,
                row__is_archived=False,
                column_id__in=activity_col_ids
            ).select_related('column')

            row_cells = defaultdict(dict)
            for cell in cells_qs:
                row_cells[cell.row_id][cell.column.name] = cell.value

            for r_id, c_dict in row_cells.items():
                date_val = c_dict.get('FOLLOW - UP DATE') or c_dict.get('FOLLOW-UP DATE') or c_dict.get('DATE')
                if not date_val:
                    continue
                try:
                    if isinstance(date_val, str):
                        d = datetime.datetime.strptime(date_val.split('T')[0], "%Y-%m-%d").date()
                    else:
                        continue
                except Exception:
                    continue

                if monday <= d <= sunday:
                    act_type = str(c_dict.get('ACTIVITY TYPE') or c_dict.get('ACTIVITY_TYPE') or '').lower().strip()
                    status = str(c_dict.get('STATUS') or '').lower().strip()

                    if 'call' in act_type or 'whatsapp' in act_type or 'linkedin' in act_type:
                        calls += 1
                    if 'site visit' in act_type or 'customer visit' in act_type or act_type == 'visit':
                        visits += 1
                    if 'enquiry' in status or 'enquiries' in status:
                        enquiries += 1
                    if 'quotation' in status or 'quote' in status:
                        quotes += 1
                    if 'order received' in status or 'order' in status:
                        orders += 1

        target_calls = 20
        target_visits = 10
        target_enquiries = 10
        target_orders = 2

        calls_ach = min(100.0, (calls / target_calls) * 100 if target_calls else 0)
        visits_ach = min(100.0, (visits / target_visits) * 100 if target_visits else 0)
        enquiries_ach = min(100.0, (enquiries / target_enquiries) * 100 if target_enquiries else 0)
        orders_ach = min(100.0, (orders / target_orders) * 100 if target_orders else 0)

        achievement_percent = round((calls_ach + visits_ach + enquiries_ach + orders_ach) / 4.0, 2)

        week_actuals = {
            'calls': calls,
            'visits': visits,
            'enquiries': enquiries,
            'quotes': quotes,
            'orders': orders,
            'achievementPercent': achievement_percent
        }

        stats_data = {
            'unique_pids': unique_pids,
            'unique_column_values': unique_column_values,
            'unique_years': unique_years,
            'status_counts': status_counts,
            'priority_counts': priority_counts,
            'project_counts': project_counts,
            'due_today_count': due_today_count,
            'overdue_count': overdue_count,
            'total_qty': total_qty,
            'completion_stats': completion_stats,
            'week_actuals': week_actuals,
        }

        cache.set(cache_key, stats_data, cls.CACHE_TTL)
        return stats_data
