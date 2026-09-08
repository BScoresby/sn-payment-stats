# Stacker News payment statistics collector

This small project downloads public aggregate activity data from the Stacker
News GraphQL API, keeps reproducible daily payment and reward datasets, and
creates weekly JSON summaries designed for analysis by ChatGPT or another LLM.

It uses only Python's standard library. There are no API keys, paid services,
or Python packages to install.

## What it collects

- `zap_actions`: `ZAP` action groups reported by `itemGrowth`
- `zap_sats`: zap-denominated value reported by `spendingGrowth`
- `daily_unique_zappers`: distinct users in the daily `ZAP` bucket reported by
  `spenderGrowth`
- `daily_unique_spenders`: distinct users across tracked paid-action types in
  the daily bucket
- `tracked_paid_actions`: sum of the action-group counts returned by
  `itemGrowth`
- `content_items_created`: `ITEM_CREATE` action groups
- complete per-type action, spending, and spender breakdowns

It also collects completed reward information:

- `reward_pool_sats`: source-funded pool shown on the historical rewards page
- `reward_distributed_sats`: `REWARD` payouts credited to stackers
- `daily_unique_reward_recipients`: distinct reward recipients in that daily bucket
- raw reward funding sources in millisats, including `DOWN_ZAP`, `BOOST`, `ZAP`,
  `ITEM_CREATE`, and `DONATE`

Reward records contain both `reward_date` and `source_activity_date`. A reward
distributed at midnight Central was funded by activity during the preceding
America/Chicago calendar day.

The collector deliberately does **not** call zap actions "Lightning
transactions." The public aggregates do not establish which actions settled as
real Lightning payments rather than involving Cowboy Credits.

## Run it on Ubuntu

You need Python 3.9 or newer (Ubuntu 22.04 or newer already has a suitable
version).

```bash
cd sn-payment-stats
python3 scripts/run_pipeline.py
```

That one command:

1. refreshes the most recent daily observations;
2. refreshes completed reward payouts and their funding-source breakdowns;
3. saves the complete API responses for provenance;
4. updates `data/daily.json` and `data/rewards_daily.json`;
5. rebuilds the payment and reward weekly summaries; and
6. writes the latest completed weeks to `data/latest_week.json` and
   `data/latest_rewards_week.json`.

On the first run it requests 90 days. Later runs re-request the most recent 35
payment days and three reward dates, allowing late corrections in Stacker News
aggregates to repair the local history automatically. Historical reward details
are requested in small batches of no more than seven dates.

Useful options:

```bash
# Refresh a specific lookback window (7-119 days)
python3 scripts/run_pipeline.py --days 60

# Display requests without changing files
python3 scripts/collect.py --dry-run

# Rebuild summaries without accessing the internet
python3 scripts/summarize.py

# Run the tests
python3 -m unittest discover -s tests -v
```

## Put it in your GitHub account

The shortest route is to install GitHub's command-line tool, then run the
included setup helper:

```bash
sudo apt update
sudo apt install -y git gh
cd sn-payment-stats
bash scripts/setup_github.sh
```

It opens GitHub's browser login if needed, creates a public repository named
`sn-payment-stats`, and pushes the project. To choose a different name, supply
it as the first argument:

```bash
bash scripts/setup_github.sh my-sn-stats
```

If you prefer to do those operations manually, create an empty public
repository on GitHub and run:

```bash
git init
git add .
git commit -m "Add Stacker News stats collector"
git branch -M main
git remote add origin https://github.com/YOURNAME/sn-payment-stats.git
git push -u origin main
```

If Git asks for a password, use a GitHub personal access token or authenticate
first with `gh auth login`; GitHub does not accept account passwords for Git
operations over HTTPS.

The included workflow at `.github/workflows/collect.yml` runs every day at
10:17 UTC (4:17 a.m. or 5:17 a.m. in Chicago, depending on daylight saving
time), rebuilds the data, and commits changed files. You can also run it at any
time from **Actions → Collect SN stats → Run workflow**.

## Ask ChatGPT to analyze it

After the GitHub workflow has run, give ChatGPT the raw URLs for these files:

```text
https://raw.githubusercontent.com/YOURNAME/sn-payment-stats/main/data/latest_week.json
https://raw.githubusercontent.com/YOURNAME/sn-payment-stats/main/data/weekly.json
https://raw.githubusercontent.com/YOURNAME/sn-payment-stats/main/data/latest_rewards_week.json
https://raw.githubusercontent.com/YOURNAME/sn-payment-stats/main/data/rewards_weekly.json
https://raw.githubusercontent.com/YOURNAME/sn-payment-stats/main/data/metric_definitions.json
```

Suggested prompt:

> Read `latest_week.json`, `weekly.json`, `latest_rewards_week.json`,
> `rewards_weekly.json`, and `metric_definitions.json`. Find the five most
> interesting developments in the latest completed week. Compare it with the
> previous week, the trailing four-week average, and historical records. Explain
> what changed and suggest three accurate social-media angles. Respect every
> metric caveat, and do not describe zap actions or rewards as Lightning
> transactions.

## Files

```text
data/
  daily.json                 canonical daily observations
  weekly.json                completed Monday-Sunday summaries
  latest_week.json           latest completed week plus LLM guidance
  rewards_daily.json         canonical daily reward observations
  rewards_weekly.json        completed reward-date weekly summaries
  latest_rewards_week.json   latest completed reward week plus LLM guidance
  metric_definitions.json    meanings and wording caveats
  raw/
    latest_response.json     latest complete GraphQL response
    history/*.json           responses keyed by collection date and request range
    rewards/                 reward growth and historical detail responses
scripts/
  collect.py                 API client, parsing, validation, and persistence
  collect_rewards.py         reward totals, recipients, sources, and validation
  summarize.py               weekly calculations
  summarize_rewards.py       weekly reward calculations
  run_pipeline.py            one-command wrapper
  setup_github.sh            optional GitHub setup helper
tests/                       offline unit tests
```

## Important limitations

- Daily unique zappers cannot be added together to obtain weekly unique
  zappers. One person active on multiple days would be counted multiple times.
- `zap_sats` is zap-denominated value. It should not automatically be described
  as value settled over Lightning.
- `content_items_created` is the API's `ITEM_CREATE` action-group count. It is a
  useful content-activity proxy but is not independently verified as a count of
  distinct posts plus comments.
- `reward_pool_sats` is the pool reported by the historical rewards page, while
  `reward_distributed_sats` is the resulting aggregate of `REWARD` payouts. The
  two can differ slightly and are preserved separately.
- Daily reward-recipient counts cannot be added together to claim weekly unique
  recipients. Weekly summaries call their sum `reward_recipient_days`.
- Reward-source values are stored in millisats exactly as returned by the API.
  `BOOST` is only the portion assigned to rewards, not gross boost spending.
- Reward payouts are custodial SN reward sats, not proof of Lightning settlement.
- Stacker News can change its public API. The collector fails rather than
  silently accepting structurally invalid responses and archives the raw
  response needed to investigate changes.

## Data provenance

The implementation was checked against the Stacker News public GraphQL schema,
the live API, and the upstream growth resolver at commit
`d4aaf0ac28e3a1cd2b2b6ed0fbd3647c0e1a5422` (checked 2026-09-02). The resolver
derives spending from `sumMcost / 1000`, action counts from `countGroup`, and
spender counts from `countUsers` for global data. It uses America/Chicago bucket
boundaries and daily buckets for requested ranges of 7 through 119 days. Reward
collection uses the public `rewards`, `stackingGrowth`, and `stackerGrowth`
queries from the same deployed upstream commit.

This project is independent of Stacker News and uses its public API.
