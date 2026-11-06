---
name: nested-metadata-note
description: The staging bucket rotates its keys every Tuesday at noon
metadata:
  type: feedback
  modified: 2026-10-14T09:30:00Z
---
Rotate the client credentials before Tuesday, or the nightly export fails with a 403.

**Why:** the export runs at 01:00 and reads the old key.
