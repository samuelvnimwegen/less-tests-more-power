from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from lesstests.java import (
    BuiltProject,
    GradleProject,
    JavaSuite,
    JUnitLauncher,
    JUnitSuite,
    JvmRunner,
    JvmSettings,
    Launcher,
    MavenProject,
    Overlay,
    SuiteFinder,
    TestNgLauncher,
)
from lesstests.proxy import Address, InterceptingProxy, Proxy, RecordingProxy
from lesstests.recording import EveryClass, EveryTest, ResetPolicy
from lesstests.service import ContainerSpec, DatabaseService, DockerService, PostgresSpec, RemoteService, Service
from lesstests.tls import CertificateAuthority


@dataclass(frozen=True, slots=True)
class SuiteSource:
    """Where a suite lives. `folder` is a project folder whose files take the place of the package folder.

    Example: the single agent Google Drive suite sits in `single-agent/fixed-tests` but declares `package generations`.
    """

    package: str
    folder: str | None = None

    def overlays(self, project: Path) -> tuple[Overlay, ...]:
        """Put the suite's files where Maven compiles them.

        Returns:
            One overlay when the suite lives outside its package folder, otherwise none.
        """
        if self.folder is None:
            return ()
        return (Overlay(project / self.folder, str(Path("src", "test", "java", *self.package.split(".")))),)


class Toolchain(Protocol):
    """How a subject's suite gets built, found and run: Maven with TestNG, or Gradle with JUnit 5."""

    @property
    def mergeable(self) -> bool:
        """Whether the Merge step can rewrite these tests. It only knows the TestNG sources of Besjes et al."""
        ...

    def project(self, source: Path, workdir: Path, overlays: tuple[Overlay, ...]) -> BuiltProject:
        """Set up a copy of the project, without building it.

        Returns:
            The project.
        """
        ...

    def suite(self, project: BuiltProject, package: str, settings: JvmSettings) -> SuiteFinder:
        """Point at the tests of one package in a built copy.

        Returns:
            What finds the tests.
        """
        ...

    def launcher(self) -> Launcher:
        """Name the test framework's side of a test JVM.

        Returns:
            The launcher.
        """
        ...


@dataclass(frozen=True, slots=True)
class MavenTestNg:
    """Maven builds the copy, the sources name the tests and TestNG runs them."""

    @property
    def mergeable(self) -> bool:
        """Merge works on these sources."""
        return True

    def project(self, source: Path, workdir: Path, overlays: tuple[Overlay, ...]) -> BuiltProject:
        """Set up a Maven copy.

        Returns:
            The project.
        """
        return MavenProject(source, workdir, overlays)

    def suite(self, project: BuiltProject, package: str, settings: JvmSettings) -> SuiteFinder:
        """Read the tests from the sources.

        Returns:
            The suite.
        """
        return JavaSuite(project.workdir, package)

    def launcher(self) -> Launcher:
        """Run TestNG.

        Returns:
            The launcher.
        """
        return TestNgLauncher()


@dataclass(frozen=True, slots=True)
class GradleJUnit:
    """Gradle builds one module of the copy, and JUnit 5 finds and runs its tests. `env` goes to Gradle only."""

    module: str
    env: dict[str, str] = field(default_factory=dict)

    @property
    def mergeable(self) -> bool:
        """Merge can't rewrite these tests, since it reads the `Map.of` calls of TestNG sources."""
        return False

    def project(self, source: Path, workdir: Path, overlays: tuple[Overlay, ...]) -> BuiltProject:
        """Set up a Gradle copy that also compiles `LesstestsJUnit`.

        Returns:
            The project.
        """
        return GradleProject(source, workdir, self.module, overlays, self.env, (JUnitLauncher.SOURCE,))

    def suite(self, project: BuiltProject, package: str, settings: JvmSettings) -> SuiteFinder:
        """Ask JUnit for the tests.

        Returns:
            The suite.
        """
        return JUnitSuite(project, package, settings)

    def launcher(self) -> Launcher:
        """Run JUnit through `LesstestsJUnit`.

        Returns:
            The launcher.
        """
        return JUnitLauncher()


@dataclass(frozen=True, slots=True)
class Timeouts:
    """How long one test alone, one whole suite run and one answer through the proxy may take, in seconds."""

    test: float = 300.0
    suite: float = 1800.0
    request: float = 60.0


@dataclass(slots=True)
class Connection:
    """Everything a run needs to reach the API: the service, the proxy in front of it and the JVM settings."""

    service: Service
    proxy: Proxy
    settings: JvmSettings


class Subject(Protocol):
    """An API of the study: where its suites live and how a run reaches it."""

    @property
    def name(self) -> str:
        """The name on the command line, like `petstore`."""
        ...

    @property
    def spec_file(self) -> str:
        """The API description, relative to the project."""
        ...

    @property
    def restcov_base_path(self) -> str | None:
        """A prefix RESTCov strips on top of the description's servers."""
        ...

    @property
    def suites(self) -> dict[str, SuiteSource]:
        """The suites by name."""
        ...

    @property
    def toolchain(self) -> Toolchain:
        """How the suite gets built, found and run."""
        ...

    @property
    def reset_policy(self) -> ResetPolicy:
        """Before which tests Record resets the service."""
        ...

    @property
    def timeouts(self) -> Timeouts:
        """How long tests, suites and answers may take."""
        ...

    def spec_path(self, source: Path, copy: Path) -> Path:
        """Find the API description, in the source project or, when the build makes it, in the built copy.

        Returns:
            The path.
        """
        ...

    def overlays(self, project: Path, suite: str) -> tuple[Overlay, ...]:
        """The files to place into the project copy before it compiles.

        Returns:
            The overlays.
        """
        ...

    def connect(self, env: dict[str, str]) -> Connection:
        """Set up the service, the proxy and the JVM settings without starting anything.

        Returns:
            The connection.
        """
        ...

    def meta(self) -> dict[str, Any]:
        """The facts a recording stores so a resumed run can check it still talks to the same API.

        Returns:
            The facts.
        """
        ...


class Runners:
    """Builds the test runner a subject needs, with its framework and its timeouts."""

    @staticmethod
    def of(subject: "Subject", project: BuiltProject, output: Path, settings: JvmSettings) -> JvmRunner:
        """Build the runner.

        Returns:
            The runner.
        """
        timeouts = subject.timeouts
        launcher = subject.toolchain.launcher()
        return JvmRunner(project, output, timeouts.test, settings, launcher, timeouts.suite)


class SuiteCatalog:
    """Looks up a suite of a subject."""

    @staticmethod
    def source(subject: Subject, suite: str) -> SuiteSource:
        """Find a suite by name.

        Returns:
            The suite.

        Raises:
            KeyError: If the subject has no such suite.
        """
        if suite not in subject.suites:
            raise KeyError(f"{subject.name} has no suite {suite!r}. Known suites: {', '.join(subject.suites)}")
        return subject.suites[suite]


@dataclass(frozen=True, slots=True)
class LocalSubject:
    """An API that runs in a local container. A reverse proxy takes the port the tests call."""

    name: str
    container: ContainerSpec
    listen: Address
    service_port: int
    spec_file: str
    suites: dict[str, SuiteSource] = field(default_factory=dict)
    restcov_base_path: str | None = None
    toolchain: Toolchain = field(default_factory=MavenTestNg)
    reset_policy: ResetPolicy = field(default_factory=EveryTest)
    timeouts: Timeouts = field(default_factory=Timeouts)

    def spec_path(self, source: Path, copy: Path) -> Path:
        """The description sits in the source project.

        Returns:
            The path.
        """
        return source / self.spec_file

    def overlays(self, project: Path, suite: str) -> tuple[Overlay, ...]:
        """Only the suite's own files, since the tests already call the proxy's port.

        Returns:
            The overlays.
        """
        return SuiteCatalog.source(self, suite).overlays(project)

    def connect(self, env: dict[str, str]) -> Connection:
        """Use a fresh container and the reverse proxy.

        Returns:
            The connection.
        """
        service = DockerService(self.container, self.service_port, f"lesstests-{self.name}")
        proxy = RecordingProxy(self.listen, service.address, self.timeouts.request)
        return Connection(service, proxy, JvmSettings(env=dict(env)))

    def meta(self) -> dict[str, Any]:
        """The image the container runs.

        Returns:
            The facts.
        """
        return {"image": self.container.image}


@dataclass(frozen=True, slots=True)
class LiveSubject:
    """A live HTTPS API. The tests reach it through an intercepting proxy that a TestNG listener switches on.

    The service can't be reset, so every test sees whatever the account holds at that moment.
    """

    name: str
    spec_file: str
    host: str
    listen: Address
    passthrough: frozenset[str]
    required_env: tuple[str, ...]
    suites: dict[str, SuiteSource] = field(default_factory=dict)
    restcov_base_path: str | None = None
    ca_folder: Path = field(default_factory=lambda: Path.home() / ".cache" / "lesstests" / "ca")
    toolchain: Toolchain = field(default_factory=MavenTestNg)
    reset_policy: ResetPolicy = field(default_factory=EveryTest)
    timeouts: Timeouts = field(default_factory=Timeouts)

    TRUSTSTORE_PASSWORD = "lesstests"

    def spec_path(self, source: Path, copy: Path) -> Path:
        """The description sits in the source project.

        Returns:
            The path.
        """
        return source / self.spec_file

    def overlays(self, project: Path, suite: str) -> tuple[Overlay, ...]:
        """The suite's files plus the listener that points RestAssured at the proxy.

        Returns:
            The overlays.
        """
        return (*SuiteCatalog.source(self, suite).overlays(project), MavenProject.support_overlay())

    def connect(self, env: dict[str, str]) -> Connection:
        """Use the intercepting proxy and hand the JVM the proxy address, the truststore and the credentials.

        Returns:
            The connection.

        Raises:
            ValueError: If a credential the tests read is missing from `env`.
        """
        missing = [name for name in self.required_env if not env.get(name)]
        if missing:
            raise ValueError(f"{self.name} needs {', '.join(missing)}. Pass them with --env-file")
        authority = CertificateAuthority.load_or_create(self.ca_folder)
        truststore = authority.java_truststore(self.ca_folder / "truststore.p12", self.TRUSTSTORE_PASSWORD)
        proxy = InterceptingProxy(self.listen, authority, self.passthrough)
        settings = proxy.jvm_settings(truststore, self.TRUSTSTORE_PASSWORD).merged(JvmSettings(env=dict(env)))
        return Connection(RemoteService(self.host), proxy, settings)

    def meta(self) -> dict[str, Any]:
        """Say that the API is live, so nobody mistakes a recording for a reset run.

        Returns:
            The facts.
        """
        return {"image": "live API"}


@dataclass(frozen=True, slots=True)
class AddressVariables:
    """The environment variables a suite reads the server's address from, like Fineract's `BACKEND_HOST`."""

    protocol: str
    host: str
    port: str

    def env(self, address: Address) -> dict[str, str]:
        """Point the tests at an address over plain HTTP.

        Returns:
            The variables.
        """
        return {self.protocol: "http", self.host: address.host, self.port: str(address.port)}


@dataclass(frozen=True, slots=True)
class ServerSubject:
    """An API whose server keeps its data in Postgres, both in local containers, like Fineract.

    The tests read the server's address from environment variables, so they call the reverse proxy with their code
    unchanged. The build makes the API description, so it sits in the built copy.
    """

    name: str
    app: ContainerSpec
    database: PostgresSpec
    listen: Address
    service_port: int
    spec_file: str
    address: AddressVariables
    toolchain: Toolchain
    suites: dict[str, SuiteSource] = field(default_factory=dict)
    test_env: dict[str, str] = field(default_factory=dict)
    reset_policy: ResetPolicy = field(default_factory=EveryTest)
    timeouts: Timeouts = field(default_factory=Timeouts)
    restcov_base_path: str | None = None

    def spec_path(self, source: Path, copy: Path) -> Path:
        """The description sits in the built copy.

        Returns:
            The path.
        """
        return copy / self.spec_file

    def overlays(self, project: Path, suite: str) -> tuple[Overlay, ...]:
        """Only the suite's own files, since environment variables point the tests at the proxy.

        Returns:
            The overlays.
        """
        return SuiteCatalog.source(self, suite).overlays(project)

    def connect(self, env: dict[str, str]) -> Connection:
        """Use both containers and the reverse proxy, and point the tests at the proxy.

        Returns:
            The connection.
        """
        service = DatabaseService(self.app, self.database, self.service_port, f"lesstests-{self.name}")
        proxy = RecordingProxy(self.listen, service.address, self.timeouts.request)
        settings = JvmSettings(env=dict(env), public_env={**self.address.env(self.listen), **self.test_env})
        return Connection(service, proxy, settings)

    def meta(self) -> dict[str, Any]:
        """Both images, since a rebuilt app image can keep its tag.

        Returns:
            The facts.
        """
        return {"image": self.app.image, "database_image": self.database.image}


PETSTORE = LocalSubject(
    name="petstore",
    container=ContainerSpec(
        image="petstore-local:f673af60-lf-20261005",
        port=8080,
        health_path="/v2/store/inventory",
        env={"SWAGGER_BASE_PATH": "/v2"},
    ),
    listen=Address("127.0.0.1", 8081),
    service_port=8082,
    spec_file="specification-petstore.json",
    suites={
        "single-agent": SuiteSource("generations.single_agent"),
        "multi-agent": SuiteSource("generations.multi_agent_chain"),
    },
)

GOOGLE_DRIVE = LiveSubject(
    name="google-drive",
    spec_file="openapi.json",
    host="www.googleapis.com",
    listen=Address("127.0.0.1", 8089),
    passthrough=frozenset({"oauth2.googleapis.com"}),
    required_env=("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"),
    suites={
        "single-agent": SuiteSource("generations", "single-agent/fixed-tests"),
        "multi-agent": SuiteSource("generations"),
    },
    # The description's only server is `https://storage.googleapis.com/`, but the multi agent tests call
    # `https://www.googleapis.com/drive/v3`, so RESTCov has to strip that prefix too.
    restcov_base_path="/drive/v3",
)

FINERACT_DATABASE = PostgresSpec("postgres:18.3", ("fineract_tenants", "fineract_default"))
FINERACT_TENANTS, FINERACT_DEFAULT_TENANT = FINERACT_DATABASE.databases
FINERACT_JVM_FLAGS = " ".join(
    [
        "-Xmx2G",
        "--add-exports=java.naming/com.sun.jndi.ldap=ALL-UNNAMED",
        *(
            f"--add-opens={module}=ALL-UNNAMED"
            for module in (
                "java.base/java.lang",
                "java.base/java.lang.invoke",
                "java.base/java.io",
                "java.base/java.security",
                "java.base/java.util",
                "java.management/javax.management",
                "java.naming/javax.naming",
            )
        ),
    ]
)

FINERACT = ServerSubject(
    name="fineract",
    app=ContainerSpec(
        image="fineract-local:feb1ba4",
        port=8080,
        health_path="/fineract-provider/actuator/health",
        # The settings of `config/docker/env/fineract*.env` and `docker-compose-postgresql-test.yml` in Fineract,
        # without TLS, LocalStack, the OAuth2 mock and the debug agent.
        env={
            "FINERACT_NODE_ID": "1",
            "FINERACT_SERVER_PORT": "8080",
            "FINERACT_SERVER_SSL_ENABLED": "false",
            "FINERACT_HIKARI_DRIVER_SOURCE_CLASS_NAME": "org.postgresql.Driver",
            "FINERACT_HIKARI_JDBC_URL": f"jdbc:postgresql://{PostgresSpec.HOST}:5432/{FINERACT_TENANTS}",
            "FINERACT_HIKARI_USERNAME": FINERACT_DATABASE.user,
            "FINERACT_HIKARI_PASSWORD": FINERACT_DATABASE.password,
            "FINERACT_DEFAULT_TENANTDB_HOSTNAME": PostgresSpec.HOST,
            "FINERACT_DEFAULT_TENANTDB_PORT": "5432",
            "FINERACT_DEFAULT_TENANTDB_UID": FINERACT_DATABASE.user,
            "FINERACT_DEFAULT_TENANTDB_PWD": FINERACT_DATABASE.password,
            "FINERACT_DEFAULT_TENANTDB_CONN_PARAMS": "",
            "FINERACT_DEFAULT_TENANTDB_TIMEZONE": "Asia/Kolkata",
            "FINERACT_DEFAULT_TENANTDB_IDENTIFIER": "default",
            "FINERACT_DEFAULT_TENANTDB_NAME": FINERACT_DEFAULT_TENANT,
            "FINERACT_DEFAULT_TENANTDB_DESCRIPTION": "Default Demo Tenant",
            "FINERACT_DEFAULT_MASTER_PASSWORD": "fineract",
            "FINERACT_MANAGEMENT_ENDPOINT_WEB_EXPOSURE_INCLUDE": "health,info",
            "FINERACT_REMOTE_JOB_MESSAGE_HANDLER_SPRING_EVENTS_ENABLED": "true",
            "FINERACT_INSECURE_HTTP_CLIENT": "true",
            "FINERACT_CONTENT_FILESYSTEM_ROOT_FOLDER": "/tmp",
            "FINERACT_EXTERNAL_EVENTS_ENABLED": "true",
            "EXECUTE_STANDING_INSTRUCTIONS_PARTITION_SIZE": "1",
            "EXECUTE_STANDING_INSTRUCTIONS_CHUNK_SIZE": "2",
            "SPRING_PROFILES_ACTIVE": "test,diagnostics",
            "JAVA_TOOL_OPTIONS": FINERACT_JVM_FLAGS,
        },
        # Some tests start a WireMock server in the test JVM and let Fineract call it.
        docker_args=("--add-host=host.docker.internal:host-gateway",),
    ),
    database=FINERACT_DATABASE,
    listen=Address("127.0.0.1", 8091),
    service_port=8092,
    spec_file="fineract-provider/build/resources/main/static/fineract.json",
    address=AddressVariables("BACKEND_PROTOCOL", "BACKEND_HOST", "BACKEND_PORT"),
    toolchain=GradleJUnit(":integration-tests", {"VERSIONING_DISABLE": "true"}),
    suites={"integration-tests": SuiteSource("org.apache.fineract.integrationtests")},
    test_env={"FINERACT_TEST_EXTERNAL_HOST": "host.docker.internal"},
    reset_policy=EveryClass(),
    timeouts=Timeouts(test=1800.0, suite=12 * 3600.0, request=600.0),
)

SUBJECTS: dict[str, Subject] = {subject.name: subject for subject in (PETSTORE, GOOGLE_DRIVE, FINERACT)}
