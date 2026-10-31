from dataclasses import dataclass, field

from lesstests.proxy import Address
from lesstests.service import ContainerSpec


@dataclass(frozen=True, slots=True)
class Subject:
    """An API of the study: how to run it, where its tests call it and where its suites live."""

    name: str
    container: ContainerSpec
    listen: Address
    service_port: int
    spec_file: str
    suites: dict[str, str] = field(default_factory=dict)

    def package(self, suite: str) -> str:
        """Turn a suite name like `single-agent` into its Java package.

        Returns:
            The package.

        Raises:
            KeyError: If the subject has no such suite.
        """
        if suite not in self.suites:
            raise KeyError(f"{self.name} has no suite {suite!r}. Known suites: {', '.join(self.suites)}")
        return self.suites[suite]


PETSTORE = Subject(
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
    suites={"single-agent": "generations.single_agent", "multi-agent": "generations.multi_agent_chain"},
)

SUBJECTS = {subject.name: subject for subject in (PETSTORE,)}
