"""ATS dispatch only; hostname and submission trust still belong to common guards."""
from .ashby import AshbyAdapter
from .greenhouse import GreenhouseAdapter
from .lever import LeverAdapter

ATS_ADAPTERS = {adapter.name: adapter for adapter in (AshbyAdapter, GreenhouseAdapter, LeverAdapter)}


def ats_adapter(provider, core):
    return ATS_ADAPTERS[provider](core)


def application_key(url, core):
    provider = core.ats_provider(url)
    return ATS_ADAPTERS[provider].application_key(url) if provider in ATS_ADAPTERS else None
