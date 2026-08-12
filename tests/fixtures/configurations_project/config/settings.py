"""Settings held in class bodies, the django-configurations way.

Never imported -- djaudit only parses it.

The point of this fixture is that a class per environment is a *module* per
environment, so the severity grading has to work on class names exactly as it
works on file names. `Dev` deliberately contains the same three lines that make
`Prod` critical, and every one of them must stay silent.
"""

from configurations import Configuration, values


class Base(Configuration):
    """Shared defaults. Reaches production, so it is graded as production."""

    # Control for DJS-003: a secret that is *required* from the environment and
    # has no default at all is the pattern the library exists to provide. A
    # rule that matched on the name alone would report the correct answer.
    SECRET_KEY = values.SecretValue()

    DEBUG = values.BooleanValue(False)
    ALLOWED_HOSTS = values.ListValue(["example.com"])

    INSTALLED_APPS = [
        "django.contrib.admin",
        "django.contrib.auth",
        "django.contrib.contenttypes",
        "django.contrib.sessions",
        "django.contrib.messages",
        "django.contrib.staticfiles",
    ]

    MIDDLEWARE = [
        "django.middleware.security.SecurityMiddleware",
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.middleware.common.CommonMiddleware",
        "django.middleware.csrf.CsrfViewMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "django.contrib.messages.middleware.MessageMiddleware",
        "django.middleware.clickjacking.XFrameOptionsMiddleware",
    ]

    ROOT_URLCONF = "config.urls"
    WSGI_APPLICATION = "config.wsgi.application"

    # Controls for DJS-004, DJS-021 and DJS-022. A dict literal nested inside a
    # class body has to resolve as well as one at module level, or the whole
    # DATABASES family goes quiet on every project using this library.
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "app",
            "USER": "app",
            "PASSWORD": values.SecretValue(environ_name="DATABASE_PASSWORD"),
            "HOST": "db.internal",
            "PORT": "5432",
            "CONN_MAX_AGE": 60,
            "OPTIONS": {"sslmode": "verify-full"},
        }
    }

    AUTH_PASSWORD_VALIDATORS = [
        {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
        {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
        {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
        {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
    ]

    PASSWORD_HASHERS = ["django.contrib.auth.hashers.Argon2PasswordHasher"]

    # Controls for DJS-006 and DJS-011: on by default, and still on when the
    # environment is what supplies them. Reading `DJANGO_SECURE_SSL_REDIRECT`
    # does not make the shipped default unknown.
    SECURE_SSL_REDIRECT = values.BooleanValue(True)
    SECURE_HSTS_SECONDS = values.IntegerValue(31536000)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    CSRF_COOKIE_HTTPONLY = True
    X_FRAME_OPTIONS = "DENY"
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    SESSION_EXPIRE_AT_BROWSER_CLOSE = True
    CSRF_COOKIE_SAMESITE = "Strict"
    SESSION_COOKIE_SAMESITE = "Strict"
    DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
    USE_TZ = True
    STATIC_URL = "static/"
    LANGUAGE_CODE = "en-us"
    TIME_ZONE = "UTC"

    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_USE_TLS = True
    EMAIL_HOST = "smtp.example.com"


class Dev(Base):
    """Local development. Every line here is a control, not a defect.

    `DEBUG = True` here and `SESSION_COOKIE_SECURE = False` here are the same
    lines that are reported as critical and high in `Prod` below. A tool that
    cannot tell the two classes apart reports all of them, and is uninstalled
    by every project that uses this library the way its documentation
    recommends.
    """

    DEBUG = True
    ALLOWED_HOSTS = ["*"]
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False
    SECURE_HSTS_SECONDS = 0
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"


class Prod(Base):
    """Production. The planted defects live here."""

    # DJS-003: a fallback secret really is shipped, and is what runs whenever
    # the environment does not override it. Overriding the base's SecretValue
    # is also what proves the subclass wins.
    SECRET_KEY = values.Value("django-insecure-9f3b2a1c8e7d6")

    # DJS-009: sessions travel in the clear.
    SESSION_COOKIE_SECURE = False

    # DJS-007: HSTS is switched back off.
    SECURE_HSTS_SECONDS = 0
