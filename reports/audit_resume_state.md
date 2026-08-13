# Pre-launch audit — CLOSED 2026-08-12 (see findings.md section 10)

The fleet completed: 6 reviewers (48 findings), 6 adversarial verifiers,
3 Opus researchers, 0 errors. Every applied fix was independently
re-verified. findings.md section 10 is the closing record: what was
fixed, the research verdicts (V2 API migration, taker rebates, new
coins, the NL geo risk), the outstanding worklist, and the launch
posture. This file now only tracks what remains OPEN:

1. Written Polymarket support answer on the tier-3 API carve-out and
   KYC jurisdiction (LAUNCH BLOCKER -- decides the foundation).
2. Live executor on polymarket-client / CTF Exchange V2 (launchgap
   review in audit_fleet_raw is the spec).
3. Alerting layer; scorer follow-ups (close-boundary clustering, voided
   booking, maker fee/meta, fill_model stamp); per-side gate decision;
   calib recalibration.
4. Evidence accumulating on its own: strict-model paper days (bots on
   f190f12 lineage since 2026-08-12 21:09 UTC), nightly
   reports/hybrid_decay.csv rows from 02:30 UTC, band_report.py
   --since for the skip_px and per-side decisions.
5. Expansion once confirmed: bnb/hype/zec, maker-side rebate variant,
   per-band sizing.

## Preservation

A background loop in the session pushes
`reports/audit_fleet_raw/` (fleet journal + every agent transcript,
including partial in-flight work) every 4 minutes. If the container is
reclaimed during the pause, everything needed to resume is on this
branch.
