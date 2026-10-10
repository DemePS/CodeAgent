"""coding_agent, the former name of the module, is an alias of seedagent: the same objects, one shared state."""

import importlib

import seedagent


def test_a_submodule_imported_by_its_old_name_is_the_same_module():
    old = importlib.import_module("coding_agent.tools.files")
    new = importlib.import_module("seedagent.tools.files")
    assert old is new
    assert old.__name__ == "seedagent.tools.files" and old.__spec__.name == "seedagent.tools.files"   # not renamed by the alias


def test_state_is_shared_between_the_two_names():
    import coding_agent.state as old_state
    from seedagent import state as new_state
    assert old_state is new_state
    previous = new_state.auto_mode
    try:
        old_state.auto_mode = not previous
        assert new_state.auto_mode is (not previous)
    finally:
        new_state.auto_mode = previous


def test_the_public_names_are_reachable_from_the_old_package():
    import coding_agent
    from coding_agent import configure, register_tool, session
    from coding_agent.ui import UI
    from seedagent.ui import UI as NewUI
    assert configure is seedagent.configure and register_tool is seedagent.register_tool
    assert session is seedagent.session and UI is NewUI
    assert coding_agent.active_provider is seedagent.active_provider


def test_the_old_command_module_still_runs():
    from coding_agent.cli import main
    from seedagent.cli import main as new_main
    assert main is new_main
