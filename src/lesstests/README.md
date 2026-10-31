# lesstests

`lesstests` records a REST API test suite through a proxy and cuts it down by black box coverage. This is the MVP. It covers the Record and Select steps of the scope, on the PetStore suites of Besjes et al.

## How it works

1. **Record.** The tool copies the Maven project and compiles it once. It then runs every test alone in a fresh JVM, after a restart of the service. The tests call the service on a fixed port, so the tool listens on that port and passes each request on to the service. It stores every request and answer, plus whether the test passed. The test code is never changed.
2. **Coverage.** RESTCov measures every test alone and the whole suite. The tool reads the covered items out of RESTCov's report. It checks that the items give the same counts as RESTCov, and that the tests together add up to the whole suite.
3. **Select.** At each budget, an integer program keeps the tests that reach the most coverage items, and among those the ones with the fewest requests. Random keeps tests at random over 30 draws. Both only drop tests, so the recording is enough and nothing runs again. RESTCov measures every smaller suite once more on its own logs, and the tool stops if that differs from the joined items.

## Setup

Needs Python 3.12 with [uv](https://docs.astral.sh/uv/), Java 21, Maven and Docker. RESTCov is not part of this repo, because its license is CC BY-NC-SA 4.0. Clone it from [github.com/2tolgahan2/RESTCov](https://github.com/2tolgahan2/RESTCov) and point the tool at its `restcov.py`.

```bash
uv sync
export LESSTESTS_RESTCOV=/path/to/RESTCov/restcov.py
```

PetStore runs from the local image `petstore-local:f673af60-lf-20261005`. The proxy takes port 8081, where the tests send their requests, and the container takes port 8082.

## Commands

```bash
uv run lesstests record --project /path/to/petstore --suite single-agent --out runs/petstore-single
uv run lesstests coverage --recording runs/petstore-single
uv run lesstests select --recording runs/petstore-single
```

`--suite` is `single-agent` or `multi-agent`. `record` picks up where it stopped when it runs again on the same folder. `select` takes `--budgets` (default `75 50 25`), `--draws` (default 30), `--seed` and `--passing-only`, which lets both selectors pick only from tests that passed alone.

## Output

| Path | What it holds |
|---|---|
| `meta.json` | The subject, suite, image and start and end time |
| `tests/` | One JSON file per test with its outcome, run time and every exchange |
| `restcov-logs/` | The same exchanges as RESTCov log files, one folder per test |
| `testng/` | TestNG's report and console output per test |
| `coverage/coverage.json` | The coverage items of every test and of the whole suite |
| `selection/report.md` | Coverage lost per budget, for Selected and Random |
| `selection/selection.json` | Every smaller suite with its tests, items and requests |

Recordings hold full request and response bodies. Keep them out of this public repo.
