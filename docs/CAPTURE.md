# Capture playbook

What to record, in what order, with what state. Component G2 of the
build scope — turning a working agent into a 3-minute video.

The video treatment ([VIDEO_TREATMENT.md](../../VIDEO_TREATMENT.md) in
the workspace, not this repo) names eight scenes. Five of them need
real screen captures; three are programmatic and already rendered.

## Pre-flight

1. Phoenix Cloud account live with `finpay-support` project visible.
2. Slack workspace with `#sentinel-incidents` channel and Mender app
   wired to the deployed `${MENDER_URL}/api/approve-patch`.
3. Local dev or Cloud Run — both work for capture; local is faster
   for iteration, Cloud Run is more authentic.
4. Screen Studio (or alternative) running, retina capture, mouse
   highlights enabled.

## Stage the demo

```bash
uv run mender stage-demo --phase1 8m --phase2 4m
```

That:

- wipes `.mender/incidents.json`, `prompts/finpay/staging/`, `.live`
- runs FinPay on v1 for 8 minutes of mixed traffic (the green run)
- flips to v2 (the regressed prompt) and runs 4 more minutes (the
  red run)
- scores everything with the LLM judge so Phoenix has annotations

Total wall time ~12–15 min. Phoenix's `finpay-support` project
will show the eval-score line trending green → red across the bump.

## Recording order

Capture each scene live, the order below; the splice in Resolve later.

### Scene 1 — Phoenix dashboard, slow bleed (0:00–0:18)

- Phoenix Cloud → `finpay-support` project → Traces page.
- Set the time window to span both phases of the staged demo (the
  default 1h works; widen if needed).
- Sort or filter so the eval scores column is visible.
- Slow scroll showing green → yellow → red trending.

### Scene 3 — Heartbeat fires (0:32–0:48)

Terminal capture, full-screen monospace.

```bash
# In a clean terminal, with FinPay v2 running:
FINPAY_PROMPT_VERSION=v2 uv run finpay-serve > /tmp/finpay.log 2>&1 &
sleep 4
uv run mender heartbeat --window 30m --target-project finpay-support
```

Capture the cycle from the green `[heartbeat]` banner through the
final structured `[scan]/[cluster]/[status]` report. Trim mid-cycle
silence in post if needed. Aim for the wall-clock to be on a tidy
minute when the heartbeat starts.

### Scene 4 — Detection terminal + chart (0:48–1:14)

Same heartbeat output as Scene 3 (detail shots of `[detect]` lines).

The chart:

- Open `${MENDER_URL}/charts/eval-trend?window=60m`.
- Capture the page; the regression-detected badge should be visible.
- Lower-third overlay is added in Resolve.

### Scene 5 — Investigation & eval (1:14–1:48)

Three-panel split. Capture each panel separately, full-screen, then
composite.

1. **Left**: open the FinPay v2 prompt YAML in your editor with
   `Always assume USD if not specified.` highlighted.
2. **Middle**: a clean terminal running
   `uv run mender investigate --window 30m`. Show the cycle header,
   `[hypothesized]` transition, eval set rows scrolling past.
3. **Right**: the eval-table view. With the investigate run still
   producing results, open `${MENDER_URL}/incidents/<id>`. Capture
   the eval-cases table (PASS/FAIL/RUNNING badges).

The numbers come from the actual run. v2 should produce 4–6 fails
out of 10 on the baseline; the staged eval should pass 8–10.

### Scene 6 — Slack approval (1:48–2:12)

- Slack desktop, `#sentinel-incidents`, light theme for contrast.
- The investigate cycle from Scene 5 should have triggered a card.
- Capture: card arrival, hover Approve, click Approve, wait for
  the confirmation message.

If the card didn't fire (e.g. `SLACK_INCOMING_WEBHOOK` was empty),
manually re-fire with: `uv run mender notify <incident_id>`.

### Scene 8 — Recovery (2:32–3:00)

After clicking Approve in Scene 6:

```bash
# Restart FinPay on the promoted prompt version.
FINPAY_PROMPT_VERSION=v3 uv run finpay-serve > /tmp/finpay.log 2>&1 &
# Drive a few minutes of traffic on the recovered prompt.
uv run finpay-traffic --duration 4m --currency-bias 0.7
# Score it.
uv run mender score-finpay --window 10m
```

Then re-capture Phoenix's dashboard for the recovered window —
freshly green eval scores in the most recent ~5 minutes.

## Common reshoots

- **Chart axis labels overlap**: widen browser to 1280+, Chart.js
  re-renders responsive.
- **Heartbeat cycle takes too long**: drop `--window` to 15m so the
  agent reasons over fewer traces.
- **v2 regression too subtle**: re-run `stage-demo --phase2 6m` to
  generate more red traces and a sharper line drop.
- **Slack card layout cramped**: zoom Slack to 110% before capturing.

## Asset deliverables

When all scenes are captured, drop them into the workspace:

```
Google Cloud Rapid Agent Hackathon/
├── captures/
│   ├── scene1_phoenix_dashboard.mov
│   ├── scene3_heartbeat_terminal.mov
│   ├── scene4_terminal_chart.mov
│   ├── scene5_left_prompt.mov
│   ├── scene5_middle_terminal.mov
│   ├── scene5_right_evaltable.mov
│   ├── scene6_slack.mov
│   └── scene8_phoenix_recovered.mov
```

The Cowork side of the workspace handles compositing in Resolve.
