"""vigiora_things: a platform-neutral client for the Vigiora Things model.

Pure Python (the default transport uses aiohttp when installed); no Home
Assistant imports, so it can be split into its own package. See
docs/integrations/things-model.md in the Vigiora server repository.
"""

from .client import (
    AiohttpTransport,
    EventSubscription,
    Response,
    StreamControl,
    ThingsClient,
    Transport,
)
from .errors import (
    ThingsApiError,
    ThingsAuthError,
    ThingsConnectionError,
    ThingsError,
    ThingsNotSupported,
)
from .mapping import EntityPlan, Rule, plan, rules_for_event
from .models import (
    STATE_REDUCERS,
    Capability,
    CapabilitySpec,
    Catalog,
    CommandSpec,
    ServerInfo,
    Thing,
    ThingEvent,
    apply_event,
)
from .sse import SSEFrame, parse_sse

__version__ = "0.1.0"

__all__ = [
    "STATE_REDUCERS",
    "AiohttpTransport",
    "Capability",
    "CapabilitySpec",
    "Catalog",
    "CommandSpec",
    "EntityPlan",
    "EventSubscription",
    "Response",
    "Rule",
    "SSEFrame",
    "ServerInfo",
    "StreamControl",
    "Thing",
    "ThingEvent",
    "ThingsApiError",
    "ThingsAuthError",
    "ThingsClient",
    "ThingsConnectionError",
    "ThingsError",
    "ThingsNotSupported",
    "Transport",
    "apply_event",
    "parse_sse",
    "plan",
    "rules_for_event",
]
