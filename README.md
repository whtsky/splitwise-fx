# splitwise-fx

Bulk currency conversion for Splitwise expenses.

Pulls a Splitwise group's expenses, converts every foreign-currency line to a target currency at the **UnionPay transaction-date rate** (the rate Chinese banks actually used to settle), and writes the converted values back via the Splitwise API.

## Install

```bash
uv tool install splitwise-fx
```

(Or `pipx install splitwise-fx`, or any other tool that installs Python CLIs into isolated venvs.)

After install, `splitwise-fx` is on your `PATH`.

## Setup

Get a Splitwise API key from <https://secure.splitwise.com/apps> and export it:

```bash
export SPLITWISE_API_KEY=your-key-here
```

(Add to `~/.zshrc` / `~/.bashrc` to persist. You can also pass `--api-key` per-invocation, or drop a `.env` file in the directory you run from.)

## Usage

```bash
# preview, then prompt y/N to apply
splitwise-fx --group "东京旅行" --to CNY

# preview only — exit without prompting
splitwise-fx --group "东京旅行" --to CNY --dry-run

# skip the prompt (automation)
splitwise-fx --group "东京旅行" --to CNY --yes

# date window
splitwise-fx --group "东京旅行" --to CNY --after 2026-04-01 --before 2026-05-01

# bypass the on-disk rate cache
splitwise-fx --group "东京旅行" --to CNY --no-cache
```

`--group` accepts either the group's name (case-insensitive) or its numeric ID. If the name is ambiguous or missing, the CLI lists matches and exits.

## How it works

1. Pulls expenses from the chosen group, paginating Splitwise's API.
2. For each foreign-currency expense, looks up that day's rate.
3. Skips settle-up payments and same-currency lines.
4. Converts cost + each user's `paid_share` and `owed_share` using `Decimal`. Distributes ±0.01 rounding residuals to the largest non-zero shares so `sum(paid) == sum(owed) == cost` holds (Splitwise rejects updates that violate this).
5. Renders a Rich preview with rate source per line (`UPI` or `FRA`).
6. On confirm, posts each update via `POST /update_expense/{id}`, sleeping 1 s between writes.

### Rate sources

| Source | When used | Notes |
|---|---|---|
| UnionPay (`unionpayintl.com/upload/jfimg/{YYYYMMDD}.json`) | Target currency is one of: AUD CAD CNY EUR GBP HKD HUF JPY MNT MOP NZD SGD THB USD VND | Files exist for every day including weekends/CN holidays (Friday rate carries through). |
| Frankfurter (`api.frankfurter.dev/v2/rate/{src}/{dst}`) | Target outside UnionPay's 15 base currencies, or pair missing from UnionPay file | Free, ECB-backed. |

Rates are cached on disk under `${XDG_CACHE_HOME:-~/.cache}/splitwise-fx/`. Past dates never expire (historical rates are immutable); today's date refreshes every hour.

## Caveats

- **Idempotency**: re-running with a different `--to` after a first conversion will re-convert already-converted expenses. The filter only skips lines whose `currency_code` already matches the target.
- **Settle-up payments are skipped** — converting them would corrupt the ledger.
- **Splitwise rate limits are undocumented**; the CLI sleeps 1 s between writes and backs off on 429s.

## Development

```bash
git clone https://github.com/whtsky/splitwise-fx
cd splitwise-fx
uv sync

# linters / type-checker / tests
uv run ruff check
uv run ruff format --check
uv run mypy src tests
uv run pytest
```

## Releasing (maintainer notes)

Releases publish to PyPI via GitHub Actions Trusted Publishing on tag push:

```bash
# bump version in pyproject.toml and src/splitwise_fx/__init__.py
git commit -am "release: vX.Y.Z"
git tag vX.Y.Z
git push origin main vX.Y.Z
```

The `release.yml` workflow builds with `uv build` and publishes via `pypa/gh-action-pypi-publish`. Trusted publishing is configured one-time at <https://pypi.org/manage/account/publishing/>.

## License

MIT — see [LICENSE](LICENSE).
