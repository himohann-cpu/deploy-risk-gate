# Design: Deploy Risk Gate

## 1. Problem

Teams usually pick one rollout strategy and apply it to everything. If it is
cautious, a one-line copy change waits an hour behind canary steps and people
learn to route around the process. If it is fast, a payment refactor reaches
every user at once.

The fix is to match the caution to the change. That raises two questions that
are normally answered by instinct:

- How risky is this change?
- Is the new version healthy enough to take more traffic?

Both can be answered by rules that a team writes down, reviews and measures.

## 2. Principles

1. **The policy is data.** Weights, tiers, rollout plans and SLO thresholds
   live in one JSON file. Changing how risk is judged is a reviewed change to
   that file.
2. **Every point has a reason.** A score nobody can explain gets overridden.
   Each factor carries its points and a sentence saying why.
3. **A model may explain, never decide.** It cannot change the score, the
   tier, the plan or a gate verdict.
4. **Doubt means wait.** The gate promotes only on evidence of health and
   rolls back only on evidence of harm. Anything else holds for a person.
5. **Unknown is not safe.** A service missing from the dependency map scores
   as unknown reach, not as no reach.

## 3. Decisions

### 3.1 Additive score with named factors

A weighted model fitted to incident history would probably predict better.
It would also be opaque, and it needs data most teams do not have. An additive
table is crude and can be read, challenged and changed by the people it
governs. Fitting the weights is a later step, once the eval has real incidents
to score against.

### 3.2 The riskiest file sets the change kind

A change touching one migration and forty lines of code is a migration. Kinds
are ranked and the highest one present wins, so a dangerous file cannot be
diluted by harmless ones.

### 3.3 Irreversible changes need a person

Automatic rollback is the safety net that makes small canary steps
worthwhile. A schema migration is not undone by shifting traffic back, so the
net is missing. Those changes require approval before starting regardless of
score, and the plan says why.

### 3.4 Compare with stable, not only with a threshold

A fixed threshold ("roll back above 1% errors") fails in the case that
matters most. When a shared dependency goes down, both versions breach
together. Rolling back the canary changes nothing, hides the real cause, and
teaches people the gate cries wolf.

So a breach needs three things: the canary is over the SLO, it is clearly
worse than stable (ratio and margin), and the gap is larger than chance
explains. If stable is failing too, the verdict is `baseline_unhealthy` and
the rollout waits.

### 3.5 No data is not good news

At 1% of a low-traffic service, a five-minute window may hold a handful of
requests. Zero errors in twelve requests says nothing. Below a minimum
request count the gate returns `insufficient_data`, extends the bake, and
then holds. It never promotes because nothing went wrong while nothing was
happening.

### 3.6 Three outcomes for a step

| Evidence | Action |
|---|---|
| Healthy | Promote |
| Harmful | Roll back |
| Unclear | Hold for a person |

Most gates have only the first two, which forces every unclear case into one
of them. The third outcome is what keeps both the false-rollback rate and the
bad-release rate down.

### 3.7 Decide the plan; let existing controllers execute it

Argo Rollouts and Flagger already shift traffic and run analysis. What they
leave open is which strategy a given change should get. This tool answers
that and writes the answer into the manifest they already use.

### 3.8 Edit the existing Rollout; do not replace it

A team's Rollout holds decisions this tool knows nothing about: analysis
templates, traffic routing, header routes. So `apply-plan` changes only the
`setWeight` and `pause` steps and keeps every other step.

Stage counts differ between the old steps and the new plan, so kept steps
cannot be mapped one to one. The rule is conservative: each distinct kept
step runs after every new stage. That may run a check more often than before
and never less.

The command prints a diff by default. Rewriting a deployment manifest is the
kind of action that should be seen before it is trusted.

### 3.9 Two ways to share control with Argo

Either Argo decides promotion with its own analysis (timed pauses), or this
tool does (indefinite pauses, promoted by `deploy-gate run`). Mixing them, with
two systems both able to promote, would make it unclear which one was
responsible for a bad release. The `--gated` flag picks one.

### 3.10 Acting is opt-in, and blind means stop

`run` prints its kubectl commands and changes nothing unless `--execute` is
given, so the gate can be watched against real rollouts first. This is shadow
mode: the first rung of earning autonomy.

When it does act, it treats missing information as a reason to stop. Lost
metrics, or a platform that does not reach the requested weight, hold the
rollout where it is.

### 3.11 Weights learn from outcomes, slowly and in the open

Hand-set weights encode a guess. Rollbacks and incidents are the evidence
that tests it, so the tool keeps a record of outcomes and adjusts.

There are two speeds, because there are two different questions:

- *Is this service fragile right now?* A rollback answers that immediately.
  The next changes to the service score higher for 30 days, with no
  recalculation.
- *Is this kind of change riskier than we assumed?* One rollback cannot answer
  that. It needs a comparison across many rollouts, so it moves slowly.

The slow path is a ratio, not a trained model: the failure rate of changes
carrying a factor, over the failure rate of all changes. That keeps it
readable. Anyone can check a row of the table by counting.

The guards matter more than the formula:

| Risk | Guard |
|---|---|
| Overreacting to one bad rollout | Smoothing toward the overall rate; 10% maximum step |
| Drifting to an extreme over months | Hard bounds at 0.5x and 2x |
| Churning the policy on noise | Deadband; minimum history |
| The 10% limit being bypassed by frequent runs | Automatic learning waits for 10 newly finished rollouts |
| Nobody knowing why a score changed | Learned weights are a file in the repo, and every assessment names the weight it used |

Two limits are stated rather than solved. The method sees correlation, so
factors that travel together share blame. And it assumes failures get
recorded; an unrecorded incident is counted as a success.

Calibration also reports how well the score predicts failure. A learning loop
that only ever reweights can hide a score that predicts nothing, so the
report puts that number in front of the reader each time.

## 4. Autonomy

| Action | Level | Why |
|---|---|---|
| Score a change and post the assessment | Advise | Nothing takes effect |
| Choose the rollout plan | Act within envelope | The envelope is the policy file, reviewed by people |
| Promote a step after a passing gate | Act within envelope | Reversible, and verified by a deterministic check |
| Roll back on a breach | Act within envelope | Reversible; speed matters most here |
| Start a critical or irreversible rollout | Act on approval | A person owns that decision |
| Resolve a held rollout | Not granted | Unclear evidence is exactly when judgement is needed |
| Adjust its own risk weights | Act within envelope, or propose | Bounded, stepped and logged. `--learn` applies them; `calibrate --write` leaves them for review. |

## 5. Evaluation

`deploy-gate eval` runs simulated rollouts with known right answers and
reports:

| Metric | Meaning |
|---|---|
| `bad_releases_stopped` | Bad versions rolled back before full traffic |
| `bad_releases_reaching_100` | Bad versions that completed. Must be zero. |
| `false_rollbacks` | Healthy versions rolled back. Must be zero. |
| `harmed_requests_staged` | Requests that failed or were slow because of a bad version, under the staged plan |
| `harmed_requests_direct` | The same, had the release gone straight to 100% behind the same gate |

The command exits non-zero if any outcome is wrong, so CI catches a policy
change that weakens the gate.

The scenario set includes the cases that break simple gates: a dependency
outage affecting both versions, too little traffic to judge, a fault that
appears only above 50% of traffic, and a healthy release with slightly noisy
errors.

## 6. Limits and next steps

- Replace simulated scenarios with replays of real rollouts, good and bad.
- Run calibration on real rollout history; the shipped history is synthetic.
- Separate the effect of factors that usually appear together.
- Feed incidents into the history automatically from the incident tracker.
- Run the Argo and Istio deployers against a real cluster; today they are
  verified only by the commands they issue.
- Map kept analysis steps to stages by weight instead of repeating them.
- Use an exact test instead of the normal approximation at low error counts.
- Feed the incident count automatically from the incident tracker; today it
  is passed in by hand.
