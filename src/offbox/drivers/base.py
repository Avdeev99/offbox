from pydantic import field_validator

from offbox.config import Model
from offbox.ssh import validate_address, validate_user


class HostVariables(Model):
    name: str
    size: str
    region: str
    image: str
    ssh_public_key: str
    user_data: str
    labels: dict[str, str]


class ProviderOutputs(Model):
    ip: str
    ssh_user: str

    @field_validator("ip")
    @classmethod
    def check_address(cls, value: str) -> str:
        return validate_address(value)

    @field_validator("ssh_user")
    @classmethod
    def check_user(cls, value: str) -> str:
        return validate_user(value)


class TofuOutput(Model):
    value: str
    type: str = "string"
    sensitive: bool = False


class TofuOutputs(Model):
    ip: TofuOutput
    ssh_user: TofuOutput
