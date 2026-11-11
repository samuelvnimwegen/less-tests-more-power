# lesstests

`lesstests` records a REST API test suite through a proxy and cuts it down by black box coverage. It covers the Record and Select steps of the scope, plus a Merge step and a benchmark, on the PetStore and Google Drive suites of Besjes et al.

## How it works

1. **Record.** The tool copies the Maven project and compiles it once. It then runs every test alone in a fresh JVM. The test code is never changed. It stores every request and answer, plus whether the test passed.
   * PetStore runs in a local container that restarts before each test. The tests call it on a fixed port, so the tool listens on that port and passes each request on.
   * Google Drive is a live HTTPS API that can't be reset. The tool runs an intercepting proxy with its own certificate authority, and adds a TestNG listener to its copy of the project that points RestAssured at that proxy. RestAssured builds its own HTTP client, which ignores the JVM's proxy settings, so the listener is the only way in without touching a test. The token request to `oauth2.googleapis.com` is passed on but never stored, and every `Authorization` and cookie header is stored as `<redacted>`.
2. **Coverage.** RESTCov measures every test alone and the whole suite. The tool reads the covered items out of RESTCov's report. It checks that the items give the same counts as RESTCov, and that the tests together add up to the whole suite.
3. **Select.** At each budget, an integer program keeps the tests that reach the most coverage items, and among those the ones with the fewest requests. Random keeps tests at random over 30 draws. Both only drop tests, so the recording is enough and nothing runs again. RESTCov measures every smaller suite once more on its own logs, and the tool stops if that differs from the joined items.
4. **Merge.** RESTCov counts parameter names, not values, and one request only gets one status back. So the tool groups the tests that send one request by operation, real status and content types. In each group, the test that sends the most documented parameter names becomes the base. A copy of the base also sends the names the other tests send, with one value each, taken as plain literals from their `Map.of` calls. The copy is added to the base's class, compiled and recorded alone. It only stays when it sent one request and got the group's status back. When it doesn't, its donors split in two halves and both halves get tried again, for at most four rounds. Then a smallest cover program picks two versions that keep every RESTCov item of the full suite: Select from the original tests only, and Select + Merge from the original tests plus the merged copies.
5. **Bench.** Original, Select and Select + Merge each run as one whole suite in one JVM, through the proxy, once per round. The order rotates every round, so no version always runs first, and one Original run warms up before the first round. Every run gets measured again with RESTCov, and kept coverage compares a run with the Original run of the same round. The report gives the median runtime, the change against Original, a paired Wilcoxon signed rank test, and the RESTCov items and value items each version kept. A value item is one value a request sent for a documented query or header parameter.

## Setup

Needs Python 3.12 with [uv](https://docs.astral.sh/uv/), Java 21, Maven and Docker. RESTCov is not part of this repo, because its license is CC BY-NC-SA 4.0. Clone it from [github.com/2tolgahan2/RESTCov](https://github.com/2tolgahan2/RESTCov) and point the tool at its `restcov.py`.

```bash
uv sync
export LESSTESTS_RESTCOV=/path/to/RESTCov/restcov.py
```

PetStore runs from the local image `petstore-local:f673af60-lf-20261005`. The proxy takes port 8081, where the tests send their requests, and the container takes port 8082.

Google Drive needs an OAuth client of a Google account you don't mind losing data in. The tests create files and empty the Drive trash. Put `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and `GOOGLE_REFRESH_TOKEN` in a file only you can read and pass it with `--env-file`. The Drive client prints its token request, so the tool replaces every value of 8 characters or more from that file with `<redacted>` in the TestNG console logs it keeps. The proxy takes port 8089. Its certificate authority lives in `~/.cache/lesstests/ca`.

## Commands

```bash
uv run lesstests record --project /path/to/petstore --suite single-agent --out runs/petstore-single
uv run lesstests coverage --recording runs/petstore-single
uv run lesstests select --recording runs/petstore-single
uv run lesstests merge --recording runs/petstore-single --out runs/petstore-single-merge
uv run lesstests bench --merge runs/petstore-single-merge --rounds 10
```

`--subject` is `petstore` (the default) or `google-drive`, and `--suite` is `single-agent` or `multi-agent`. `record` picks up where it stopped when it runs again on the same folder. It stops if the subject, suite, project, spec or image differ from the first run, or if a recorded test left the suite. The project is copied without its `target/` and `.git` folders, so `--out` must sit outside it. Tests need `@Test` on each method. A class level `@Test` stops the run. `select` reuses the saved coverage until the recording changes. It takes `--budgets` (default `75 50 25`), `--draws` (default 30), `--seed` and `--passing-only`, which lets both selectors pick only from tests that passed alone. `merge` takes `--rounds` (default 4). `bench` takes `--rounds` (default 10), `--warmup` (default 1) and `--out`, and keeps the runs it already made, so a stopped benchmark resumes. `record`, `merge` and `bench` take `--env-file`.

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

The merge folder holds `merges.json` (every merge tried, why it stayed or went), `rounds/` (each round's sources, project copy and recording), `project/` (the copy with every kept merge, which the benchmark runs), `merged/` (the recording and coverage of the kept merges), `plan.json` and `plan.md` (the three versions and what the recordings predict) and `bench/` (every run with its logs, RESTCov report and TestNG output, plus `report.md`).

Recordings hold full request and response bodies. Keep them out of this public repo.
