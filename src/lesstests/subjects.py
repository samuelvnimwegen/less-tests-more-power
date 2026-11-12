from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from lesstests.java import JvmSettings, MavenProject, Overlay
from lesstests.proxy import Address, InterceptingProxy, Proxy, RecordingProxy
from lesstests.service import ContainerSpec, DockerService, RemoteService, Service
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
        return Connection(service, RecordingProxy(self.listen, service.address), JvmSettings(env=dict(env)))

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

    TRUSTSTORE_PASSWORD = "lesstests"

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

SUBJECTS: dict[str, Subject] = {subject.name: subject for subject in (PETSTORE, GOOGLE_DRIVE)}
