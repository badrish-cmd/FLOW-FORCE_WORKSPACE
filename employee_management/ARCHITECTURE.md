# Employee Management Module - Architecture Documentation

## Overview

The Employee Management module is the core HR and personnel administration system for Flow-Force Workspace. It provides comprehensive employee lifecycle management, audit logging, approval workflows, and session tracking.

**Status**: Production Ready  
**Version**: 1.0.0  
**Last Updated**: 2024  

---

## Table of Contents

1. [Module Purpose](#module-purpose)
2. [Data Models](#data-models)
3. [Service Layer](#service-layer)
4. [Views & Request Handling](#views--request-handling)
5. [Admin Interface](#admin-interface)
6. [Signals & Automation](#signals--automation)
7. [URL Routing](#url-routing)
8. [Templates & Frontend](#templates--frontend)
9. [Permissions & Access Control](#permissions--access-control)
10. [Database Schema](#database-schema)
11. [API Endpoints](#api-endpoints)
12. [Testing Strategy](#testing-strategy)

---

## Module Purpose

The Employee Management module handles:

- **Employee CRUD Operations**: Create, read, update, delete employee records
- **Role Management**: Assign and manage user roles (SUPER_ADMIN, ADMIN, DEPARTMENT_ADMIN, EMPLOYEE)
- **Status Workflow**: Manage employee approval status (PENDING → APPROVED/REJECTED)
- **Audit Trail**: Complete activity logging with before/after change tracking
- **Session Management**: Login/logout tracking with duration calculation
- **Profile Management**: Employee profile pictures with version history
- **Bulk Operations**: Perform actions on multiple employees simultaneously
- **Export Functionality**: Export employee data to CSV format
- **Approval Workflow**: Multi-stage approval process for new employees

---

## Data Models

### 1. EmployeeUser (Custom)
**Location**: `auth_app.models.EmployeeUser`

Extends Django's AbstractBaseUser with:
```python
- username: Unique username
- email: Employee email (unique)
- full_name: Full name
- department: ForeignKey to Department
- role: Choice field (SUPER_ADMIN, ADMIN, DEPARTMENT_ADMIN, EMPLOYEE)
- status: Choice field (PENDING, APPROVED, REJECTED)
- is_active: Boolean (account active/inactive)
- is_staff: Boolean (admin access)
- created_at: DateTime
- updated_at: DateTime
```

### 2. ManagedEmployee (Proxy)
**Location**: `employee_management.models.ManagedEmployee`

Proxy model of EmployeeUser for admin interface customization. Provides clean separation for:
- Custom admin actions (approve, reject, activate, deactivate)
- Specialized admin list view with filtering and color coding
- Bulk operations specific to employee management

### 3. EmployeeActivityLog
**Location**: `employee_management.models.EmployeeActivityLog`

Complete audit trail of all employee actions:
```python
- employee: ForeignKey to EmployeeUser
- activity_type: Choice field with 15 types:
    * CREATE: Employee created
    * UPDATE: Employee data updated
    * DELETE: Employee record deleted
    * ACTIVATE: Account activated
    * DEACTIVATE: Account deactivated
    * APPROVE: Employee approved
    * REJECT: Employee rejected
    * ROLE_CHANGE: Role changed
    * DEPARTMENT_CHANGE: Department changed
    * PASSWORD_RESET: Password reset by admin
    * STATUS_CHANGE: Status changed
    * LOGIN: User login
    * LOGOUT: User logout
    * EXPORT: Data exported
    * BULK_ACTION: Bulk operation performed

- description: Human-readable description
- changes: JSONField storing before/after values:
    {
        "field_name": {
            "old": "old_value",
            "new": "new_value"
        }
    }
- performed_by: ForeignKey to EmployeeUser (who performed action)
- ip_address: IP address of request
- user_agent: Browser/client information
- created_at: DateTime of activity

- Indexes:
    - (employee, activity_type)
    - (employee, created_at)
    - (performed_by, created_at)
```

### 4. EmployeeLoginHistory
**Location**: `employee_management.models.EmployeeLoginHistory`

Session tracking for security auditing:
```python
- employee: ForeignKey to EmployeeUser
- login_at: DateTime of login
- logout_at: DateTime of logout (nullable)
- is_active: Boolean (session active/closed)
- ip_address: IP address
- user_agent: Browser information
- session_duration: Calculated field (minutes between login/logout)

- Properties:
    - session_duration: Read-only minutes calculation
```

### 5. EmployeeProfilePicture
**Location**: `employee_management.models.EmployeeProfilePicture`

Profile image management with versioning:
```python
- employee: OneToOneField to EmployeeUser
- image: ImageField (auto-optimized)
- is_current: Boolean (version control)
- uploaded_at: DateTime
- file_size: Integer (bytes)

- Features:
    - Automatic resize to 400x400px
    - RGBA → RGB conversion
    - Quality 85 JPEG compression
    - Version history tracking
    - Automatic old image cleanup
```

### 6. EmployeeApprovalQueue
**Location**: `employee_management.models.EmployeeApprovalQueue`

Approval workflow state management:
```python
- employee: ForeignKey to EmployeeUser
- priority: Choice field:
    * LOW
    * MEDIUM
    * HIGH
    * URGENT
    
- is_approved: Boolean
- submitted_at: DateTime
- reviewed_by: ForeignKey to EmployeeUser (nullable)
- reviewed_at: DateTime (nullable)

- Properties:
    - status: Computed from is_approved flag
    - days_pending: Calculated days since submission
```

---

## Service Layer

**Location**: `employee_management/services.py`

### EmployeeService

Centralized business logic for employee operations:

#### create_employee()
Creates new employee with:
- Full validation
- Automatic EmployeeActivityLog entry (CREATE)
- EmployeeApprovalQueue entry if status=PENDING
- Returns: EmployeeUser instance

#### update_employee()
Updates employee with:
- Change tracking (before/after values)
- Activity logging with specific activity type based on change:
  - ROLE_CHANGE if role modified
  - DEPARTMENT_CHANGE if department modified
  - STATUS_CHANGE if status modified
  - UPDATE for other changes
- Support for partial updates
- Returns: Updated EmployeeUser instance

#### approve_employee()
Approval workflow:
- Sets status to APPROVED
- Sets is_active to True
- Marks EmployeeApprovalQueue.is_approved = True
- Creates activity log (APPROVE)
- Returns: Updated EmployeeUser instance

#### reject_employee()
Rejection workflow:
- Sets status to REJECTED
- Sets is_active to False
- Marks EmployeeApprovalQueue.is_approved = False
- Creates activity log (REJECT)
- Returns: Updated EmployeeUser instance

#### activate_employee() / deactivate_employee()
Activation state management:
- Sets is_active flag
- Creates activity log (ACTIVATE/DEACTIVATE)
- Returns: Updated EmployeeUser instance

#### reset_password_by_admin()
Admin password reset:
- Generates 12-character random password (upper + lower + digits)
- Sets employee password
- Creates activity log (PASSWORD_RESET)
- Returns: Generated password (to send to employee)

#### log_login()
Session creation:
- Creates EmployeeLoginHistory record
- Creates activity log (LOGIN)
- Stores IP address, user agent
- Returns: EmployeeLoginHistory instance

#### log_logout()
Session termination:
- Finds last active EmployeeLoginHistory
- Sets logout_at timestamp
- Calculates session_duration
- Sets is_active to False
- Creates activity log (LOGOUT)
- Returns: Updated EmployeeLoginHistory instance

### EmployeeExportService

**export_employees_to_csv()**
Generates CSV export with columns:
1. Employee ID
2. Full Name
3. Email
4. Department
5. Role
6. Status
7. Active (Yes/No)
8. Joined Date
9. Last Updated

---

## Views & Request Handling

**Location**: `employee_management/views.py`

### Authentication
All views require login via `@login_required` decorator.

### List & Filter Views

#### employee_list()
**URL**: `/employees/`  
**Methods**: GET  
**Permissions**: SUPER_ADMIN, ADMIN, or DEPARTMENT_ADMIN

**Features**:
- Filters by:
  - search: Full name or email (case-insensitive)
  - role: Employee role
  - status: Employee status
  - department: Department (filtered by user permissions)
  - active: Active/inactive status
- Sorting: 8 options (name asc/desc, email, department, joined, status, updated)
- Pagination: 25 items per page
- Context includes stats (total, pending, active, inactive)
- Respects role-based data scoping

#### approval_center()
**URL**: `/employees/approvals/`  
**Methods**: GET  
**Permissions**: DEPARTMENT_ADMIN

**Features**:
- Shows PENDING employees in user's department
- Search by name/email
- Filter by priority (LOW, MEDIUM, HIGH, URGENT)
- Pagination: 20 items per page
- Inline approve/reject actions

### Detail & Edit Views

#### employee_detail()
**URL**: `/employees/<id>/`  
**Methods**: GET  
**Permissions**: Role-based access (can view if manageable)

**Context**:
- Employee data
- 20 recent activity logs
- 10 recent login sessions
- Quick action buttons (if has permission)

#### employee_create()
**URL**: `/employees/create/`  
**Methods**: GET, POST  
**Permissions**: SUPER_ADMIN, ADMIN

**POST Behavior**:
- Creates employee via EmployeeService.create_employee()
- Validates role selection
- Redirects to employee detail on success
- Returns form with errors on validation failure

#### employee_edit()
**URL**: `/employees/<id>/edit/`  
**Methods**: GET, POST  
**Permissions**: Role-based (SUPER_ADMIN, ADMIN, or self for limited fields)

**POST Behavior**:
- Updates via EmployeeService.update_employee()
- Validates role change permissions
- Audits all changes
- Redirects to detail on success

### Action Views

#### employee_approve(), employee_reject()
**URL**: `/employees/<id>/approve/`, `/employees/<id>/reject/`  
**Methods**: POST  
**Permissions**: SUPER_ADMIN, ADMIN, or DEPARTMENT_ADMIN  
**Decorator**: `@require_POST`

**Behavior**:
- Calls EmployeeService method
- Creates activity log
- Updates approval queue
- Redirects to employee detail

#### employee_activate(), employee_deactivate()
**URL**: `/employees/<id>/activate/`, `/employees/<id>/deactivate/`  
**Methods**: POST  
**Permissions**: SUPER_ADMIN, ADMIN  
**Decorator**: `@require_POST`

**Behavior**:
- Calls EmployeeService method
- Logs action
- Redirects to referring page

#### employee_reset_password()
**URL**: `/employees/<id>/reset-password/`  
**Methods**: POST  
**Permissions**: SUPER_ADMIN, ADMIN  
**Decorator**: `@require_POST`

**Behavior**:
- Generates new password via EmployeeService
- Creates activity log
- Returns password for communication to employee

### Bulk Operations

#### bulk_action()
**URL**: `/employees/bulk/action/`  
**Methods**: POST  
**Permissions**: SUPER_ADMIN, ADMIN  
**Decorator**: `@require_POST`

**Features**:
- Supports: activate, deactivate, approve, reject
- Validates employee IDs
- Performs action on multiple employees
- Logs bulk action activity
- Returns count of affected employees

### History Views

#### activity_history()
**URL**: `/employees/activity-history/`  
**Methods**: GET  
**Permissions**: SUPER_ADMIN, ADMIN

**Features**:
- Shows all EmployeeActivityLog entries
- Filter by activity_type
- Pagination: 50 items per page
- Shows collapsible change details

#### login_history()
**URL**: `/employees/login-history/`  
**Methods**: GET  
**Permissions**: SUPER_ADMIN, ADMIN

**Features**:
- Shows all EmployeeLoginHistory entries
- Filter by session status (active/closed)
- Pagination: 50 items per page
- Shows duration in minutes

### Export

#### export_employees()
**URL**: `/employees/export/`  
**Methods**: GET  
**Permissions**: SUPER_ADMIN, ADMIN

**Features**:
- Applies same filters as employee_list
- Generates CSV file
- Logs EXPORT activity
- Returns downloadable CSV attachment

---

## Admin Interface

**Location**: `employee_management/admin.py`

### ManagedEmployeeAdmin
**Model**: ManagedEmployee (proxy)

**Features**:
- Color-coded role badges:
  - Red: SUPER_ADMIN
  - Orange: ADMIN
  - Purple: DEPARTMENT_ADMIN
  - Blue: EMPLOYEE
- Status badges (APPROVED green, PENDING yellow, REJECTED red)
- Active status indicator
- Fieldsets for organization:
  - Personal Information (full_name, email)
  - Role & Status (role, status, is_active)
  - Metadata (created_at, updated_at)
- Bulk actions:
  - Activate selected employees
  - Deactivate selected employees
  - Approve selected employees
  - Reject selected employees
- Search: full_name, email
- List filters: role, status, is_active, department

### EmployeeActivityLogAdmin
**Model**: EmployeeActivityLog

**Features**:
- READONLY (preserves audit integrity)
- No add/edit/delete permissions
- Activity type color-coded badges
- Displays before/after changes
- Search: employee name, activity_type
- List filters: activity_type, created_at, performed_by
- List display: activity_type, employee, description, performed_by, created_at

### EmployeeLoginHistoryAdmin
**Model**: EmployeeLoginHistory

**Features**:
- READONLY
- Session duration calculated display
- Active/closed status badges
- Search: employee, ip_address
- List filters: is_active, login_at
- List display: employee, login_at, logout_at, session_duration, ip_address

### EmployeeProfilePictureAdmin
**Model**: EmployeeProfilePicture

**Features**:
- Image preview (100x100px)
- Current version badge
- Search: employee name
- List display: employee, image_preview, is_current, uploaded_at

### EmployeeApprovalQueueAdmin
**Model**: EmployeeApprovalQueue

**Features**:
- Priority badges (LOW blue, MEDIUM yellow, HIGH orange, URGENT red)
- Status badges (APPROVED green, PENDING yellow)
- Approval actions: approve_from_queue, reject_from_queue
- Search: employee name
- List filters: priority, is_approved
- List display: employee, priority, is_approved, submitted_at, reviewed_by

---

## Signals & Automation

**Location**: `employee_management/signals.py`

### Signal Handlers

#### handle_employee_approval_workflow()
**Triggered**: post_save on EmployeeUser  
**Logic**:
- When employee.status = APPROVED
- Find EmployeeApprovalQueue entry
- Set is_approved = True
- Purpose: Keep approval queue in sync with employee status

#### optimize_profile_picture()
**Triggered**: post_save on EmployeeProfilePicture  
**Logic**:
- Resize image to 400x400px
- Convert RGBA to RGB
- Save as JPEG with quality 85
- Purpose: Optimize storage and display

#### mark_old_pictures_as_archived()
**Triggered**: post_save on EmployeeProfilePicture  
**Logic**:
- When is_current = True
- Set all other pictures for same employee to is_current = False
- Purpose: Maintain single current picture per employee

#### cleanup_profile_picture_file()
**Triggered**: post_delete on EmployeeProfilePicture  
**Logic**:
- Delete physical image file from storage
- Purpose: Prevent orphaned files

---

## URL Routing

**Location**: `employee_management/urls.py`

```python
# List & Filter
path('', views.employee_list, name='employee_list')
path('approvals/', views.approval_center, name='approval_center')

# CRUD
path('create/', views.employee_create, name='employee_create')
path('<int:id>/', views.employee_detail, name='employee_detail')
path('<int:id>/edit/', views.employee_edit, name='employee_edit')

# Actions
path('<int:id>/activate/', views.employee_activate, name='employee_activate')
path('<int:id>/deactivate/', views.employee_deactivate, name='employee_deactivate')
path('<int:id>/approve/', views.employee_approve, name='employee_approve')
path('<int:id>/reject/', views.employee_reject, name='employee_reject')
path('<int:id>/reset-password/', views.employee_reset_password, name='employee_reset_password')

# Bulk Operations
path('bulk/action/', views.bulk_action, name='bulk_action')

# Export
path('export/', views.export_employees, name='export_employees')

# History
path('activity-history/', views.activity_history, name='activity_history')
path('login-history/', views.login_history, name='login_history')

# Deprecated (backward compatibility)
path('<int:id>/<action>/', views.legacy_action, name='legacy_action')
```

---

## Templates & Frontend

**Location**: `templates/employee_management/`

### Main Templates

1. **base.html**
   - Master layout with sidebar navigation
   - Message/alert display
   - Bootstrap 5 + custom CSS
   - JavaScript initialization

2. **employee_list.html**
   - Employee directory with filters
   - Metric cards (total, pending, active, inactive)
   - Search and multi-filter support
   - Bulk action controls
   - Pagination

3. **employee_detail.html**
   - Two-column layout
   - Quick action buttons
   - Recent activity logs (20)
   - Recent login sessions (10)
   - Sticky sidebar with status summary

4. **employee_form.html**
   - Create/edit employee form
   - Bootstrap form styling
   - Per-field error display
   - Form sections for organization

5. **approval_center.html**
   - Pending approvals only
   - Priority and search filtering
   - Inline approve/reject
   - Pagination

6. **activity_history.html**
   - Complete audit trail
   - Activity type filtering
   - Collapsible change details
   - Pagination (50/page)

7. **login_history.html**
   - Session tracking table
   - Session duration calculation
   - Active/closed status filter
   - Summary statistics

### Partial Templates

- **role_badge.html**: Color-coded role display
- **status_badge.html**: Status color indicators
- **activity_badge.html**: Activity type color coding
- **action_dropdown.html**: Reusable action menu

### Styling

**CSS File**: `static/css/employee_management/employee_management.css`

- Bootstrap 5 integration
- Custom color scheme
- Responsive grid layouts
- Badge styling
- Table styling
- Form styling
- Pagination styling
- Animation and transitions

### JavaScript

**JS File**: `static/js/employee_management/employee_management.js`

**Modules**:

1. **EmployeeManagement**
   - Bulk action checkbox management
   - Table row interactions
   - Form validation
   - Filter validation
   - CSV export
   - Print functionality
   - Real-time search

2. **ActivityTimeline**
   - Collapse button handling
   - Relative time display (time ago)
   - Auto-update every minute

---

## Permissions & Access Control

### Role-Based Access Control

**SUPER_ADMIN**
- Full access to all employees
- Can create, edit, delete, approve, reject
- Can reset passwords
- Can export data
- Can view all activity logs
- Can perform bulk actions

**ADMIN**
- Can manage employees in their department
- Can create, edit, delete, approve, reject
- Can reset passwords
- Can export data
- Can view activity logs for their department
- Can perform bulk actions

**DEPARTMENT_ADMIN**
- Can view employees in their department
- Can approve/reject employees in their department
- Can export their department data
- Cannot create new employees (view only)
- Can view activity logs for their department
- Cannot perform bulk actions

**EMPLOYEE**
- Can view own profile
- Cannot access employee management
- Cannot view other employees

### Permission Decorators

```python
@employee_access_required  # Checks if user is SUPER_ADMIN, ADMIN, or DEPARTMENT_ADMIN
@require_POST  # Only allows POST requests (prevents CSRF, enforces state change)
@login_required  # Requires authenticated user
```

### Data Scoping

Views automatically scope data by user role:
- SUPER_ADMIN: See all employees
- ADMIN: See all employees
- DEPARTMENT_ADMIN: See only employees in their department

---

## Database Schema

### Relationships Diagram

```
EmployeeUser (auth_app)
    ↓
    ├── ManagedEmployee (proxy)
    ├── EmployeeActivityLog (many-to-one)
    ├── EmployeeLoginHistory (many-to-one)
    ├── EmployeeProfilePicture (one-to-one)
    └── EmployeeApprovalQueue (one-to-one)

Department
    ↑
    └── EmployeeUser (many-to-one)
```

### Key Indexes

**EmployeeActivityLog**:
- (employee_id, activity_type)
- (employee_id, created_at)
- (performed_by_id, created_at)

**EmployeeLoginHistory**:
- (employee_id, login_at)
- (employee_id, is_active)

**EmployeeApprovalQueue**:
- (employee_id)
- (priority, is_approved)

---

## API Endpoints

### HTTP Methods

- **GET**: Retrieve data (employee_list, employee_detail, *_history views)
- **POST**: Modify data (create, edit, actions, bulk_action, export)

### Response Patterns

**Success (GET)**:
```python
{
    'status': 200,
    'data': {...},
    'message': 'Success'
}
```

**Success (POST)**:
```python
{
    'status': 200,
    'redirect': '/employees/<id>/',
    'message': 'Employee updated successfully'
}
```

**Error (400)**:
```python
{
    'status': 400,
    'errors': {...},
    'message': 'Validation failed'
}
```

**Error (403)**:
```python
{
    'status': 403,
    'message': 'Permission denied'
}
```

---

## Testing Strategy

### Unit Tests

**Models**:
- EmployeeActivityLog: Test activity type choices, JSON changes storage
- EmployeeLoginHistory: Test duration calculation
- EmployeeApprovalQueue: Test priority levels
- EmployeeProfilePicture: Test image optimization

**Services**:
- EmployeeService: Test all CRUD operations
- EmployeeService: Test activity logging
- EmployeeService: Test approval workflow
- EmployeeExportService: Test CSV generation

### Integration Tests

**Views**:
- Test role-based access control
- Test data scoping by department
- Test pagination and filtering
- Test bulk operations
- Test activity logging for each action

**Admin**:
- Test admin actions (approve, reject, activate, deactivate)
- Test bulk admin actions
- Test admin list filtering

### Functional Tests

- Employee creation workflow
- Employee approval workflow
- Employee data export
- Bulk employee operations
- Activity history tracking
- Login history tracking

---

## Performance Considerations

### Query Optimization

- Use `select_related()` for ForeignKey relationships
- Use `prefetch_related()` for reverse relationships
- Index frequently filtered fields
- Pagination (25-50 items per page)

### Caching Strategy

- Cache employee list (5 minutes)
- Cache approval queue (2 minutes)
- Cache permission checks (per request)

### Database Maintenance

- Archive old activity logs (> 1 year)
- Clean up orphaned profile pictures
- Maintain database indexes

---

## Security Considerations

### Data Protection

- Passwords hashed via Django's PBKDF2
- Sensitive fields audit-logged
- IP addresses and user agents recorded
- Activity logs immutable (READONLY in admin)

### Access Control

- Role-based access control (RBAC)
- Row-level security (department scoping)
- CSRF protection on POST requests
- SQL injection protection via ORM

### Audit Trail

- All employee changes logged
- User and timestamp recorded
- Before/after values stored
- IP address and user agent captured

---

## Deployment Notes

### Prerequisites

- Python 3.10+
- Django 4.2+
- PostgreSQL 12+ (production)
- Pillow library (image processing)

### Migration Steps

```bash
python manage.py makemigrations employee_management
python manage.py migrate employee_management
python manage.py createsuperuser
python manage.py collectstatic
```

### Configuration

Set in `settings.py`:
- `EMPLOYEE_LIST_PAGE_SIZE = 25`
- `EMPLOYEE_HISTORY_PAGE_SIZE = 50`
- `PROFILE_PICTURE_MAX_SIZE = 5242880`  # 5MB
- `MEDIA_ROOT` (for image storage)

---

## Future Enhancements

1. **Advanced Analytics**
   - Employee performance dashboards
   - Turnover analysis
   - Department statistics

2. **Workflow Enhancements**
   - Multi-level approval (manager → dept admin → admin)
   - Approval deadline tracking
   - Approval notifications

3. **Integration**
   - Email notifications
   - Slack integration
   - API for third-party systems

4. **Compliance**
   - GDPR data export
   - Data retention policies
   - Compliance reporting

---

## Support & Troubleshooting

### Common Issues

**Issue**: Permission denied when accessing employee  
**Solution**: Check user role and department assignment

**Issue**: Activity log not updating  
**Solution**: Ensure signals are imported in apps.py

**Issue**: Profile picture not resizing  
**Solution**: Verify Pillow library installed

**Issue**: Pagination not working  
**Solution**: Check page_size settings and template pagination blocks

---

## Related Modules

- **auth_app**: Custom user model and authentication
- **task_tracker**: Task management system
- **employee_management**: This module
- **department_management**: Department management (linked)

---

*End of Architecture Documentation*
