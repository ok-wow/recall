# Fixtures

`no_substance_corpus/` — 13 real sessions the substance gate parked as
non-substantive, reduced to STRUCTURE ONLY: record type, role, and content-block
type. No conversation text of any kind. That is the entire set of fields
`transcript_substance.py` reads, and the reduction was verified equivalent —
all 13 produce byte-identical counts and exit codes stripped vs original.

They are kept as files rather than as pointers into a live transcript directory
because transcripts age off disk. A corpus that lives only as a path is one that
quietly becomes empty, which is the failure the corpus exists to catch.

Every one is 1–2 user turns and ZERO tool calls, yet 95–140 KB on disk. That gap
is the whole reason the gate counts turns instead of bytes.
