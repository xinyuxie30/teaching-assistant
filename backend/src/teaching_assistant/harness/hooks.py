"""Code-registered extension hooks with immutable, versioned configuration."""

from types import MappingProxyType

from pydantic import Field, ValidationError

from teaching_assistant.domain import Contract, DomainError, digest


class DenyConfig(Contract):
    denied_tools: list[str] = Field(default_factory=list, max_length=40)
    block_stop: bool = False


class DenyHooks:
    def __init__(self, config):
        self.denied_tools = frozenset(config.denied_tools)
        self.block_stop = config.block_stop

    def pre_tool(self, name, context):
        return "configured_hook_denied" if name in self.denied_tools else None

    def post_tool(self, name, result):
        pass

    def stop(self, context):
        return not self.block_stop


# Extensions are registered by application code with an explicit implementation
# version, never imported from a model-supplied name, path, URL or command.
REGISTRY = MappingProxyType({"deny-tools": ("1", DenyConfig, DenyHooks)})


def snapshot(settings):
    name = settings.harness_hook_id
    if name not in REGISTRY:
        raise DomainError("unknown_hook_profile", "Hook is not registered in application code")
    version, schema, _ = REGISTRY[name]
    try:
        config = schema.model_validate(settings.harness_hook_config).model_dump(mode="json")
    except ValidationError:
        raise DomainError("invalid_hook_config", "Hook configuration violates its schema") from None
    value = {"id": name, "version": version, "config": config}
    return {**value, "content_hash": digest(value)}


def build(saved):
    try:
        name, version, config = saved["id"], saved["version"], saved["config"]
        declared, schema, factory = REGISTRY[name]
        if version != declared or saved["content_hash"] != digest(
            {"id": name, "version": version, "config": config}
        ):
            raise ValueError
        return factory(schema.model_validate(config))
    except (KeyError, TypeError, ValueError, ValidationError):
        raise DomainError(
            "agent_hook_mismatch", "Pinned hook configuration cannot be restored"
        ) from None
