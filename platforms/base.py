"""Runtime injection shared by otherwise independent platform adapters."""


class PlatformAdapter:
    def __init__(self, core, services=None):
        self.core = core
        self.services = services if services is not None else core
