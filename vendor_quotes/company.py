"""Which Flow Force company (Zoho Books organisation) a piece of work belongs to."""
import contextvars
from contextlib import contextmanager

FFI = "FFI"
FFE = "FFE"
CHOICES = [(FFI, "FFI - PT. Flow Force Indonesia"), (FFE, "FFE - PT. Flow Force Engineering")]
NAMES = {FFI: "PT. Flow Force Indonesia", FFE: "PT. Flow Force Engineering"}

_current = contextvars.ContextVar("flowforce_company", default=FFI)


def current():
    return _current.get()


def clean(code):
    code = (code or "").strip().upper()
    return code if code in NAMES else FFI


@contextmanager
def using(code):
    """All Zoho Books calls inside this block go to that company's connection and organisation."""
    token = _current.set(clean(code))
    try:
        yield
    finally:
        _current.reset(token)
