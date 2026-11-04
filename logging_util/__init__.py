# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structured per-PDF outcome logging for the accessibility pipeline.

Shared by the production entry points (``main.py``, ``app.py``)
and the batch manifest harness so every run emits the same JSONL outcome
record. See ``failure_taxonomy`` for the failure-reason codes and
``outcome_recorder`` for the record builder, JSONL writer, and log helpers.
"""
