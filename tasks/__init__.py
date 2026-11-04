# SPDX-License-Identifier: AGPL-3.0-or-later
"""Task queue infrastructure for the PDF Accessibility Pipeline.

Provides Celery-based async task dispatch with automatic fallback
to synchronous execution when Redis is unavailable.
"""
