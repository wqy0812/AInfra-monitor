"""Publish fixed-window native records through the direct VM datasource."""
from native_records import apply
NAME='perses-accelerated'  # Identify the datasource to retire in older release journals.


def published(resources, state=None):
    return apply(resources)
