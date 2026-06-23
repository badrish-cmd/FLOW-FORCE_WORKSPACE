# Task Tracker Architecture

## 1. Architecture

The task tracker is implemented as a dynamic, database-driven tracker engine layered on top of Django.

- Presentation layer: Django templates with Bootstrap 5 for tracker dashboard, spreadsheet board, kanban board, calendar, timeline, task detail, and task forms.
- View layer: class-based or function-based Django views that only orchestrate request parsing, permissions, filtering, and rendering.
- Service layer: business logic for column management, row lifecycle, task mail automation, escalation, audit events, and reporting.
- Data layer: normalized dynamic tracker tables that store tracker definitions, columns, rows, cells, comments, attachments, notifications, history, and saved filters.

## 2. Database Design

### Core tracker entities
- `Tracker`: a logical tracker such as Engineering Daily Tracker or Sales Tracker.
- `TrackerColumn`: dynamic columns defined per tracker.
- `TrackerRow`: one task record.
- `TrackerCell`: value storage for dynamic custom columns.
- `TaskAssignment`: explicit primary/secondary/watcher assignment records.
- `TaskFilter`: saved filter presets for reusable tracker views.

### Operational entities
- `TaskComment`: threaded comments and internal notes.
- `TaskAttachment`: uploaded files with version-ready metadata.
- `TaskHistory`: immutable audit entries for lifecycle changes.
- `Notification`: in-app notification records for assignment, alerts, and escalations.

### Core task fields
- `priority`: supported as a first-class row attribute with values from LOW to CRITICAL.
- `status`: supports the standard workflow plus CANCELLED, with room for future custom statuses.

## 3. Relationships

- A `Tracker` has many `TrackerColumn` records.
- A `Tracker` has many `TrackerRow` task rows.
- A `TrackerRow` has many `TrackerCell` records, one per custom column.
- A `TrackerRow` has many `TaskComment`, `TaskAttachment`, `TaskHistory`, and `Notification` records.
- `TaskComment` supports replies through a self-referential parent link.

## 4. Permissions

- `SUPER_ADMIN`: full access to all trackers, rows, columns, filters, exports, and audits.
- `ADMIN`: full tracker management access and organization-wide reporting.
- `DEPARTMENT_ADMIN`: access limited to the user’s department tracker scope.
- `EMPLOYEE`: view and update only assigned tasks, with comment and attachment participation where allowed.

## 5. APIs and URLs

Phase 1 endpoints should cover:
- Tracker dashboard and tracker detail pages.
- Spreadsheet, kanban, calendar, timeline, my-tasks, and overdue views.
- Column management actions.
- Task create, edit, status update, bulk update, bulk delete, bulk complete, and due-date change.
- Comment creation, attachment upload, search, and filter application.
- Import and export jobs for CSV, Excel, and PDF.

## 6. UI Structure

- Dashboard: summary widgets and tracker cards.
- Spreadsheet view: sheet-like grid with sticky fixed columns.
- Kanban view: grouped by status with drag-and-drop later.
- Calendar view: due-date centric cards grouped by day.
- Timeline view: date-range lanes for project-style tracking.
- My Tasks and Overdue views: personal execution queues.
- Task detail: row metadata, comments, attachments, and audit trail.
- CSV import/export: spreadsheet round-tripping for tracker data, with Excel/PDF reserved for the next iteration.

## 7. Implementation Phasing

1. Phase 1: tracker views, filters, search, bulk actions, and export/import scaffolding.
2. Phase 2: employee management enhancements.
3. Phase 3: dynamic department management.
4. Phase 4: project management support.
5. Phase 5: notification center.
6. Phase 6: reporting and analytics.
7. Phase 7: future module compatibility without schema redesign.
