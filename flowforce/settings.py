"""
Django settings for flowforce project.
"""

from pathlib import Path
import os
from dotenv import load_dotenv

# --------------------------------------------------
# BASE DIRECTORY
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from .env file if it exists
load_dotenv(os.path.join(BASE_DIR, '.env'))

# --------------------------------------------------
# SECURITY
# --------------------------------------------------

SECRET_KEY = os.getenv('SECRET_KEY', 'django-insecure-your-secret-key-change-in-production')

DEBUG = os.getenv('DEBUG', 'True') == 'True'

allowed_hosts_env = os.getenv('ALLOWED_HOSTS', '*')
if allowed_hosts_env == '*':
    ALLOWED_HOSTS = ['*']
else:
    ALLOWED_HOSTS = [host.strip() for host in allowed_hosts_env.split(',') if host]

# --------------------------------------------------
# APPLICATIONS
# --------------------------------------------------

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Local Apps
    'auth_app',
    'employee_management',
    'task_tracker',
    'rest_framework',
    'tables',
    'tasks',
]

# --------------------------------------------------
# MIDDLEWARE
# --------------------------------------------------

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# --------------------------------------------------
# URLS
# --------------------------------------------------

ROOT_URLCONF = 'flowforce.urls'

# --------------------------------------------------
# TEMPLATES
# --------------------------------------------------

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',

        'DIRS': [
            os.path.join(BASE_DIR, 'templates')
        ],

        'APP_DIRS': True,

        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'tasks.context_processors.global_context',
            ],
        },
    },
]

# --------------------------------------------------
# WSGI
# --------------------------------------------------

WSGI_APPLICATION = 'flowforce.wsgi.application'

# --------------------------------------------------
# DATABASE
# --------------------------------------------------

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# --------------------------------------------------
# CUSTOM USER MODEL
# --------------------------------------------------

AUTH_USER_MODEL = 'auth_app.EmployeeUser'

# --------------------------------------------------
# PASSWORD VALIDATION
# --------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME':
        'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME':
        'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME':
        'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME':
        'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# --------------------------------------------------
# INTERNATIONALIZATION
# --------------------------------------------------

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True

# --------------------------------------------------
# STATIC FILES
# --------------------------------------------------

STATIC_URL = '/static/'

STATICFILES_DIRS = [
    os.path.join(BASE_DIR, 'static')
]

STATIC_ROOT = os.path.join(
    BASE_DIR,
    'staticfiles'
)

# --------------------------------------------------
# MEDIA FILES
# --------------------------------------------------

MEDIA_URL = '/media/'

MEDIA_ROOT = os.path.join(
    BASE_DIR,
    'media'
)

# --------------------------------------------------
# DEFAULT PK
# --------------------------------------------------

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# --------------------------------------------------
CSRF_TRUSTED_ORIGINS = [
    'http://localhost:8000',
    'http://127.0.0.1:8000',
    'https://flowforceworkspace.cloud',
    'https://www.flowforceworkspace.cloud',
]

SITE_URL = os.getenv('SITE_URL', 'https://flowforceworkspace.cloud')

# --------------------------------------------------
# LOGIN SETTINGS
# --------------------------------------------------

LOGIN_URL = '/login/'

LOGIN_REDIRECT_URL = '/'

LOGOUT_REDIRECT_URL = '/login/'

# SESSION SETTINGS
# Keep sessions active for 10 years (forever until logged out)
SESSION_COOKIE_AGE = 10 * 365 * 24 * 60 * 60  # 315360000 seconds
SESSION_EXPIRE_AT_BROWSER_CLOSE = False


# EMAIL CONFIGURATION

EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'

EMAIL_HOST = 'smtp.gmail.com'
EMAIL_PORT = 587

EMAIL_USE_TLS = True

EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', 'operations.flowforce@gmail.com')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', 'dnqq hseq ubdh zear')

DEFAULT_FROM_EMAIL = f'Flow-Force Workspace <{EMAIL_HOST_USER}>'

# CELERY SETTINGS
from celery.schedules import crontab

import sys
CELERY_TASK_ALWAYS_EAGER = os.getenv('CELERY_TASK_ALWAYS_EAGER', 'False') == 'True' or 'test' in sys.argv
CELERY_BROKER_URL = 'redis://localhost:6379/0'
CELERY_RESULT_BACKEND = 'redis://localhost:6379/0'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_TIMEZONE = 'UTC'
CELERY_BEAT_SCHEDULE = {
    'send-daily-alert-mails': {
        'task': 'tasks.tasks.send_daily_alert_mails',
        'schedule': crontab(hour=8, minute=0),  # 08:00 AM daily
    },
    'check-overdue-escalations': {
        'task': 'tasks.tasks.check_overdue_escalations',
        'schedule': crontab(hour=10, minute=0),  # 10:00 AM daily
    },
    'retry-failed-emails': {
        'task': 'tasks.tasks.retry_failed_emails',
        'schedule': crontab(minute=0),  # Every hour
    },
}

# --------------------------------------------------
# SQLITE WAL MODE CONCURRENCY OPTIMIZATION
# --------------------------------------------------
from django.db.backends.signals import connection_created
from django.dispatch import receiver

@receiver(connection_created)
def configure_sqlite(sender, connection, **kwargs):
    if connection.vendor == 'sqlite':
        cursor = connection.cursor()
        cursor.execute('PRAGMA journal_mode=WAL;')
        cursor.execute('PRAGMA synchronous=NORMAL;')
        cursor.execute('PRAGMA busy_timeout=10000;')  # 10s busy timeout

