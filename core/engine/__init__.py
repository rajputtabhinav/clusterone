"""Async engine: a dedicated asyncio event loop in its own thread.

Hosts the network scanner and the update job runner (Phase 2+). Results cross
back to the Qt GUI thread via signals using queued connections.
"""
