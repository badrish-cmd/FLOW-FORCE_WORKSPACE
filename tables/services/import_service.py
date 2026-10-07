import csv
import datetime
import io
import logging
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date
from dateutil import parser as du_parser

from tables.models import Table, Row, Column, CellValue
from tasks.models import Task
from auth_app.models import EmployeeUser
from tables.permissions import has_table_access, get_employees_with_table_access
from tables.services.statistics_service import TableStatisticsService
from .broadcaster import TableEventBroadcaster

logger = logging.getLogger(__name__)


class TableImportError(Exception):
    """Base exception for table import errors."""
    pass


class TableImportValidationError(TableImportError):
    """Raised when import data or parameters fail validation."""
    pass


class TableImportPermissionDeniedError(TableImportError):
    """Raised when user lacks permission to import rows into table."""
    pass


class TableImportService:
    """
    Dedicated service for CSV, Google Sheets, and tabular data imports.
    Extracts import business logic from tables/views.py in Phase 3E-3.
    """

    @classmethod
    def safe_parse_date(cls, val):
        """
        Parses dates flexibly from strings, date objects, Excel serial dates,
        or numeric timestamps across varied regional formats.
        """
        if val is None or val == "":
            return None
        if isinstance(val, (datetime.datetime, datetime.date)):
            return val if isinstance(val, datetime.date) else val.date()

        val_str = str(val).strip()
        if not val_str:
            return None

        # Check for Excel serial date numbers (e.g. 45443 or 45443.0) or 8-digit numeric dates
        try:
            val_float = float(val_str)
            if 10000 <= val_float <= 100000:
                base_date = datetime.date(1899, 12, 30)
                return base_date + datetime.timedelta(days=int(val_float))
            elif val_str.isdigit() and len(val_str) == 8:
                try:
                    return datetime.date(int(val_str[:4]), int(val_str[4:6]), int(val_str[6:8]))
                except ValueError:
                    pass
                try:
                    return datetime.date(int(val_str[4:8]), int(val_str[2:4]), int(val_str[:2]))
                except ValueError:
                    pass
        except ValueError:
            pass

        # Try Django's parse_date first
        try:
            d = parse_date(val_str)
            if d:
                return d
        except Exception:
            pass

        # Try dateutil parser with dayfirst=True and dayfirst=False
        try:
            return du_parser.parse(val_str, dayfirst=True).date()
        except Exception:
            pass

        try:
            return du_parser.parse(val_str, dayfirst=False).date()
        except Exception:
            pass

        # Try explicit format strptime
        for fmt in (
            "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d",
            "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y",
            "%d %b %Y", "%d %B %Y"
        ):
            try:
                return datetime.datetime.strptime(val_str, fmt).date()
            except ValueError:
                pass

        return None

    @classmethod
    def normalize_header(cls, name, job_type="GENERAL"):
        """
        Normalizes header string by stripping whitespace, newlines, and punctuation,
        and mapping known column synonyms according to table job_type.
        """
        if not name:
            return ""
        h = str(name).strip().upper()
        h = h.replace("\r", " ").replace("\n", " ").replace("\t", " ")
        h = h.replace(".", "_").replace(" ", "_").replace("-", "_").replace("/", "_")
        while "__" in h:
            h = h.replace("__", "_")
        h = h.strip("_")

        is_sales = (job_type == "SALES")
        is_list_pid = (job_type == "LIST_PID")
        is_logs = (job_type == "LOGS")

        # Map standard column name synonyms
        if is_sales:
            if h in [
                "TASK_NAME", "TASKNAME", "TASK", "CUSTOMER_NAME", "CUSTOMERNAME",
                "CUSTOMER", "CLIENT_NAME", "CLIENTNAME", "CLIENT", "NAME", "COMPANY"
            ]:
                return "CUSTOMER_NAME"
            if h in [
                "DUE_DATE", "DUEDATE", "FOLLOW_UP_DATE", "FOLLOWUPDATE",
                "FOLLOW_UP", "FOLLOWUP", "DATE"
            ]:
                return "FOLLOW_UP_DATE"
        elif is_list_pid:
            if h in [
                "ENQUIRY_NO", "ENQUIRYNO", "ENQUIRY", "ENQUIRIES", "TASK_NAME",
                "TASKNAME", "TASK", "CUSTOMER_NAME", "ENQUIRY_NO_QUOTATION_NO",
                "ENQUIRY_QUOTATION_NO", "ENQUIRY_NO_QUOTATION", "QUOTATION_NO",
                "QUOTATION", "QUOTATION_NUMBER", "ENQUIRY_NUMBER"
            ]:
                return "ENQUIRY_NO_QUOTATION_NO"
            if h in ["PID", "PID_NO", "PID_NUMBER"]:
                return "PID"
            if h in [
                "DUE_DATE_FLOW_FORCE", "FLOW_FORCE_DUE_DATE", "FLOW_FORCE",
                "DUE_DATE", "FOLLOW_UP_DATE", "DUE", "DEADLINE"
            ]:
                return "DUE_DATE_FLOW_FORCE"
            if h in ["NEW_PID_NO", "NEWPIDNO", "NEW_PID"]:
                return "NEW_PID_NO"
            if h in ["FFE_SINGAPORE", "FFE_SINGAPORE_PTE_LTD", "FFE"]:
                return "FFE_SINGAPORE"
            if h in ["COMPANY_NAME", "COMPANYNAME", "COMPANY"]:
                return "COMPANY_NAME"
            if h in ["DUE_DATE_CUSTOMER", "CUSTOMER_DUE_DATE", "DUE_CUSTOMER", "CUSTOMER_DUE"]:
                return "DUE_DATE_CUSTOMER"
            if h in ["QTY", "QUANTITY"]:
                return "QTY"
        elif is_logs:
            if h in [
                "TOOL_NAME", "TOOLNAME", "TOOL", "TOOL_NAME_LOG", "EQUIPMENT",
                "INSTRUMENT", "TASK_NAME", "TASKNAME", "TASK", "NAME"
            ]:
                return "TOOL_NAME"
            if h in [
                "RETURN_DATE", "RETURNDATE", "RETURN_BY", "RETURN",
                "DUE_DATE", "DUEDATE", "TARGET_DATE", "DEADLINE"
            ]:
                return "RETURN_DATE"
            if h in ["ISSUE_DATE", "ISSUEDATE", "ISSUED_DATE", "DATE", "ISSUE"]:
                return "ISSUE_DATE"
            if h in ["DAYS_OVERDUE", "DAYSOVERDUE", "DAYS_DUE", "OVERDUE_DAYS", "DAYS"]:
                return "DAYS_OVERDUE"
            if h in ["ISSUED_BY", "ISSUEDBY", "GIVEN_BY", "ISSUER"]:
                return "ISSUED_BY"
            if h in ["RECEIVED_BY", "RECEIVEDBY", "TAKEN_BY", "RECEIVER", "BORROWER"]:
                return "RECEIVED_BY"
        else:
            if h in [
                "TASK_NAME", "TASKNAME", "TASK", "CUSTOMER_NAME", "CUSTOMERNAME",
                "CUSTOMER", "CLIENT_NAME", "CLIENTNAME", "CLIENT", "NAME", "TITLE",
                "SUBJECT", "DESCRIPTION", "PARTICULARS", "ITEM", "SUMMARY", "JOB",
                "JOB_NAME", "WORK"
            ]:
                return "TASK_NAME"
            if h in [
                "DUE_DATE", "DUEDATE", "FOLLOW_UP_DATE", "FOLLOWUPDATE",
                "FOLLOW_UP", "FOLLOWUP", "TARGET_DATE", "DEADLINE", "DUE", "DATE"
            ]:
                return "DUE_DATE"

        if h in ["INITIAL_MAIL", "INITIALMAIL"]:
            return "INITIAL_MAIL"
        if h in ["ALERT_MAIL", "ALERTMAIL"]:
            return "ALERT_MAIL"
        if h in [
            "S_NO", "SNO", "SL_NO", "SLNO", "SERIAL_NO", "SERIALNO",
            "S_NO_", "SR_NO", "SRNO", "NO", "SR"
        ]:
            return "S_NO"

        return h

    @classmethod
    @transaction.atomic
    def import_rows_from_csv_data(cls, file_data, table, user, header_row=None, data_row=None, check_permissions=False):
        """
        Parses CSV string data, matches headers, creates Rows, CellValues, Tasks,
        and assigns users in bulk inside an atomic transaction.

        :param file_data: Raw CSV string
        :param table: Table instance
        :param user: User initiating the import
        :param header_row: Optional 1-based index of header row
        :param data_row: Optional 1-based index where data begins
        :param check_permissions: Boolean whether to enforce table EDIT access
        :return: Tuple (created_rows, error_message)
        """
        if check_permissions and not has_table_access(user, table, "EDIT"):
            raise TableImportPermissionDeniedError("No edit access to this table")

        lines = file_data.splitlines()

        # 1. Dynamically locate the header row
        header_idx = -1
        is_sales = (table.job_type == "SALES")
        is_list_pid = (table.job_type == "LIST_PID")
        is_personal = (table.job_type == "PERSONAL")
        is_logs = (table.job_type == "LOGS")

        table_col_names_upper = {c.name.strip().upper() for c in table.columns.all()}

        if header_row is not None and data_row is not None:
            try:
                header_line = lines[int(header_row) - 1]
                data_lines = lines[int(data_row) - 1:]
                lines_to_parse = [header_line] + data_lines
            except IndexError:
                return None, f"Specified header row ({header_row}) or data row ({data_row}) is out of bounds."
        else:
            # Scan lines to find a header candidate
            for idx, line in enumerate(lines[:30]):
                if not line.strip():
                    continue
                tokens = [t.strip().upper() for t in line.replace(";", ",").replace("\t", ",").split(",")]
                tokens = [t for t in tokens if t]
                if not tokens:
                    continue

                has_s_no = any(
                    t in ["S_NO", "S.NO", "S. NO.", "SL_NO", "SL.NO", "SL. NO.", "S NO", "SL NO", "SR_NO", "SR.NO"]
                    for t in tokens
                )
                if is_sales:
                    has_task = any("CUSTOMER" in t or "CLIENT" in t or "TASK" in t for t in tokens)
                    has_due = any("FOLLOW" in t or "UP" in t or "DUE" in t for t in tokens)
                elif is_list_pid:
                    has_task = any("ENQUIRY" in t or "PID" in t or "TASK" in t or "QUOTATION" in t for t in tokens)
                    has_due = any("FLOW" in t or "FORCE" in t or "CUSTOMER" in t or "DUE" in t for t in tokens)
                else:
                    has_task = any("TASK" in t or "NAME" in t or "TITLE" in t or "SUBJECT" in t or "ITEM" in t for t in tokens)
                    has_due = any("DUE" in t or "DATE" in t or "FOLLOW" in t for t in tokens)

                col_match_count = sum(
                    1 for t in tokens if t in table_col_names_upper or t.replace(" ", "_") in table_col_names_upper
                )

                if (has_s_no and (has_task or has_due)) or (has_task and has_due) or col_match_count >= 2:
                    header_idx = idx
                    break

            if header_idx != -1:
                lines_to_parse = lines[header_idx:]
            else:
                # Default to line 0 (Row 1 is header) if auto-detection finds no specific line
                lines_to_parse = lines

        # Detect delimiter (comma, semicolon, tab)
        sample_header = lines_to_parse[0] if lines_to_parse else ""
        delimiter = ","
        if ";" in sample_header and "," not in sample_header:
            delimiter = ";"
        elif "\t" in sample_header and "," not in sample_header:
            delimiter = "\t"

        io_string = io.StringIO("\n".join(lines_to_parse))
        reader = csv.DictReader(io_string, delimiter=delimiter)

        if not reader.fieldnames:
            return None, "Import file is empty or invalid"

        # Map normalized DB column name -> Column object
        normalized_db_cols = {}
        for col in table.columns.all():
            norm_name = cls.normalize_header(col.name, table.job_type)
            normalized_db_cols[norm_name] = col

        db_col_names = set(normalized_db_cols.keys())

        # Normalize CSV fieldnames to match DB columns
        csv_headers = []
        header_mapping = {}
        for name in reader.fieldnames:
            if not name:
                continue
            normalized = cls.normalize_header(name, table.job_type)
            csv_headers.append(normalized)
            header_mapping[name] = normalized

        # Dynamic primary column matching
        if not is_personal:
            required_header = (
                "CUSTOMER_NAME" if is_sales else
                ("ENQUIRY_NO_QUOTATION_NO" if is_list_pid else "TASK_NAME")
            )
            if required_header not in csv_headers:
                non_task_sys_cols = {
                    "S_NO", "DATE", "DUE_DATE", "FOLLOW_UP_DATE",
                    "DUE_DATE_FLOW_FORCE", "DUE_DATE_CUSTOMER",
                    "INITIAL_MAIL", "ALERT_MAIL"
                }
                candidate_primary = [h for h in csv_headers if h not in non_task_sys_cols]
                if not candidate_primary:
                    return None, f"Required column for task name/customer name/enquiry/quotation no is missing in the CSV sheet headers. Expected one of: {required_header}"

        # Performance Optimizations: pre-map columns, pre-query S_NO base, and setup user cache
        columns_by_name = {c.name: c for c in table.columns.all()}

        base_s_no = 0
        s_no_col = normalized_db_cols.get("S_NO")
        if s_no_col:
            latest_cell = CellValue.objects.filter(column=s_no_col).order_by("-id").first()
            if latest_cell and latest_cell.value is not None:
                if isinstance(latest_cell.value, int):
                    base_s_no = latest_cell.value
                elif isinstance(latest_cell.value, str) and latest_cell.value.isdigit():
                    base_s_no = int(latest_cell.value)

        resolved_users_cache = {}
        list_pid_employees = []
        if is_list_pid:
            list_pid_employees = list(get_employees_with_table_access(table))

        rows_to_create = []
        row_temp_data = []
        row_import_idx = 1

        for row_dict in reader:
            normalized_row = {}
            has_any_value = False
            for original_key, val in row_dict.items():
                if not original_key:
                    continue
                normalized_key = header_mapping.get(original_key)
                if normalized_key:
                    normalized_row[normalized_key] = val
                    if val is not None and str(val).strip() != "":
                        has_any_value = True

            # Skip completely empty rows
            if not has_any_value:
                continue

            # Task name & date resolution
            if is_list_pid:
                # CRITICAL: Strict PID mapping. Enquiry/Quotation is separate from PID.
                # Blank enquiry remains blank and never falls back greedily to PID.
                enquiry_val = normalized_row.get("ENQUIRY_NO_QUOTATION_NO", "")
                ff_date_str = normalized_row.get("DUE_DATE_FLOW_FORCE")
                cust_date_str = normalized_row.get("DUE_DATE_CUSTOMER")
                ff_date = cls.safe_parse_date(ff_date_str) if ff_date_str else None
                cust_date = cls.safe_parse_date(cust_date_str) if cust_date_str else None
                due_date = ff_date or cust_date
            elif is_personal:
                task_name = None
                for col_name, val in normalized_row.items():
                    if col_name in ["TASK_NAME", "TASK NAME", "NAME", "TITLE", "SUBJECT", "TASK"]:
                        task_name = val
                        break
                if not task_name:
                    for col_name, val in normalized_row.items():
                        col = normalized_db_cols.get(col_name)
                        if col and col.data_type == "TEXT" and val:
                            task_name = val
                            break
                if not task_name:
                    task_name = f"Personal Row {row_import_idx}"
                due_date = None
            else:
                if is_sales:
                    task_name = normalized_row.get("CUSTOMER_NAME", "")
                    due_date_str = normalized_row.get("FOLLOW_UP_DATE")
                elif is_logs:
                    task_name = normalized_row.get("TOOL_NAME") or normalized_row.get("TASK_NAME", "")
                    due_date_str = normalized_row.get("RETURN_DATE") or normalized_row.get("DUE_DATE")
                else:
                    task_name = normalized_row.get("TASK_NAME", "")
                    due_date_str = normalized_row.get("DUE_DATE")

                due_date = cls.safe_parse_date(due_date_str) if due_date_str else None

            # Priority parsing
            priority = "MEDIUM"
            for k, v in normalized_row.items():
                if k == "PRIORITY" and v:
                    priority = v
                    break

            status_val = "PENDING"
            for k, v in normalized_row.items():
                if k == "STATUS" and v:
                    status_val = v
                    break

            # Stage Row object creation (unsaved)
            row = Row(table=table, created_by=user)
            rows_to_create.append(row)

            # Auto compute S_NO (using pre-fetched base)
            s_no = base_s_no + row_import_idx
            row_import_idx += 1

            # Parse and normalize other system fields
            csv_date_str = normalized_row.get("DATE") or normalized_row.get("ISSUE_DATE")
            date_val = None
            if csv_date_str:
                parsed_d = cls.safe_parse_date(csv_date_str)
                if parsed_d:
                    date_val = parsed_d.isoformat()
            if not date_val and not is_list_pid:
                date_val = timezone.localdate().isoformat()

            initial_mail_val = normalized_row.get("INITIAL_MAIL", "NO")
            if initial_mail_val:
                initial_mail_val = str(initial_mail_val).strip().upper()
                if initial_mail_val not in ["YES", "NO"]:
                    initial_mail_val = "NO"
            else:
                initial_mail_val = "NO"

            alert_mail_val = normalized_row.get("ALERT_MAIL", "NO")
            if alert_mail_val:
                alert_mail_val = str(alert_mail_val).strip().upper()
                if alert_mail_val not in ["YES", "NO"]:
                    alert_mail_val = "NO"
            else:
                alert_mail_val = "NO"

            if is_sales:
                system_field_names = ["S_NO", "DATE", "FOLLOW_UP_DATE", "CUSTOMER_NAME", "INITIAL_MAIL", "ALERT_MAIL"]
                cell_values = {
                    "S_NO": s_no,
                    "DATE": date_val,
                    "FOLLOW_UP_DATE": due_date.isoformat() if due_date else None,
                    "CUSTOMER_NAME": task_name,
                    "INITIAL_MAIL": initial_mail_val,
                    "ALERT_MAIL": alert_mail_val
                }
            elif is_list_pid:
                system_field_names = ["S_NO", "DATE", "ENQUIRY_NO_QUOTATION_NO", "DUE_DATE_FLOW_FORCE", "INITIAL_MAIL", "ALERT_MAIL"]
                cell_values = {
                    "S_NO": s_no,
                    "DATE": date_val,
                    "ENQUIRY_NO_QUOTATION_NO": enquiry_val,
                    "DUE_DATE_FLOW_FORCE": ff_date.isoformat() if ff_date else None,
                    "INITIAL_MAIL": initial_mail_val,
                    "ALERT_MAIL": alert_mail_val
                }
            elif is_logs:
                system_field_names = [
                    "S_NO", "ISSUE_DATE", "RETURN_DATE", "DAYS_OVERDUE",
                    "TOOL_NAME", "STATUS", "ISSUED_BY", "RECEIVED_BY",
                    "INITIAL_MAIL", "ALERT_MAIL"
                ]
                issue_date_val = cls.safe_parse_date(normalized_row.get("ISSUE_DATE") or normalized_row.get("DATE")) or timezone.localdate()
                return_date_val = due_date or issue_date_val
                status_val = normalized_row.get("STATUS", "Not Returned")
                days_overdue = 0
                if str(status_val).strip().upper() not in ["RETURNED", "COMPLETED"]:
                    if return_date_val and timezone.localdate() > return_date_val:
                        days_overdue = (timezone.localdate() - return_date_val).days

                cell_values = {
                    "S_NO": s_no,
                    "ISSUE_DATE": issue_date_val.isoformat() if hasattr(issue_date_val, 'isoformat') else str(issue_date_val),
                    "RETURN_DATE": return_date_val.isoformat() if hasattr(return_date_val, 'isoformat') else str(return_date_val),
                    "DAYS_OVERDUE": days_overdue,
                    "TOOL_NAME": task_name,
                    "STATUS": status_val,
                    "ISSUED_BY": normalized_row.get("ISSUED_BY", user.full_name or user.email),
                    "RECEIVED_BY": normalized_row.get("RECEIVED_BY", ""),
                    "INITIAL_MAIL": initial_mail_val,
                    "ALERT_MAIL": alert_mail_val
                }
            elif is_personal:
                system_field_names = []
                cell_values = {}
            else:
                system_field_names = ["S_NO", "DATE", "DUE_DATE", "TASK_NAME", "INITIAL_MAIL", "ALERT_MAIL"]
                cell_values = {
                    "S_NO": s_no,
                    "DATE": date_val,
                    "DUE_DATE": due_date.isoformat() if due_date else None,
                    "TASK_NAME": task_name,
                    "INITIAL_MAIL": initial_mail_val,
                    "ALERT_MAIL": alert_mail_val
                }

            # Map remaining custom columns including status, priority, and date columns
            for col_name, val in normalized_row.items():
                if col_name not in system_field_names:
                    if col_name in db_col_names:
                        col = normalized_db_cols[col_name]
                        if is_list_pid and col.name == "DUE_DATE_CUSTOMER":
                            cell_values[col.name] = cust_date.isoformat() if cust_date else None
                        else:
                            cell_values[col.name] = val

            # Normalize priority and status for Task model
            norm_priority = str(priority).upper().strip().replace(" ", "_")
            if norm_priority not in [choice[0] for choice in Task.PRIORITY_CHOICES]:
                norm_priority = "MEDIUM"

            # Resolve assignee user if provided in any USER data_type column or header representation
            user_to_assign = None
            for col_name, val in cell_values.items():
                col = None
                if col_name in system_field_names:
                    col = normalized_db_cols.get(col_name)
                else:
                    col = columns_by_name.get(col_name)

                if col and (col.data_type == "USER" or col.name.upper() in ["ASSIGNED_TO", "ASSIGNED TO", "ASSIGNEE"]):
                    if val:
                        cache_key = str(val).strip()
                        if cache_key in resolved_users_cache:
                            user_to_assign = resolved_users_cache[cache_key]
                        else:
                            try:
                                if str(val).isdigit():
                                    user_to_assign = EmployeeUser.objects.get(id=int(val), is_active=True)
                                elif "@" in str(val):
                                    user_to_assign = EmployeeUser.objects.get(email=val, is_active=True)
                                else:
                                    user_to_assign = EmployeeUser.objects.get(full_name__iexact=val, is_active=True)
                            except EmployeeUser.DoesNotExist:
                                user_to_assign = EmployeeUser.objects.filter(
                                    Q(full_name__icontains=val) | Q(email__icontains=val),
                                    is_active=True
                                ).first()
                            resolved_users_cache[cache_key] = user_to_assign
                        break

            row_temp_data.append({
                'cell_values': cell_values,
                'due_date': due_date,
                'norm_priority': norm_priority,
                'initial_mail_val': initial_mail_val,
                'alert_mail_val': alert_mail_val,
                'user_to_assign': user_to_assign,
                'system_field_names': system_field_names
            })

        # 1. Bulk Create Row records
        created_rows = Row.objects.bulk_create(rows_to_create)

        cells_to_create = []
        tasks_to_create = []

        # 2. Iterate through newly created rows to build unsaved CellValue and Task objects
        for i, row in enumerate(created_rows):
            temp = row_temp_data[i]
            row_cells = temp['cell_values']

            for name, val in row_cells.items():
                col = normalized_db_cols.get(name) or columns_by_name.get(name)
                if col:
                    cells_to_create.append(
                        CellValue(row=row, column=col, value=val, updated_by=user)
                    )

            task = Task(
                row=row,
                due_date=temp['due_date'],
                priority=temp['norm_priority'],
                status="PENDING",
                assigned_by=user,
                initial_mail_sent=(temp['initial_mail_val'] == "YES"),
                alert_mail_sent=(temp['alert_mail_val'] == "YES")
            )
            tasks_to_create.append(task)

        # 3. Bulk insert CellValue and Task objects
        CellValue.objects.bulk_create(cells_to_create, batch_size=5000)
        created_tasks = Task.objects.bulk_create(tasks_to_create)

        # 4. Map task assignees in bulk using join table ThroughModel
        ThroughModel = Task.assigned_to.through
        through_fields = ThroughModel._meta.fields
        task_field = None
        user_field = None
        for f in through_fields:
            if f.is_relation:
                if f.related_model == Task:
                    task_field = f.name
                else:
                    user_field = f.name

        through_objs = []
        for i, task in enumerate(created_tasks):
            temp = row_temp_data[i]
            user_to_assign = temp['user_to_assign']
            assignees = [user_to_assign] if user_to_assign else []

            if is_list_pid:
                assignees = list_pid_employees

            for employee in assignees:
                kwargs = {
                    task_field: task,
                    user_field: employee
                }
                through_objs.append(ThroughModel(**kwargs))

        if through_objs:
            ThroughModel.objects.bulk_create(through_objs, batch_size=5000, ignore_conflicts=True)

        # After successfully importing and creating rows, analyze and setup filter for LIST_PID
        if is_list_pid:
            company_col = table.columns.filter(name__iexact="COMPANY_NAME").first()
            if company_col:
                unique_values = CellValue.objects.filter(
                    column=company_col,
                    row__table=table,
                    row__is_archived=False
                ).exclude(
                    value__isnull=True
                ).exclude(
                    value=""
                ).values_list("value", flat=True).distinct()

                cleaned_values = sorted(list(set(str(v).strip() for v in unique_values if str(v).strip())))

                company_col.data_type = "DROPDOWN"
                company_col.is_filterable = True
                company_col.options = ",".join(cleaned_values)
                company_col.save()

        # Invalidate table statistics cache
        TableStatisticsService.invalidate_cache(table.id)

        # Broadcast batch table import completed event on transaction commit
        TableEventBroadcaster.broadcast_table_import_completed(
            table_id=table.id,
            imported_rows=len(created_rows),
            user=user
        )

        return created_rows, None

    @classmethod
    @transaction.atomic
    def import_from_google_sheet(cls, sheet_url, table, user, header_row=None, data_row=None, check_permissions=False):
        """
        Fetches CSV export from Google Sheets URL and delegates to import_rows_from_csv_data.
        """
        import re
        import urllib.request

        match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", sheet_url)
        if not match:
            return None, "Invalid Google Sheets URL format. Make sure it contains '/spreadsheets/d/[ID]'"

        spreadsheet_id = match.group(1)
        gid_match = re.search(r"[#&?]gid=([0-9]+)", sheet_url)
        if gid_match:
            export_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv&gid={gid_match.group(1)}"
        else:
            export_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv"

        try:
            req = urllib.request.Request(
                export_url,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            with urllib.request.urlopen(req, timeout=15) as response:
                content = response.read().decode('utf-8')
        except Exception as e:
            return None, f"Error fetching Google Sheet: {str(e)}. Ensure the spreadsheet is public or shared 'Anyone with the link can view'."

        return cls.import_rows_from_csv_data(
            file_data=content,
            table=table,
            user=user,
            header_row=header_row,
            data_row=data_row,
            check_permissions=check_permissions
        )
