"""Declarative base shared by all models."""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Deterministic names that mirror PostgreSQL's own defaults (orders_pkey, stores_wa_jid_key,
# orders_store_id_fkey, ...). CHECK constraints are named "<table>_<name>", so the constraints that
# docs/05 names explicitly keep their exact names (e.g. "courier_matches_status" ->
# "orders_courier_matches_status"). Indexes are always named explicitly in the models.
NAMING_CONVENTION = {
    "ix": "%(table_name)s_%(column_0_N_name)s_idx",
    "uq": "%(table_name)s_%(column_0_N_name)s_key",
    "ck": "%(table_name)s_%(constraint_name)s",
    "fk": "%(table_name)s_%(column_0_name)s_fkey",
    "pk": "%(table_name)s_pkey",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
