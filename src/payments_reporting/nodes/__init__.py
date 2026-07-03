"""LangGraph nodes  --  pure functions over GraphState."""

from .analysis import analysis_agent
from .aggregate import aggregate
from .charts import chart_generator
from .dispatch import dispatch_emails
from .email import email_agent
from .ingest import ingest
from .trigger import trigger

__all__ = [
    "analysis_agent",
    "aggregate",
    "chart_generator",
    "dispatch_emails",
    "email_agent",
    "ingest",
    "trigger",
]