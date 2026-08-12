# Pre-launch audit — resume state (2026-08-12, session limit pause)

Written so the audit continues from exactly here after the limit resets
(~8:10pm UTC). Everything below is pushed; nothing lives only in the
session container.

## Where the audit stands

**Fleet (workflow wf_8d12de52-f04):** 6 code reviewers + 3 web
researchers, each reviewer adversarially verified. COMPLETED and cached:
math, fills, accounting, ops reviewers (their full findings are in
`reports/audit_fleet_raw/journal.jsonl` and were applied in commit
3f0f3bb). STILL TO RUN (died on the session limit, resume with
`Workflow({scriptPath: <session>/workflows/scripts/prelaunch-audit-wf_8d12de52-f04.js,
resumeFromRunId: "wf_8d12de52-f04"})` — completed agents replay from
cache): leakage reviewer, launchgap reviewer, verify:math, verify:fills,
verify:accounting, verify:ops, and the three research agents
(venue-rules, geo-server, api-live — these run on **Opus 5** per user
instruction; the model override is in the script, line ~97 only).

**Verifier note:** the four applied reviews were verified by ME against
the code before applying (commit 3f0f3bb); the formal verify agents are
belt-and-braces and may REFUTE something — reconcile any refutation
against what was already applied.

## Applied so far (all pushed)

- 9fbc1bc five-fix batch (lifetime ledger, in-flight opposite-side
  guard, kill flushes pending, cold-start sigma, orphan slots) + tools
  (audit_doubledip, band_report, tape_chainlink).
- 72060b7 halt ladder (streak breaker 6-in-20 → 2h cool-off → half-size
  10-market probe → day kill; $400 backstop) + skip_px knob (OFF).
- 3f0f3bb fleet findings: direction-aware tape caps, windowed claim
  ledger, stale-book fill guard, per-side re-fire gate, oracle hole
  guards (MAX_HOLE_S=5), strike latch grace 3.5s, settle-quality guard +
  voided-market deferral, backfill-on-reconnect (merge), watchdog hoist,
  executable-Down = min(real ask, mirrored Up bid), restart position
  replay + risk-state transition persistence, guarded loops, ENOSPC-safe
  logging, queue-drop instant resync. Harness: 22/22 PASS.

## NOT yet done (the resume worklist)

1. **Scorer follow-ups from the accounting review (code written for none
   of these yet):** close-boundary clustering (cluster on t1 epoch, not
   slug — cross-coin same-window correlation 0.52-0.65 means t=+3.40 is
   an upper bound); voided/50-50 booking at 0.50/share in score_paper
   (run.py side is done); maker-fill fee/meta fix in paper.py; fill_model
   version stamp + exact-duplicate dedup in score_paper; reset_pnl.sh
   unit derivation from RUNNING units.
2. **droplet_setup.sh:** move jsf-edgecheck out of jsf.slice (its 600M
   allowance can push the slice past MemoryMax at 02:30 UTC daily).
3. **Alerting (ops MAJOR, launch blocker for live):** no push/webhook on
   kill/halt/feed-death; probe-kill auto-resumes at UTC midnight with
   nobody told. Design: alert_url in config + OnFailure= units; manual
   re-arm after probe kill in live mode.
4. **Per-side (Up vs Down) EV split** of the validation + calib
   asymmetry check (math reviewer: table says Down side may be -EV at
   high asks; publish the split before live, consider per-side gates).
5. **Remaining fleet agents** (leakage, launchgap, 3 research) + fold
   their findings into this report and reports/findings.md §10.
6. **Awaiting droplet data:** the tape_chainlink 24h table LANDED and
   REFUTED the 9.6 proxy hypothesis — see findings.md §9.7. Chainlink-z
   is BELOW Binance-z on 4/5 coins; both tapes read ~0 on the last 24h
   (edge decay is now a live concern); sol (+2.46 t=+3.51) and doge
   (+2.86 t=+4.11) keep a real edge on the correct series. NEXT: run
   `tape_chainlink.py --hours 24` again for the new HYBRID-z variant
   (replicates the live bot's basis-adjusted Binance spot — the missing
   piece before final judgment), check reports/decay_log.csv, and get
   2-3 days of paper under the strict model (deployed 53cee3c). DO NOT
   size a live launch off the pre-3f0f3bb record; clean subset +2.99c/sh
   is the number, forward expectation +1 to +2c until proven better.
7. **Launch config decision pending evidence:** max_price 0.97 or
   skip_px [0.95, 0.98] (band evidence: (0.95,0.98] -2.57c/sh t=-1.19,
   (0.98,1.0] +0.87c/sh t=+13.9); flat 100sh sizing vs per-band sizing.
8. **findings.md §10** (this audit's record) not yet written.

## Live state on the droplet (as of pause)

Deployed 72060b7 + restarted (halt ladder live; 3f0f3bb NOT yet pulled
— pull + restart_bots.sh on resume). Record: +$2,147 / +3.68c/sh /
t=+3.40 / 630 markets / 27.8h. BTC killed today at -$282 (variance, not
breakage — the new $400 backstop + streak logic won't repeat that trip).
Doubledip: 63.5% of shares flagged as possible re-claims (upper bound);
clean subset +2.99c/sh — comfortably above the ~1.8c Binance tape.

## Preservation

A background loop in the session pushes
`reports/audit_fleet_raw/` (fleet journal + every agent transcript,
including partial in-flight work) every 4 minutes. If the container is
reclaimed during the pause, everything needed to resume is on this
branch.
