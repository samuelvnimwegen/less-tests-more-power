import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import ClassVar, Protocol

from lesstests.proxy import Address


class Service(Protocol):
    """The service under test, which the tool can start, reset and stop."""

    @property
    def address(self) -> Address:
        """Where the service listens."""
        ...

    def start(self) -> None:
        """Start the service and wait until it answers."""
        ...

    def reset(self) -> None:
        """Put the service back in its starting state."""
        ...

    def stop(self) -> None:
        """Stop the service."""
        ...


@dataclass(frozen=True, slots=True)
class ContainerSpec:
    """How to run a service image. The health path must answer 200 once the service is up."""

    image: str
    port: int
    health_path: str
    env: dict[str, str] = field(default_factory=dict)
    docker_args: tuple[str, ...] = ()

    def run_arguments(self, name: str, publish: str, *extra: str) -> list[str]:
        """Build the `docker run` arguments, with the extra ones before the image.

        Returns:
            The arguments after `docker`.
        """
        env = [arg for key, value in self.env.items() for arg in ("-e", f"{key}={value}")]
        return ["run", "-d", "--name", name, *extra, *env, *self.docker_args, "-p", publish, self.image]


@dataclass(frozen=True, slots=True)
class HealthCheck:
    """Polls a health path until it answers 200."""

    address: Address
    path: str
    timeout: float

    def wait(self, name: str) -> None:
        """Wait for the first 200.

        Raises:
            TimeoutError: If no 200 comes back within the timeout.
        """
        url = f"http://{self.address.host}:{self.address.port}{self.path}"
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as answer:
                    if answer.status == 200:
                        return
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                pass
            time.sleep(0.25)
        raise TimeoutError(f"{name} did not answer 200 on {url} within {self.timeout} s")


class DockerService:
    """A service in one Docker container. A restart resets it, which suits services that keep their data in memory."""

    def __init__(self, spec: ContainerSpec, host_port: int, name: str, ready_timeout: float = 120.0) -> None:
        """Set up the container without starting it."""
        self._spec = spec
        self._name = name
        self._address = Address("127.0.0.1", host_port)
        self._ready_timeout = ready_timeout

    @property
    def address(self) -> Address:
        """Where the container's port is published on this machine."""
        return self._address

    def start(self) -> None:
        """Start a fresh container, replacing any old one with the same name."""
        self._docker("rm", "-f", self._name, check=False)
        publish = f"{self._address.host}:{self._address.port}:{self._spec.port}"
        self._docker(*self._spec.run_arguments(self._name, publish))
        self._wait_until_ready()

    def reset(self) -> None:
        """Restart the container, which wipes the in-memory data."""
        self._docker("restart", "-t", "1", self._name)
        self._wait_until_ready()

    def stop(self) -> None:
        """Remove the container."""
        self._docker("rm", "-f", self._name, check=False)

    def _wait_until_ready(self) -> None:
        HealthCheck(self._address, self._spec.health_path, self._ready_timeout).wait(self._name)

    @staticmethod
    def _docker(*args: str, check: bool = True) -> None:
        subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


class RemoteService:
    """A live API the tool can't start or reset, like Google Drive. The tests reach it by its own host name."""

    def __init__(self, host: str, port: int = 443) -> None:
        """Name the host the tests call."""
        self._address = Address(host, port)

    @property
    def address(self) -> Address:
        """The host and port of the live API, which the intercepting proxy reaches over TLS."""
        return self._address

    def start(self) -> None:
        """Do nothing, the API is already running."""

    def reset(self) -> None:
        """Do nothing, since a live API has no starting state the tool can go back to."""

    def stop(self) -> None:
        """Do nothing, the API keeps running."""


@dataclass(frozen=True, slots=True)
class PostgresSpec:
    """The Postgres container next to an app. The app reaches it by the host name `db`."""

    image: str
    databases: tuple[str, ...]
    user: str = "postgres"
    password: str = "lesstests"

    HOST: ClassVar[str] = "db"
    TEMPLATE_PREFIX: ClassVar[str] = "lesstests_start_"

    def template(self, database: str) -> str:
        """Name the copy that holds a database as the app left it after its first start.

        Returns:
            The template's name.
        """
        return f"{self.TEMPLATE_PREFIX}{database}"


class DockerCommand(Protocol):
    """Runs one docker command."""

    def __call__(self, *args: str, check: bool = True) -> None:
        """Run `docker` with these arguments."""
        ...


class DatabaseService:
    """An app container with a Postgres container on their own Docker network, like Fineract.

    The app's data lives in Postgres, so a restart alone keeps it. After the first start every database gets copied
    to a template. A reset stops the app, makes every database again from its template and starts the app, since
    the app holds caches and open connections to the old databases.
    """

    DATABASE_TIMEOUT: ClassVar[float] = 120.0

    def __init__(
        self,
        app: ContainerSpec,
        database: PostgresSpec,
        host_port: int,
        name: str,
        ready_timeout: float = 900.0,
        docker: DockerCommand | None = None,
    ) -> None:
        """Set up both containers without starting them."""
        self._app = app
        self._database = database
        self._address = Address("127.0.0.1", host_port)
        self._name = name
        self._ready = HealthCheck(self._address, app.health_path, ready_timeout)
        self._docker = docker or DockerService._docker

    @property
    def address(self) -> Address:
        """Where the app's port is published on this machine."""
        return self._address

    @property
    def _db(self) -> str:
        return f"{self._name}-db"

    def start(self) -> None:
        """Start both containers fresh, let the app fill the databases, and keep a template of each."""
        self.stop()
        self._docker("network", "create", self._name)
        database = self._database
        self._docker(
            "run", "-d", "--name", self._db, "--network", self._name, "--network-alias", database.HOST,
            "-e", f"POSTGRES_USER={database.user}", "-e", f"POSTGRES_PASSWORD={database.password}", database.image,
        )  # fmt: skip
        self._wait_for_database()
        for name in database.databases:
            self._sql(f'CREATE DATABASE "{name}"')
        publish = f"{self._address.host}:{self._address.port}:{self._app.port}"
        self._docker(*self._app.run_arguments(self._name, publish, "--network", self._name))
        self._ready.wait(self._name)
        self._docker("stop", "-t", "2", self._name)
        for name in database.databases:
            self._sql(f'CREATE DATABASE "{database.template(name)}" TEMPLATE "{name}"')
        self._docker("start", self._name)
        self._ready.wait(self._name)

    def reset(self) -> None:
        """Stop the app, put every database back from its template and start the app again."""
        self._docker("stop", "-t", "2", self._name)
        for name in self._database.databases:
            self._sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            self._sql(f'CREATE DATABASE "{name}" TEMPLATE "{self._database.template(name)}"')
        self._docker("start", self._name)
        self._ready.wait(self._name)

    def stop(self) -> None:
        """Remove both containers and their network."""
        self._docker("rm", "-f", self._name, self._db, check=False)
        self._docker("network", "rm", self._name, check=False)

    def _sql(self, statement: str) -> None:
        user = self._database.user
        self._docker("exec", self._db, "psql", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", "postgres", "-c", statement)

    def _wait_for_database(self) -> None:
        """Wait until Postgres takes connections over TCP, since its first boot only listens on a socket.

        Raises:
            TimeoutError: If it never does.
        """
        deadline = time.monotonic() + self.DATABASE_TIMEOUT
        while time.monotonic() < deadline:
            try:
                self._docker("exec", self._db, "pg_isready", "-q", "-h", "127.0.0.1", "-U", self._database.user)
                return
            except subprocess.CalledProcessError:
                time.sleep(0.5)
        raise TimeoutError(f"{self._db} never took connections")
