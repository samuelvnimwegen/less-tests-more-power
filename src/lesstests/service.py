import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

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
        env = [arg for key, value in self._spec.env.items() for arg in ("-e", f"{key}={value}")]
        publish = f"{self._address.host}:{self._address.port}:{self._spec.port}"
        self._docker("run", "-d", "--name", self._name, *env, "-p", publish, self._spec.image)
        self._wait_until_ready()

    def reset(self) -> None:
        """Restart the container, which wipes the in-memory data."""
        self._docker("restart", "-t", "1", self._name)
        self._wait_until_ready()

    def stop(self) -> None:
        """Remove the container."""
        self._docker("rm", "-f", self._name, check=False)

    def _wait_until_ready(self) -> None:
        url = f"http://{self._address.host}:{self._address.port}{self._spec.health_path}"
        deadline = time.monotonic() + self._ready_timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as answer:
                    if answer.status == 200:
                        return
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                pass
            time.sleep(0.25)
        raise TimeoutError(f"{self._name} did not answer 200 on {url} within {self._ready_timeout} s")

    @staticmethod
    def _docker(*args: str, check: bool = True) -> None:
        subprocess.run(["docker", *args], check=check, capture_output=True, text=True)
