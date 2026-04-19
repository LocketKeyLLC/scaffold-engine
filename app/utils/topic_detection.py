"""Shared topic detection — decouples callers from gt_extractor private API.

This module is the canonical import path for topic classification. Keeps
gt_extractor's keyword dict as the source of truth while giving other
modules (research_agent, future classifiers) a stable public name.
"""
from app.modules.gt_extractor import _detect_topic_id as detect_topic_id

__all__ = ["detect_topic_id"]
