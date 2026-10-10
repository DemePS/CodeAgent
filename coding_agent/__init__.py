"""coding_agent: the former name of the module, kept so that existing code keeps working.

`import coding_agent` and `import coding_agent.x.y` give the very same module objects as `seedagent` and `seedagent.x.y`: one copy of
each, one shared state. New code should import `seedagent`.
"""

import importlib
import importlib.abc
import importlib.util
import sys

import seedagent

__path__: list[str] = []  # a package, so that its submodules go through the finder below
_PREFIX = "coding_agent."


class _Alias(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """coding_agent.a.b is the module seedagent.a.b, not a second copy of it."""

    def __init__(self) -> None:
        self._specs: dict = {}

    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(_PREFIX):
            return importlib.util.spec_from_loader(fullname, self)
        return None

    def create_module(self, spec):
        real = importlib.import_module("seedagent." + spec.name[len(_PREFIX):])
        self._specs[real.__name__] = real.__spec__  # the import system is about to overwrite it with the alias's
        return real

    def exec_module(self, module) -> None:
        module.__spec__ = self._specs.pop(module.__name__, module.__spec__)


sys.meta_path.insert(0, _Alias())


def __getattr__(name: str):
    return getattr(seedagent, name)
