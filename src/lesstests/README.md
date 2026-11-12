# lesstests

`lesstests` records a REST API test suite through a proxy and cuts it down by black box coverage. It covers the Record and Select steps of the scope, plus a Merge step and a benchmark, on the PetStore and Google Drive suites of Besjes et al. and on the integration tests of Apache Fineract.

## How it works

1. **Record.** The tool copies the Maven or Gradle project and compiles it once. It then runs every test alone in a fresh JVM. The test code is never changed. It stores every request and answer, plus whether the test passed.
   * PetStore runs in a local container that restarts before each test. The tests call it on a fixed port, so the tool listens on that port and passes each request on.
   * Fineract keeps its data in Postgres, so it runs in a local container next to a Postgres container. After the first start the tool copies each database to a template. A reset stops Fineract, makes each database again from its template and starts Fineract, which takes about 48 seconds. That's too slow before each of its 3,053 tests, so Fineract resets before the first test of each class instead. The tests of one class still run alone, each in its own JVM, but see what the earlier ones of that class left behind. A run that stopped halfway through a class records that whole class again. The tests read the server's address from `BACKEND_PROTOCOL`, `BACKEND_HOST` and `BACKEND_PORT`, so the tool points those at its proxy.
   * Fineract's tests are JUnit 5 and its build is Gradle. The tool adds an init script to its Gradle run that writes the test classpath, the java binary of the module's toolchain and the JVM flags of the module's test task. A small Java class of the tool, `LesstestsJUnit`, gets compiled next to the copy. It lists the tests through the project's own JUnit Platform, which also finds package private, inherited, nested and parameterized tests, and leaves `@Disabled` ones out. It also runs a list of tests and writes one outcome per method, the worst one over the runs of a parameterized test.
   * Google Drive is a live HTTPS API that can't be reset. The tool runs an intercepting proxy with its own certificate authority, and adds a TestNG listener to its copy of the project that points RestAssured at that proxy. RestAssured builds its own HTTP client, which ignores the JVM's proxy settings, so the listener is the only way in without touching a test. The token request to `oauth2.googleapis.com` is passed on but never stored, and every `Authorization` and cookie header is stored as `<redacted>`.
2. **Coverage.** RESTCov measures every test alone and the whole suite. The tool reads the covered items out of RESTCov's report. It checks that the items give the same counts as RESTCov, and that the tests together add up to the whole suite.
3. **Select.** At each budget, an integer program keeps the tests that reach the most coverage items, and among those the ones with the fewest requests. Random keeps tests at random over 30 draws. Both only drop tests, so the recording is enough and nothing runs again. RESTCov measures every smaller suite once more on its own logs, and the tool stops if that differs from the joined items.
4. **Merge.** RESTCov counts parameter names, not values, and one request only gets one status back. So the tool groups the tests that send one request by operation, real status and content types. It first checks that its own matching of each request to an operation agrees with RESTCov, and stops when it doesn't. In each group, the test that sends the most documented parameter names becomes the base. A copy of the base also sends the names the other tests send, with one value each, taken as plain literals from their `Map.of` calls. The copy is added to the base's class, compiled and recorded alone. It only stays when it sent one request and got the group's status back. When it doesn't, its donors split in two halves and both halves get tried again, for at most four rounds. Then a smallest cover program picks two versions that keep every RESTCov item of the full suite: Select from the original tests only, and Select + Merge from the original tests plus the merged copies. Merge only rewrites the TestNG suites. Fineract's tests go through a generated client, so `lesstests plan` plans Original and Select for it without a Merge step, and counts the tests that sent exactly one request.
5. **Bench.** Original, Select and Select + Merge each run as one whole suite in one JVM, through the proxy, once per round. The order rotates every round, so no version always runs first, and one Original run warms up before the first round. Every run gets measured again with RESTCov, and kept coverage compares a run with the Original run of the same round. The report gives the median runtime, the change against Original, a paired Wilcoxon signed rank test, and the RESTCov items and value items each version kept. A value item is one value a request sent for a documented query or header parameter.

## Setup

Needs Python 3.12 with [uv](https://docs.astral.sh/uv/), Java 21, Maven and Docker, and Java 25 for Fineract. RESTCov is not part of this repo, because its license is CC BY-NC-SA 4.0. Clone it from [github.com/2tolgahan2/RESTCov](https://github.com/2tolgahan2/RESTCov) and point the tool at its `restcov.py`.

```bash
uv sync
export LESSTESTS_RESTCOV=/path/to/RESTCov/restcov.py
```

PetStore runs from the local image `petstore-local:f673af60-lf-20261005`. The proxy takes port 8081, where the tests send their requests, and the container takes port 8082.

Fineract needs Java 25, since its build plugins load the server's classes. Run the tool with `JAVA_HOME` pointing at a JDK 25, and the tests run on the JDK that Gradle's toolchain picks. Build the server image once from the same commit as the source, with git versioning switched off since a shallow clone has no tags:

```bash
cd /path/to/fineract
JAVA_HOME=/path/to/jdk-25 VERSIONING_DISABLE=true ./gradlew :fineract-provider:jibDockerBuild -Djib.to.image=fineract-local:feb1ba4 -x test
```

Fineract runs from `fineract-local:feb1ba4` next to `postgres:18.3`, on a Docker network of their own. The proxy takes port 8091 and Fineract port 8092. Tests that need LocalStack or the OAuth2 mock server of Fineract's CI fail, since the tool only starts Fineract and Postgres.

Google Drive needs an OAuth client of a Google account you don't mind losing data in. The tests create files and empty the Drive trash. Put `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and `GOOGLE_REFRESH_TOKEN` in a file only you can read and pass it with `--env-file`. The Drive client prints its token request and every access token it gets, so the tool replaces every bearer token and every value of 8 characters or more from that file with `<redacted>` in the TestNG console logs it keeps. The proxy takes port 8089. Its certificate authority lives in `~/.cache/lesstests/ca`.

## Commands

```bash
uv run lesstests record --project /path/to/petstore --suite single-agent --out runs/petstore-single
uv run lesstests coverage --recording runs/petstore-single
uv run lesstests select --recording runs/petstore-single
uv run lesstests merge --recording runs/petstore-single --out runs/petstore-single-merge
uv run lesstests bench --merge runs/petstore-single-merge --rounds 10

JAVA_HOME=/path/to/jdk-25 uv run lesstests record --subject fineract --project /path/to/fineract --suite integration-tests --out runs/fineract
uv run lesstests plan --recording runs/fineract --out runs/fineract-plan
uv run lesstests bench --plan runs/fineract-plan --rounds 3
```

`--subject` is `petstore` (the default), `google-drive` or `fineract`. `--suite` is `single-agent` or `multi-agent`, or `integration-tests` for Fineract. `--only` records only the tests whose id matches a regular expression, which is handy for a first try on a few classes. A later run without `--only` on the same folder records the rest. `record` picks up where it stopped when it runs again on the same folder. It stops if the subject, suite, project, spec or image differ from the first run, or if a recorded test left the suite. The project is copied without its `target/` and `.git` folders, or `build/`, `.gradle/` and `.git` for Gradle, so `--out` must sit outside it. TestNG tests need `@Test` on each method. A class level `@Test` stops the run. `select` reuses the saved coverage until the recording changes. It takes `--budgets` (default `75 50 25`), `--draws` (default 30), `--seed` and `--passing-only`, which lets both selectors pick only from tests that passed alone. `merge` takes `--rounds` (default 4) and refuses an `--out` inside the recording, since the recording's project copy is the source of every merge. A copy that doesn't compile only drops itself. `plan` takes `--out` and writes the folder `bench` reads, with Original and Select only. `bench --plan` reads a folder of `merge` or `plan` (`--merge` still works). It takes `--rounds` (default 10), `--warmup` (default 1) and `--out`, and keeps the runs it already made, so a stopped benchmark resumes. It refuses a folder that holds runs of another plan. `record`, `merge` and `bench` take `--env-file`.

## Output

| Path | What it holds |
|---|---|
| `meta.json` | The subject, suite, image and start and end time |
| `tests/` | One JSON file per test with its outcome, run time and every exchange |
| `restcov-logs/` | The same exchanges as RESTCov log files, one folder per test |
| `testng/` | The report and console output per test, from TestNG or `LesstestsJUnit` |
| `coverage/coverage.json` | The coverage items of every test and of the whole suite |
| `selection/report.md` | Coverage lost per budget, for Selected and Random |
| `selection/selection.json` | Every smaller suite with its tests, items and requests |

The merge folder holds `merges.json` (every merge tried, why it stayed or went, and every one-request test it couldn't merge with the reason), `rounds/` (each round's sources, project copy and recording), `project/` (the copy with every kept merge, which the benchmark runs), `merged/` (the recording and coverage of the kept merges), `plan.json` and `plan.md` (the three versions and what the recordings predict) and `bench/` (every run with its logs, RESTCov report and TestNG output, plus `report.md`). The plan folder holds `meta.json`, which names the recording's project copy for the benchmark, `plan.json`, `plan.md` and `bench/`.

Recordings hold full request and response bodies. Keep them out of this public repo.
