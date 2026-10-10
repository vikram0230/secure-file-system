from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    # Every datetime column is TIMESTAMPTZ; naive timestamps make expiry and
    # audit ordering ambiguous across hosts in different zones.
    type_annotation_map: ClassVar[dict[Any, Any]] = {datetime: DateTime(timezone=True)}
