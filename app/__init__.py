"""Broadside -- a self-hosted cross-poster for Bluesky and Mastodon.

This package holds the whole application:

    config    -- load/save the JSON config; the one place secrets are read
    errors    -- the shared ApiError taxonomy (rejected vs unreachable, etc.)
    facets    -- Bluesky URL link-facet computation (UTF-8 byte offsets)
    bluesky   -- AT Protocol client (sessions, blobs, records, threading)
    mastodon  -- Mastodon client (verify, instance limits, async media, statuses)
    posting   -- serial fan-out orchestration with the retry policy
    logstore  -- append-only per-attempt history
    server    -- the Flask app, routes, and JSON API

See broadside-spec.md for the full specification this implements.
"""
