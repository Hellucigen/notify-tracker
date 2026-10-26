"""toastcrumbs -- recover Windows notification history from wpndatabase.db."""

from .carve import carve_all, carve_file
from .database import DatabaseError, open_copy, read_handlers, read_notifications
from .model import Handler, Notification, from_filetime
from .toast import PayloadError, parse_payload

__version__ = "0.1.0"

__all__ = [
    "DatabaseError",
    "Handler",
    "Notification",
    "PayloadError",
    "carve_all",
    "carve_file",
    "from_filetime",
    "open_copy",
    "parse_payload",
    "read_handlers",
    "read_notifications",
    "__version__",
]
